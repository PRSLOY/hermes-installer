"""Transport-level garbage must be reported as NETWORK, not as a vague VERIFY.

A proxy or half-dead link can raise http.client.HTTPException subclasses
(BadStatusLine / IncompleteRead) that are neither urllib.error.HTTPError nor
OSError. Before the fix those escaped request(), were caught by configure.py's
generic handler, and surfaced as "Не удалось проверить Hermes" -- telling the
user nothing about the actual connection problem. They are now a transport
failure (status 0), so request_with_retry retries and a persistent failure ends
in the correct NETWORK verdict.
"""
import http.client
import unittest
from unittest import mock

from backend import provider


class TransportErrorTests(unittest.TestCase):
    def _open_raises(self, exc):
        opener = mock.Mock()
        opener.open = mock.Mock(side_effect=exc)
        return mock.patch.object(provider.urllib.request, 'build_opener', return_value=opener)

    def test_bad_status_line_is_a_transport_failure(self):
        with self._open_raises(http.client.BadStatusLine('HTTP garbage')):
            status, body = provider.request('https://example.invalid/v1', 'k', '/models')
        self.assertEqual(status, 0, 'garbage response must be status 0 (transport)')
        self.assertIsNone(body)

    def test_incomplete_read_is_a_transport_failure(self):
        with self._open_raises(http.client.IncompleteRead(b'partial')):
            status, body = provider.request('https://example.invalid/v1', 'k', '/models')
        self.assertEqual(status, 0)

    def test_transport_garbage_ends_as_network_after_retries(self):
        """request_with_retry over the real request() still ends in NETWORK."""
        with self._open_raises(http.client.BadStatusLine('HTTP garbage')):
            with self.assertRaises(provider.Failure) as ctx:
                provider.probe_model('https://example.invalid/v1', 'k', 'm')
        self.assertEqual(ctx.exception.code, 'NETWORK')


if __name__ == '__main__':
    unittest.main(verbosity=2)
