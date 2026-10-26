import io
from http.client import BadStatusLine, HTTPResponse, IncompleteRead
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from jev_labeler import transport
from jev_labeler.transport import APIError, NoRedirect, request_json


def ok_response(payload=b'{"ok": true}'):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = payload
    return response


class TransportTests(unittest.TestCase):
    @patch('jev_labeler.transport.build_opener')
    def test_protocol_errors_are_sanitized(self, opener):
        for failure in (BadStatusLine('PRIVATE RESPONSE'), IncompleteRead(b'PRIVATE RESPONSE')):
            with self.subTest(error=type(failure).__name__):
                opener.return_value.open.side_effect = failure
                with self.assertRaisesRegex(RuntimeError, 'API connection failed') as error:
                    request_json('https://api.github.com/x', 'secret')
                self.assertNotIn('PRIVATE', str(error.exception))

    @patch('jev_labeler.transport.build_opener')
    def test_truncated_chunked_response_is_sanitized(self, opener):
        socket = MagicMock()
        socket.makefile.return_value = io.BytesIO(
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n20\r\nPRIVATE')
        response = HTTPResponse(socket)
        response.begin()
        opener.return_value.open.return_value = response
        with self.assertRaisesRegex(RuntimeError, 'API connection failed') as error:
            request_json('https://api.github.com/x', 'secret')
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertTrue(response.isclosed())

    def test_no_redirects(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://attacker.test'))

    @patch('jev_labeler.transport.build_opener')
    def test_http_errors_do_not_leak_response(self, opener):
        opener.return_value.open.side_effect = HTTPError('https://secret', 403, 'TOKEN', {}, io.BytesIO(b'PRIVATE PR'))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403') as error:
            request_json('https://api.github.com/x', 'secret')
        self.assertNotIn('TOKEN', str(error.exception))
        self.assertNotIn('PRIVATE', str(error.exception))

    @patch('jev_labeler.transport.build_opener')
    def test_bounded_response(self, opener):
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'123456'
        with self.assertRaisesRegex(RuntimeError, 'safety limit'):
            request_json('https://api.github.com/x', 'secret', limit=5)

    @patch('jev_labeler.transport.build_opener')
    def test_invalid_json_sanitized(self, opener):
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'PRIVATE'
        with self.assertRaisesRegex(RuntimeError, 'invalid JSON'):
            request_json('https://api.github.com/x', 'secret')

    @patch('jev_labeler.transport.time.sleep')
    @patch('jev_labeler.transport.build_opener')
    def test_transient_get_is_retried_after_retry_after(self, opener, sleep):
        opener.return_value.open.side_effect = [
            HTTPError('https://secret', 503, 'TOKEN', {'Retry-After': '2'}, io.BytesIO(b'PRIVATE')),
            ok_response(),
        ]
        self.assertEqual(request_json('https://api.github.com/x', 'secret'), {'ok': True})
        self.assertEqual(opener.return_value.open.call_count, 2)
        sleep.assert_called_once_with(2.0)

    @patch('jev_labeler.transport.time.sleep')
    @patch('jev_labeler.transport.build_opener')
    def test_exhausted_retries_fail_without_leaking_bodies(self, opener, sleep):
        opener.return_value.open.side_effect = [
            HTTPError('https://secret', status, 'TOKEN', {}, io.BytesIO(b'PRIVATE PR'))
            for status in (429, 502, 504)]
        with self.assertRaises(APIError) as error:
            request_json('https://api.github.com/x', 'secret')
        self.assertEqual(error.exception.status, 504)
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(opener.return_value.open.call_count, transport.RETRY_ATTEMPTS)
        self.assertEqual(sleep.call_count, transport.RETRY_ATTEMPTS - 1)

    @patch('jev_labeler.transport.time.sleep')
    @patch('jev_labeler.transport.build_opener')
    def test_writes_and_client_errors_are_never_retried(self, opener, sleep):
        for method, status in (('POST', 503), ('DELETE', 429), ('GET', 404), ('GET', 403)):
            with self.subTest(method=method, status=status):
                opener.return_value.open.reset_mock()
                opener.return_value.open.side_effect = HTTPError(
                    'https://secret', status, 'TOKEN', {}, io.BytesIO(b'PRIVATE'))
                with self.assertRaises(APIError) as error:
                    request_json('https://api.github.com/x', 'secret', method, {'a': 1})
                self.assertEqual(error.exception.status, status)
                self.assertEqual(opener.return_value.open.call_count, 1)
        sleep.assert_not_called()

    def test_retry_delay_is_bounded_and_ignores_unsafe_values(self):
        for raw, expected in ((None, 0.5), ('2', 2.0), ('60', transport.MAX_RETRY_DELAY),
                              ('0', 0.5), ('-3', 0.5), ('nan', 0.5), ('inf', 0.5),
                              ('Wed, 21 Oct 2015 07:28:00 GMT', 0.5)):
            with self.subTest(retry_after=raw):
                self.assertEqual(transport._retry_delay({'Retry-After': raw}, 0), expected)
        self.assertEqual(transport._retry_delay({'Retry-After': 'bogus'}, 2), 2.0)
        self.assertEqual(transport._retry_delay(None, 2), 2.0)
