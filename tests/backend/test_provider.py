"""Offline tests only: no real provider, installation or live config writes."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('provider', ROOT / 'backend/provider.py')
p = importlib.util.module_from_spec(spec)
if spec.loader and (ROOT / 'backend/provider.py').exists():
    spec.loader.exec_module(p)

class ProviderTests(unittest.TestCase):
    def test_endpoint_never_guesses_other_hosts(self):
        self.assertTrue(hasattr(p, 'base_url_candidates'), 'safe endpoint adapter not implemented')
        self.assertEqual(p.base_url_candidates('https://chat.example.test/v1'), ['https://chat.example.test/v1'])
        for value in ['http://example.test/v1', 'https://u:p@example.test/v1', 'https://example.test/v1?q=x', 'https://example.test/v1#x']:
            with self.assertRaises(ValueError):
                p.base_url_candidates(value)

    def test_normalization_requires_explicit_api_path(self):
        self.assertEqual(p.base_url_candidates('  example.test/custom/api///  '), ['https://example.test/custom/api'])
        for value in ['https://example.test', 'example.test/', 'https://example.test/a\\\\b', 'https://example.test/a\npath']:
            with self.assertRaises(ValueError): p.base_url_candidates(value)

    def test_probe_rejects_auth_quota_malformed_timeout(self):
        self.assertTrue(hasattr(p, 'probe_model'), 'probe adapter missing')
        for status, body, expected in [(401, {}, 'AUTH'), (429, {}, 'QUOTA'), (402, {}, 'QUOTA'), (0, {}, 'NETWORK'), (200, {}, 'VERIFY'), (200, {'choices':[{'message': {'content':'quota exceeded'}}]}, 'QUOTA')]:
            with self.assertRaises(p.Failure) as cm:
                p.probe_model('https://example.test/v1', 'secret', 'model', transport=lambda *a: (status, body))
            self.assertEqual(cm.exception.code, expected)
        self.assertEqual(p.probe_model('https://example.test/v1', 'secret', 'model', transport=lambda *a: (200, {'choices':[{'message': {'content':'Tokyo'}}]})), 'model')

    def test_quota_messages_tell_empty_key_from_overload(self):
        # 402 = nothing on the key (e.g. Dahl gift tokens left in the account pool);
        # 429 = rate limit / overload / some gateways also use it for no credits.
        with self.assertRaises(p.Failure) as empty:
            p.check_status(402)
        with self.assertRaises(p.Failure) as busy:
            p.check_status(429)
        self.assertEqual((empty.exception.code, busy.exception.code), ('QUOTA', 'QUOTA'))
        self.assertIn('Allocate', str(empty.exception))
        self.assertIn('минуту', str(busy.exception))
        self.assertIn('баланс', str(busy.exception))
        self.assertNotEqual(str(empty.exception), str(busy.exception))

    def test_tls_uses_certifi_bundle_not_bare_windows_store(self):
        # Sandbox 2026-09-22: a fresh Windows lacks Go Daddy/Starfield roots until
        # the OS fetches them on demand; Python reads the store as-is, so Telegram,
        # Wolfram, Context7 and DeepWiki failed while PowerShell succeeded. certifi
        # is pinned by Hermes (certifi==2026.5.20).
        import certifi, ssl
        ctx = p.tls_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        bundle = ssl.create_default_context(cafile=certifi.where())
        self.assertGreaterEqual(len(ctx.get_ca_certs()), len(bundle.get_ca_certs()))
        # Every installer opener must use it, not the default context.
        src = {name: (ROOT / 'backend' / name).read_text(encoding='utf-8') for name in ('provider.py', 'extras.py', 'telegram.py')}
        for name, text in src.items():
            self.assertNotIn('build_opener(NoRedirect())', text, name)
            self.assertIn('tls_context()', text, name)

    def test_redirects_not_followed(self):
        self.assertTrue(hasattr(p, 'NoRedirect'), 'redirect protection missing')
        self.assertIsNone(p.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://foreign.test'))

    def test_config_preserves_settings_and_idempotence(self):
        self.assertTrue(hasattr(p, 'configured_text'), 'config adapter missing')
        src = '# keep\nterminal:\n  timeout: 99\nmodel: ""\ncustom_providers: []\n'
        first = p.configured_text(src, 'https://example.test/v1', 'demo')
        self.assertEqual(first, p.configured_text(first, 'https://example.test/v1', 'demo'))
        self.assertIn('# keep\nterminal:\n  timeout: 99\n', first)
        self.assertNotIn('secret-test-key', first)
        self.assertIn('${HERMES_SUBSCRIBER_API_KEY}', first)
        with self.assertRaises(p.Failure):
            p.configured_text('model:\n  default: existing\n', 'https://example.test/v1', 'demo')

    def test_failed_hermes_reply_is_not_success(self):
        self.assertTrue(hasattr(p, 'check_hermes_result'), 'Hermes verifier missing')
        for result in [{}, {'completed':False, 'final_response':'error'}, {'completed':True, 'final_response':''}, {'completed':True, 'final_response':'quota exceeded'}]:
            with self.assertRaises(p.Failure):
                p.check_hermes_result(result)
        p.check_hermes_result({'completed':True, 'final_response':'Tokyo'})

    def test_key_cannot_cross_origin_in_runtime_httpx(self):
        self.assertTrue(hasattr(p, 'guard_runtime_http'), 'runtime redirect guard missing')
        import httpx
        with p.guard_runtime_http('https://example.test/v1', 'sentinel-key'):
            c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'ok':True})))
            with self.assertRaises(p.Failure):
                c.get('https://foreign.test/v1', headers={'Authorization':'Bearer sentinel-key'})
            self.assertEqual(c.get('https://example.test/v1',headers={'Authorization':'Bearer sentinel-key'}).status_code,200)
            c.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
