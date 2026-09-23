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


if __name__ == '__main__':
    unittest.main(verbosity=2)
