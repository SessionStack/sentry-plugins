from __future__ import absolute_import

import responses

from exam import fixture
from sentry.testutils import PluginTestCase

from sentry_plugins.sessionstack.plugin import SessionStackPlugin


EXPECTED_SESSION_ADDRESS = (
    'https://app.sessionstack.com/player/#/sessions/588778a6c5762c1d566653ff'
    '?access_token=example-access-token'
)

GENERATE_SHAREABLE_URL_ADDRESS = (
    'https://api.sessionstack.com/sentry/sessions/'
    '588778a6c5762c1d566653ff/shareable_url'
)


class SessionStackPluginTest(PluginTestCase):
    @fixture
    def plugin(self):
        return SessionStackPlugin()

    def test_conf_key(self):
        assert self.plugin.conf_key == 'sessionstack'

    def test_entry_point(self):
        self.assertAppInstalled('sessionstack', 'sentry_plugins.sessionstack')
        self.assertPluginInstalled('sessionstack', self.plugin)

    @responses.activate
    def test_config_validation(self):
        responses.add(responses.GET, 'https://api.sessionstack.com/v1/websites/0')

        config = {
            'account_email': 'user@example.com',
            'api_token': 'example-api-token',
            'website_id': 0
        }

        self.plugin.validate_config(self.project, config)

    @responses.activate
    def test_event_preprocessing(self):
        responses.add(responses.GET, GENERATE_SHAREABLE_URL_ADDRESS)

        self.plugin.enable(self.project)
        self.plugin.set_option('account_email', 'user@example.com',
                               self.project)
        self.plugin.set_option('api_token', 'example-api-token', self.project)
        self.plugin.set_option('website_id', 0, self.project)

        event = {
            'project': self.project.id,
            'contexts': {
                'sessionstack': {
                    'session_id': '588778a6c5762c1d566653ff',
                    'type': 'sessionstack'
                }
            },
            'platform': 'javascript'
        }

        event_preprocessors = self.plugin.get_event_preprocessors(event)
        add_sessionstack_context = event_preprocessors[0]

        processed_event = add_sessionstack_context(event)

        event_contexts = processed_event.get('contexts')
        sessionstack_context = event_contexts.get('sessionstack')
        session_url = sessionstack_context.get('session_url')

        assert session_url == EXPECTED_SESSION_ADDRESS
