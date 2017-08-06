from __future__ import absolute_import

import requests

from sentry.http import safe_urlopen

from .utils import get_basic_auth, remove_trailing_slashes


API_URL = 'https://api.sessionstack.com'

WEBSITES_ENDPOINT = '/v1/websites/{website_id}'
SESSION_URL_ENDPOINT = '/sentry/sessions/{session_id}/shareable_url'


class SessionStackClient(object):
    def __init__(self, account_email, api_token, website_id, **kwargs):
        self.website_id = website_id

        api_url = kwargs.get('api_url') or API_URL
        self.api_url = remove_trailing_slashes(api_url)

        self.request_headers = {
            'Authorization': get_basic_auth(account_email, api_token),
            'Content-Type': 'application/json'
        }

    def validate_api_access(self):
        website_endpoint = WEBSITES_ENDPOINT.format(website_id=self.website_id)

        try:
            response = self._make_request(website_endpoint, 'GET')
        except requests.exceptions.ConnectionError:
            raise InvalidApiUrlError

        if response.status_code == requests.codes.UNAUTHORIZED:
            raise UnauthorizedError
        elif response.status_code == requests.codes.BAD_REQUEST:
            raise InvalidWebsiteIdError
        elif response.status_code == requests.codes.NOT_FOUND:
            raise InvalidApiUrlError

        response.raise_for_status()

    def get_session_url(self, session_id, event_timestamp):
        session_url_endpoint = SESSION_URL_ENDPOINT.format(
            website_id=self.website_id,
            session_id=session_id
        )

        try:
            response = self._make_request(session_url_endpoint, 'GET', params={
                'event_timestamp': event_timestamp
            })

            session_url = response.content.get('url')

            return session_url
        except requests.exceptions.ConnectionError:
            return None

    def _make_request(self, endpoint, method, **kwargs):
        url = self.api_url + endpoint

        request_kwargs = {'method': method, 'headers': self.request_headers}

        body = kwargs.get('body')
        if body:
            request_kwargs['json'] = body

        return safe_urlopen(url, **request_kwargs)


class UnauthorizedError(Exception):
    pass


class InvalidWebsiteIdError(Exception):
    pass


class InvalidApiUrlError(Exception):
    pass
