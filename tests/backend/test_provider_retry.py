"""RED/GREEN: the API check must retry transient transport failures instead of
reporting a dead network on the first timeout.

Live evidence (Windows Sandbox, 2026-09-18): the FIRST HTTPS call to
https://openrouter.ai/api/v1/models took 65 s (cold TLS/proxy), while the same
call afterwards took ~1 s. probe_model used a single request with timeout=45 and
mapped status 0 straight to Failure('NETWORK'), so a bad key surfaced as
"API не отвечает" instead of the correct 401/AUTH verdict.

Contract kept intact:
  * a definitive provider verdict (401/403 -> AUTH, 402/429 -> QUOTA) is raised
    on the FIRST response, never retried away;
  * retries are bounded, so a genuinely dead link still ends in NETWORK;
  * no provider-controlled text ever reaches the caller.
"""
import unittest

from backend import provider


class ApiRetryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._pauses = provider.RETRY_PAUSES
        provider.RETRY_PAUSES = (0, 0)   # no real sleeping in tests

    def tearDown(self):
        provider.RETRY_PAUSES = self._pauses

    def transport(self, statuses, body=None):
        """Return a transport that yields the given statuses in order."""
        def _transport(base, key, path, payload=None):
            self.calls.append(path)
            index = min(len(self.calls) - 1, len(statuses) - 1)
            return statuses[index], body
        return _transport

    def ok_body(self):
        return {'id': 'chatcmpl-1', 'choices': [{'message': {'content': 'Tokyo'}}]}

    def test_transient_timeout_then_401_reports_auth_not_network(self):
        """One timed-out attempt must not mask a definitive 401."""
        transport = self.transport([0, 401], body=self.ok_body())
        with self.assertRaises(provider.Failure) as ctx:
            provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=transport)
        self.assertEqual(ctx.exception.code, 'AUTH')
        self.assertGreaterEqual(len(self.calls), 2, 'timeout was not retried at all')

    def test_timeout_then_success_is_accepted(self):
        transport = self.transport([0, 200], body=self.ok_body())
        self.assertEqual(provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=transport), 'm')
        self.assertEqual(len(self.calls), 2)

    def test_repeated_timeouts_still_fail_closed_as_network(self):
        """A truly dead link must still end in NETWORK, after bounded retries."""
        transport = self.transport([0], body=None)
        with self.assertRaises(provider.Failure) as ctx:
            provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=transport)
        self.assertEqual(ctx.exception.code, 'NETWORK')
        self.assertLessEqual(len(self.calls), 4, 'retry loop is unbounded')

    def test_definitive_401_is_not_retried(self):
        transport = self.transport([401], body=None)
        with self.assertRaises(provider.Failure) as ctx:
            provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=transport)
        self.assertEqual(ctx.exception.code, 'AUTH')
        self.assertEqual(len(self.calls), 1, 'a definitive verdict must not be retried')

    def test_quota_verdict_is_not_retried(self):
        transport = self.transport([429], body=None)
        with self.assertRaises(provider.Failure) as ctx:
            provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=transport)
        self.assertEqual(ctx.exception.code, 'QUOTA')
        self.assertEqual(len(self.calls), 1)

    def test_gateway_502_then_success_is_accepted(self):
        """Live 2026-09-23: GWarden 502 for a few seconds must not fail the key check."""
        transport = self.transport([502, 200], body=self.ok_body())
        self.assertEqual(provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=transport), 'm')
        self.assertEqual(len(self.calls), 2)

    def test_persistent_gateway_errors_end_in_network_after_bounded_retries(self):
        for status in (502, 503, 504):
            self.calls = []
            with self.assertRaises(provider.Failure) as ctx:
                provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=self.transport([status]))
            self.assertEqual(ctx.exception.code, 'NETWORK')
            self.assertEqual(len(self.calls), provider.TRANSPORT_ATTEMPTS)

    def test_internal_500_is_a_verdict_not_retried(self):
        with self.assertRaises(provider.Failure):
            provider.probe_model('https://example.invalid/v1', 'k', 'm', transport=self.transport([500]))
        self.assertEqual(len(self.calls), 1)

    def test_pauses_between_attempts(self):
        slept = []
        provider.RETRY_PAUSES = (3, 8)
        status, _ = provider.request_with_retry('https://example.invalid/v1', 'k', '/models',
                                                transport=self.transport([0, 502, 200]), sleep=slept.append)
        self.assertEqual((status, slept), (200, [3, 8]))

if __name__ == '__main__':
    unittest.main(verbosity=2)
