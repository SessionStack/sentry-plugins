from __future__ import absolute_import

import logging
import six
from six.moves.urllib.parse import parse_qs, quote_plus, unquote_plus, urlencode, urlsplit, urlunsplit

from rest_framework.response import Response

from django.conf import settings
from django.conf.urls import url

from sentry.models import GroupMeta
from sentry.plugins.bases.issue2 import IssuePlugin2, IssueGroupActionEndpoint, PluginError
from sentry.utils.http import absolute_uri

from sentry_plugins.base import CorePluginMixin
from sentry_plugins.jira.client import JIRAClient, JIRAError, JIRAUnauthorized

# A list of common builtin custom field types for JIRA for easy reference.
JIRA_CUSTOM_FIELD_TYPES = {
    'select': 'com.atlassian.jira.plugin.system.customfieldtypes:select',
    'textarea': 'com.atlassian.jira.plugin.system.customfieldtypes:textarea',
    'multiuserpicker': 'com.atlassian.jira.plugin.system.customfieldtypes:multiuserpicker'
}

ERR_UNAUTHORIZED = (
    'Unauthorized: either your username and password were '
    'invalid or you do not have access'
)

ERR_INTERNAL = (
    'An internal error occurred with the integration and the '
    'Sentry team has been notified'
)


class JiraPlugin(CorePluginMixin, IssuePlugin2):
    description = 'Integrate JIRA issues by linking a project.'
    slug = 'jira'
    title = 'JIRA'
    conf_title = title
    conf_key = slug
    allowed_actions = ('create', 'unlink')

    asset_key = 'jira'
    assets = [
        'dist/jira.js',
    ]

    def get_group_urls(self):
        _patterns = super(JiraPlugin, self).get_group_urls()
        _patterns.append(url(r'^autocomplete',
                             IssueGroupActionEndpoint.as_view(view_method_name='view_autocomplete',
                                                              plugin=self)))
        return _patterns

    def is_configured(self, request, project, **kwargs):
        if not self.get_option('default_project', project):
            return False
        return True

    def get_group_description(self, request, group, event):
        # mostly the same as parent class, but change ``` to {code}
        output = [
            absolute_uri(group.get_absolute_url()),
        ]
        body = self.get_group_body(request, group, event)
        if body:
            output.extend([
                '',
                '{code}',
                body,
                '{code}',
            ])
        return '\n'.join(output)

    def build_dynamic_field(self, group, field_meta):
        """
        Builds a field based on JIRA's meta field information
        """
        schema = field_meta['schema']

        # set up some defaults for form fields
        fieldtype = 'text'
        fkwargs = {
            'label': field_meta['name'],
            'required': field_meta['required'],
        }
        # override defaults based on field configuration
        if (schema['type'] in ['securitylevel', 'priority']
                or schema.get('custom') == JIRA_CUSTOM_FIELD_TYPES['select']):
            fieldtype = 'select'
            fkwargs['choices'] = self.make_choices(field_meta.get('allowedValues'))
        elif schema.get('items') == 'user' or schema['type'] == 'user':
            fieldtype = 'select'
            sentry_url = '/api/0/issues/%s/plugins/%s/autocomplete' % (group.id, self.slug)
            fkwargs['url'] = '%s?jira_url=%s' % (sentry_url, quote_plus(field_meta.get('autoCompleteUrl')))
            fkwargs['has_autocomplete'] = True
            fkwargs['placeholder'] = 'Start typing to search for a user'
        elif schema['type'] in ['timetracking']:
            # TODO: Implement timetracking (currently unsupported alltogether)
            return None
        elif schema.get('items') in ['worklog', 'attachment']:
            # TODO: Implement worklogs and attachments someday
            return None
        elif schema['type'] == 'array' and schema['items'] != 'string':
            fieldtype = 'select'
            fkwargs.update({
                'multiple': True,
                'choices': self.make_choices(field_meta.get('allowedValues')),
                'default': []
            })

        # break this out, since multiple field types could additionally
        # be configured to use a custom property instead of a default.
        if schema.get('custom'):
            if schema['custom'] == JIRA_CUSTOM_FIELD_TYPES['textarea']:
                fieldtype = 'textarea'

        fkwargs['type'] = fieldtype
        return fkwargs

    def get_issue_type_meta(self, issue_type, meta):
        issue_types = meta['issuetypes']
        issue_type_meta = None
        if issue_type:
            matching_type = [t for t in issue_types if t['id'] == issue_type]
            issue_type_meta = matching_type[0] if len(matching_type) > 0 else None

        # still no issue type? just use the first one.
        if not issue_type_meta:
            issue_type_meta = issue_types[0]

        return issue_type_meta

    def get_new_issue_fields(self, request, group, event, **kwargs):
        fields = super(JiraPlugin, self).get_new_issue_fields(request, group, event, **kwargs)

        jira_project_key = self.get_option('default_project', group.project)

        client = self.get_jira_client(group.project)
        try:
            meta = client.get_create_meta_for_project(jira_project_key)
        except JIRAUnauthorized:
            raise PluginError('Something went wrong. Please check your configuration.')

        if not meta:
            raise PluginError('Error in JIRA configuration, no projects '
                              'found for user %s.' % client.username)

        # check if the issuetype was passed as a GET parameter
        issue_type = None
        if request is not None:
            issue_type = request.GET.get('issue_type')

        if issue_type is None:
            issue_type = self.get_option('default_issue_type', group.project)

        issue_type_meta = self.get_issue_type_meta(issue_type, meta)

        fields = [{
            'name': 'project',
            'label': 'Jira Project',
            'choices': ((meta['id'], jira_project_key),),
            'default': meta['id'],
            'type': 'select',
            'readonly': True
        }] + fields + [{
            'name': 'issuetype',
            'label': 'Issue Type',
            'default': issue_type or issue_type_meta['id'],
            'type': 'select',
            'choices': self.make_choices(meta['issuetypes'])
        }]

        # title is renamed to summary before sending to JIRA
        standard_fields = [f['name'] for f in fields] + ['summary']
        ignored_fields = (self.get_option('ignored_fields', group.project) or '').split(',')

        # apply ordering to fields based on some known built-in JIRA fields.
        # otherwise weird ordering occurs.
        anti_gravity = {"priority": -150,
                        "fixVersions": -125,
                        "components": -100,
                        "security": -50}

        dynamic_fields = issue_type_meta.get('fields').keys()
        dynamic_fields.sort(key=lambda f: anti_gravity.get(f) or 0)
        # build up some dynamic fields based on required shit.
        for field in dynamic_fields:
            if field in standard_fields or field in [x.strip() for x in ignored_fields]:
                # don't overwrite the fixed fields for the form.
                continue
            mb_field = self.build_dynamic_field(group, issue_type_meta['fields'][field])
            if mb_field:
                mb_field['name'] = field
                fields.append(mb_field)

        for field in fields:
            if field['name'] == 'priority':
                # whenever priorities are available, put the available ones in the list.
                # allowedValues for some reason doesn't pass enough info.
                field['choices'] = self.make_choices(client.get_priorities().json)
                field['default'] = self.get_option('default_priority', group.project) or ''
            elif field['name'] == 'fixVersions':
                field['choices'] = self.make_choices(client.get_versions(jira_project_key).json)

        return fields

    def get_issue_label(self, group, issue_id, **kwargs):
        return issue_id

    def get_issue_url(self, group, issue_id, **kwargs):
        instance = self.get_option('instance_url', group.project)
        return "%s/browse/%s" % (instance, issue_id)

    def view_autocomplete(self, request, group, **kwargs):
        query = request.GET.get('autocomplete_query')
        jira_url = request.GET.get('jira_url')
        if jira_url:
            jira_url = unquote_plus(jira_url)
            parsed = list(urlsplit(jira_url))
            jira_query = parse_qs(parsed[3])

            jira_client = self.get_jira_client(group.project)

            project = self.get_option('default_project', group.project)

            if '/rest/api/latest/user/' in jira_url:  # its the JSON version of the autocompleter
                is_xml = False
                jira_query['username'] = query.encode('utf8')
                jira_query.pop('issueKey', False)  # some reason JIRA complains if this key is in the URL.
                jira_query['project'] = project.encode('utf8')
            else:  # its the stupid XML version of the API.
                is_xml = True
                jira_query['query'] = query.encode('utf8')
                if jira_query.get('fieldName'):
                    jira_query['fieldName'] = jira_query['fieldName'][0]  # for some reason its a list.

            parsed[3] = urlencode(jira_query)
            final_url = urlunsplit(parsed)

            autocomplete_response = jira_client.get_cached(final_url)
            users = []

            if is_xml:
                for userxml in autocomplete_response.xml.findAll("users"):
                    users.append({
                        'id': userxml.find('name').text,
                        'text': userxml.find('html').text
                    })
            else:
                for user in autocomplete_response.json:
                    users.append({
                        'id': user['name'],
                        'text': '%s - %s (%s)' % (user['displayName'], user['emailAddress'], user['name'])
                    })

            field = request.GET.get('autocomplete_field')
            return Response({field: users})

    def message_from_error(self, exc):
        if isinstance(exc, JIRAUnauthorized):
            return ERR_UNAUTHORIZED
        elif isinstance(exc, JIRAError):
            return ('Error Communicating with Jira (HTTP %s): %s' % (
                exc.status_code,
                exc.json.get('message', 'unknown error') if exc.json else 'unknown error',
            ))
        else:
            return ERR_INTERNAL

    def raise_error(self, exc):
        # TODO(jess): switch this from JIRAError to the standard
        # shared exeption classes
        if not isinstance(exc, JIRAError):
            self.logger.exception(six.text_type(exc))
        raise PluginError(self.message_from_error(exc))

    def create_issue(self, request, group, form_data, **kwargs):
        cleaned_data = {}

        # protect against mis-configured plugin submitting a form without an
        # issuetype assigned.
        if not form_data.get('issuetype'):
            raise PluginError('Issue Type is required.')

        jira_project_key = self.get_option('default_project', group.project)
        client = self.get_jira_client(group.project)
        meta = client.get_create_meta_for_project(jira_project_key)

        if not meta:
            raise PluginError('Something went wrong. Check your plugin configuration.')

        issue_type_meta = self.get_issue_type_meta(form_data['issuetype'], meta)

        fs = issue_type_meta['fields']
        for field in fs.keys():
            f = fs[field]
            if field == 'description':
                cleaned_data[field] = form_data[field]
            elif field == 'summary':
                cleaned_data['summary'] = form_data['title']
            if field in form_data.keys():
                v = form_data.get(field)
                if v:
                    schema = f['schema']
                    if schema.get('type') == 'string' and not schema.get('custom') == JIRA_CUSTOM_FIELD_TYPES['select']:
                        continue  # noop
                    if schema['type'] == 'user' or schema.get('items') == 'user':
                        v = {'name': v}
                    elif schema.get('custom') == JIRA_CUSTOM_FIELD_TYPES.get('multiuserpicker'):
                        # custom multi-picker
                        v = [{'name': v}]
                    elif schema['type'] == 'array' and schema.get('items') != 'string':
                        v = [{'id': vx} for vx in v]
                    elif schema['type'] == 'array' and schema.get('items') == 'string':
                        v = [v]
                    elif schema.get('custom') == JIRA_CUSTOM_FIELD_TYPES.get('textarea'):
                        v = v
                    elif (schema.get('type') != 'string'
                            or schema.get('items') != 'string'
                            or schema.get('custom') == JIRA_CUSTOM_FIELD_TYPES.get('select')):
                        v = {'id': v}
                    cleaned_data[field] = v

        if not (isinstance(cleaned_data['issuetype'], dict)
                and 'id' in cleaned_data['issuetype']):
            # something fishy is going on with this field, working on some JIRA
            # instances, and some not.
            # testing against 5.1.5 and 5.1.4 does not convert (perhaps is no longer included
            # in the projectmeta API call, and would normally be converted in the
            # above clean method.)
            cleaned_data['issuetype'] = {'id': cleaned_data['issuetype']}

        try:
            response = client.create_issue(cleaned_data)
        except Exception as e:
            self.raise_error(e)

        return response.json.get('key')

    def get_jira_client(self, project):
        instance = self.get_option('instance_url', project)
        username = self.get_option('username', project)
        pw = self.get_option('password', project)
        return JIRAClient(instance, username, pw)

    def make_choices(self, x):
        return [(y['id'], y['name'] if 'name' in y else y['value']) for y in x] if x else []

    def validate_config_field(self, project, name, value, actor=None):
        value = super(JiraPlugin, self).validate_config_field(project, name, value, actor)
        # Don't make people update password every time
        if name == 'password':
            value = value or self.get_option('password', project)
        return value

    def validate_config(self, project, config, actor=None):
        """
        ```
        if config['foo'] and not config['bar']:
            raise PluginError('You cannot configure foo with bar')
        return config
        ```
        """
        client = JIRAClient(config['instance_url'],
                            config['username'],
                            config['password'])
        try:
            client.get_projects_list()
        except JIRAError as e:
            self.raise_error(e)

        return config

    def get_configure_plugin_fields(self, request, project, **kwargs):
        instance = self.get_option('instance_url', project)
        username = self.get_option('username', project)
        pw = self.get_option('password', project)
        jira_project = self.get_option('default_project', project)

        project_choices = []
        priority_choices = []
        issue_type_choices = []
        if instance and username and pw:
            client = JIRAClient(instance, username, pw)
            try:
                projects_response = client.get_projects_list()
            except JIRAError:
                projects_response = None
            else:
                projects = projects_response.json
                if projects:
                    project_choices = [(p.get('key'), '%s (%s)' % (p.get('name'), p.get('key'))) for p in projects]

            if jira_project:
                try:
                    priorities_response = client.get_priorities()
                except JIRAError:
                    priorities_response = None
                else:
                    priorities = priorities_response.json
                    if priorities:
                        priority_choices = [(p.get('id'), '%s' % (p.get('name'))) for p in priorities]

                try:
                    meta = client.get_create_meta_for_project(jira_project)
                except JIRAError:
                    meta = None
                else:
                    if meta:
                        issue_type_choices = self.make_choices(meta['issuetypes'])

        return [{
            'name': 'instance_url',
            'label': 'JIRA Instance URL',
            'default': instance,
            'type': 'text',
            'placeholder': 'e.g. "https://jira.atlassian.com"',
            'help': 'It must be visible to the Sentry server'
        }, {
            'name': 'username',
            'label': 'Username',
            'default': username,
            'type': 'text',
            'help': 'Ensure the JIRA user has admin permissions on the project'
        }, {
            'name': 'password',
            'label': 'Password',
            'type': 'secret',
            'required': pw is None,
            'help': 'Only enter a new password if you wish to update the stored value'
                    if pw is not None else None
        }, {
            'name': 'default_project',
            'label': 'Linked Project',
            'type': 'select',
            'choices': project_choices,
            'default': jira_project,
            'required': False
        }, {
            'name': 'ignored_fields',
            'label': 'Ignored Fields',
            'type': 'textarea',
            'required': False,
            'placeholder': 'e.g. "components, security, customfield_10006"',
            'default': self.get_option('ignored_fields', project) or '',
            'help': 'Comma-separated list of properties that you don\'t want to show in the form'
        }, {
            'name': 'default_priority',
            'label': 'Default Priority',
            'type': 'select',
            'choices': priority_choices,
            'required': False,
            'default': self.get_option('default_priority', project)
        }, {
            'name': 'default_issue_type',
            'label': 'Default Issue Type',
            'type': 'select',
            'choices': issue_type_choices,
            'required': False,
            'default': self.get_option('default_issue_type', project)
        }, {
            'name': 'auto_create',
            'label': 'Automatically create JIRA Tickets',
            'default': self.get_option('auto_create', project) or False,
            'type': 'bool',
            'required': False,
            'help': 'Automatically create a JIRA ticket for EVERY new issue'
        }]

    def should_create(self, group, event, is_new):
        if not is_new:
            return False

        if not self.get_option('auto_create', group.project):
            return False

        # XXX(dcramer): Sentry doesn't expect GroupMeta referenced here so we
        # need to populate the cache
        GroupMeta.objects.populate_cache([group])
        if GroupMeta.objects.get_value(group, '%s:tid' % self.get_conf_key(), None):
            return False

        return True

    def post_process(self, group, event, is_new, is_sample, **kwargs):
        if not self.should_create(group, event, is_new):
            return

        fields = self.get_new_issue_fields(None, group, event, **kwargs)

        post_data = {}
        included_fields = set(['priority', 'issuetype', 'title', 'description', 'project'])
        for field in fields:
            name = field['name']
            if name in included_fields:
                post_data[name] = field.get('default')

        if not (post_data.get('priority') and post_data.get('issuetype') and post_data.get('project')):
            return

        interface = event.interfaces.get('sentry.interfaces.Exception')

        if interface:
            post_data['description'] += '\n{code}%s{code}' % interface.get_stacktrace(event, system_frames=False,
                                                                                      max_frames=settings.SENTRY_MAX_STACKTRACE_FRAMES)

        try:
            issue_id = self.create_issue(
                request={},
                group=group,
                form_data=post_data)
        except PluginError as e:
            logging.exception('Error creating JIRA ticket: %s', e)
        else:
            prefix = self.get_conf_key()
            GroupMeta.objects.set_value(group, '%s:tid' % prefix, issue_id)