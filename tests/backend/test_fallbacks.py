"""Offline tests for the backup-providers stage (issue #10).

The live check (provider.select_model) is injected; protect() (a PowerShell ACL
call) is replaced by a recorder so nothing is spawned. Every key below is a fake
built at runtime, never a real credential. No host Hermes file is touched.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
sys.path.insert(0, str(BACKEND))
spec = importlib.util.spec_from_file_location('fallbacks_stage', BACKEND / 'fallbacks.py')
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)
from provider import Failure  # noqa: E402

KEY1 = 'fake-' + 'dahl' + '-key-' + 'A1b2C3d4'
KEY2 = 'fake-' + 'atria' + "-k'ey\\" + 'Z9y8'          # quote + backslash: dotenv escaping
PRIMARY = 'https://gwarden.example/v1'
CONFIG = ('# user comment kept\n'
          'model:\n'
          '  default: "glm-5.3"\n'
          '  provider: "custom"\n'
          '  base_url: "' + PRIMARY + '"\n'
          '  api_key: "${HERMES_SUBSCRIBER_API_KEY}"\n'
          '  api_mode: "chat_completions"\n'
          'agent:\n'
          '  api_max_retries: 7   # tuned by the installer\n'
          'fallback_providers: []\n'
          'display:\n'
          '  busy_input_mode: steer\n')
ENV = "HERMES_SUBSCRIBER_API_KEY='primary-fake-key-123'\n"


def entry(provider_id, endpoint, key, model='m-1'):
    return {'provider_id': provider_id, 'endpoint': endpoint, 'model': model, 'api_key': key}


class Probe:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def __call__(self, base, key, model):
        self.calls.append((base, model))
        if base in self.fail:
            raise self.fail[base]
        return model or 'catalog-model'


class FallbacksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fallbacks-test-')
        self.home = Path(self.tmp.name)
        self.config = self.home / 'config.yaml'
        self.env = self.home / '.env'
        self.config.write_text(CONFIG, encoding='utf-8', newline='')
        self.env.write_text(ENV, encoding='utf-8', newline='')
        self._protect, self._atomic = fb.protect, fb.atomic_write
        self.protected = []
        fb.protect = lambda path: self.protected.append(Path(path).name)

    def tearDown(self):
        fb.protect, fb.atomic_write = self._protect, self._atomic
        self.tmp.cleanup()

    def run_stage(self, entries, probe):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = fb.main(self.home, entries, probe=probe)
        text = out.getvalue() + json.dumps(result)
        for key in (KEY1, KEY2):
            self.assertNotIn(key, text, 'a key leaked into the report')
        return result

    def parsed(self):
        return yaml.safe_load(self.config.read_text(encoding='utf-8'))

    def env_map(self):
        return fb.env_values(self.env.read_bytes())

    # --- success ---------------------------------------------------------------
    def test_two_verified_entries_are_written_to_env_and_chain(self):
        probe = Probe()
        result = self.run_stage([entry('dahl', 'https://dahl.example/v1/', KEY1),
                                 entry('atria', 'api.atria.example/v1', KEY2, model='')], probe)
        self.assertEqual(result, {'ok': True, 'results': [{'provider_id': 'dahl', 'status': 'added'},
                                                          {'provider_id': 'atria', 'status': 'added'}]})
        self.assertEqual(probe.calls, [('https://dahl.example/v1', 'm-1'), ('https://api.atria.example/v1', '')])
        cfg = self.parsed()
        self.assertEqual(cfg['fallback_providers'], [
            {'provider': 'custom', 'model': 'm-1', 'base_url': 'https://dahl.example/v1',
             'api_mode': 'chat_completions', 'key_env': 'HERMES_SUBSCRIBER_FALLBACK_1_KEY'},
            {'provider': 'custom', 'model': 'catalog-model', 'base_url': 'https://api.atria.example/v1',
             'api_mode': 'chat_completions', 'key_env': 'HERMES_SUBSCRIBER_FALLBACK_2_KEY'}])
        expected = yaml.safe_load(CONFIG)
        self.assertEqual({k: v for k, v in cfg.items() if k != 'fallback_providers'},
                         {k: v for k, v in expected.items() if k != 'fallback_providers'})
        text = self.config.read_text(encoding='utf-8')
        self.assertTrue(text.startswith(CONFIG.split('fallback_providers')[0]), 'text before the section changed')
        self.assertTrue(text.endswith('display:\n  busy_input_mode: steer\n'), 'text after the section changed')
        self.assertIn('# tuned by the installer', text)
        for key in (KEY1, KEY2):
            self.assertNotIn(key, text, 'a key reached config.yaml')
        env = self.env_map()
        self.assertEqual(env['HERMES_SUBSCRIBER_FALLBACK_1_KEY'], KEY1)
        self.assertEqual(env['HERMES_SUBSCRIBER_FALLBACK_2_KEY'], KEY2)
        self.assertEqual(env['HERMES_SUBSCRIBER_API_KEY'], 'primary-fake-key-123')
        self.assertEqual(len(self.protected), 1, 'the .env temp file must be ACL-protected before the write')
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['.env', 'config.yaml'], 'temp files left behind')

    def test_env_quoting_matches_configure(self):
        self.env.write_bytes(b"OTHER=1")      # no trailing newline: must not glue lines
        self.run_stage([entry('atria', 'https://api.atria.example/v1', KEY2)], Probe())
        raw = self.env.read_bytes().decode('utf-8')
        quoted = "'" + KEY2.replace('\\', '\\\\').replace("'", "\\'") + "'"
        self.assertEqual(raw, 'OTHER=1\nHERMES_SUBSCRIBER_FALLBACK_1_KEY=' + quoted + '\n')
        self.assertEqual(self.env_map(), {'OTHER': '1', 'HERMES_SUBSCRIBER_FALLBACK_1_KEY': KEY2})

    def test_missing_section_is_appended_and_crlf_kept(self):
        src = CONFIG.replace('fallback_providers: []\n', '').replace('\n', '\r\n')
        self.config.write_bytes(src.encode('utf-8'))
        self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
        raw = self.config.read_bytes().decode('utf-8')
        self.assertTrue(raw.startswith(src))
        self.assertNotIn('\n', raw.replace('\r\n', ''), 'mixed line endings')
        self.assertEqual(len(self.parsed()['fallback_providers']), 1)

    def test_user_entries_are_kept_and_block_list_extended(self):
        src = CONFIG.replace('fallback_providers: []\n',
                             'fallback_providers:\n'
                             '- provider: openrouter\n'
                             '  model: deepseek/deepseek-chat\n'
                             '# a comment that belongs to display\n')
        self.config.write_text(src, encoding='utf-8', newline='')
        result = self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
        self.assertEqual(result['results'][0]['status'], 'added')
        chain = self.parsed()['fallback_providers']
        self.assertEqual(chain[0], {'provider': 'openrouter', 'model': 'deepseek/deepseek-chat'})
        self.assertEqual(chain[1]['base_url'], 'https://dahl.example/v1')
        self.assertIn('# a comment that belongs to display\ndisplay:', self.config.read_text(encoding='utf-8'))

    # --- failed live check: nothing written --------------------------------------
    def test_failed_check_writes_nothing_and_maps_codes(self):
        before = (self.config.read_bytes(), self.env.read_bytes())
        probe = Probe(fail={'https://a.example/v1': Failure('AUTH', 'x ' + KEY1),
                            'https://b.example/v1': Failure('QUOTA', 'y')})
        result = self.run_stage([entry('a', 'https://a.example/v1', KEY1), entry('b', 'https://b.example/v1', KEY2)], probe)
        self.assertEqual([r['status'] for r in result['results']], ['auth', 'quota'])
        self.assertFalse(result['ok'])
        self.assertEqual((self.config.read_bytes(), self.env.read_bytes()), before)
        self.assertEqual(self.protected, [])
        for code, status in (('NETWORK', 'network'), ('VERIFY', 'verify'), ('CONFIG', 'failed')):
            probe = Probe(fail={'https://a.example/v1': Failure(code, 'z')})
            self.assertEqual(self.run_stage([entry('a', 'https://a.example/v1', KEY1)], probe)['results'][0]['status'], status)
        crash = Probe(fail={'https://a.example/v1': RuntimeError('boom ' + KEY1)})
        self.assertEqual(self.run_stage([entry('a', 'https://a.example/v1', KEY1)], crash)['results'][0]['status'], 'failed')
        self.assertEqual((self.config.read_bytes(), self.env.read_bytes()), before)

    def test_one_good_one_bad_writes_only_the_good_one(self):
        probe = Probe(fail={'https://b.example/v1': Failure('NETWORK', 'n')})
        result = self.run_stage([entry('a', 'https://a.example/v1', KEY1), entry('b', 'https://b.example/v1', KEY2)], probe)
        self.assertEqual([r['status'] for r in result['results']], ['added', 'network'])
        self.assertEqual([e['base_url'] for e in self.parsed()['fallback_providers']], ['https://a.example/v1'])
        self.assertNotIn('HERMES_SUBSCRIBER_FALLBACK_2_KEY', self.env_map())

    def test_malformed_entries_primary_and_duplicates_are_never_probed(self):
        probe = Probe()
        bad = [entry('x', 'http://insecure.example/v1', KEY1), entry('x', 'https://ok.example/v1', 'short'),
               entry('bad id!', 'https://ok.example/v1', KEY1), entry('p', PRIMARY + '/', KEY1), 'not-a-dict']
        for item in bad:
            self.assertEqual(self.run_stage([item], probe)['results'][0]['status'], 'failed')
        self.assertEqual(probe.calls, [])
        dup = self.run_stage([entry('a', 'https://a.example/v1', KEY1), entry('b', 'https://A.example/v1/', KEY2)], probe)
        self.assertEqual([r['status'] for r in dup['results']], ['added', 'failed'])
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(self.run_stage([entry('a', 'https://a.example/v1', KEY1)] * 3, probe)['results'],
                         [{'provider_id': 'a', 'status': 'failed'}] * 2)

    # --- idempotency ----------------------------------------------------------------
    def test_rerun_is_idempotent(self):
        entries = [entry('dahl', 'https://dahl.example/v1', KEY1), entry('atria', 'https://api.atria.example/v1', KEY2)]
        self.run_stage(entries, Probe())
        snapshot = (self.config.read_bytes(), self.env.read_bytes())
        self.protected.clear()
        probe = Probe()
        result = self.run_stage(entries, probe)
        self.assertEqual([r['status'] for r in result['results']], ['exists', 'exists'])
        self.assertTrue(result['ok'])
        self.assertEqual(probe.calls, [], 'an existing backup must not spend quota again')
        self.assertEqual((self.config.read_bytes(), self.env.read_bytes()), snapshot)
        self.assertEqual(self.protected, [])

    def test_foreign_slot_value_is_never_overwritten(self):
        self.env.write_text(ENV + "HERMES_SUBSCRIBER_FALLBACK_1_KEY='users-own-value-1'\n", encoding='utf-8', newline='')
        self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
        env = self.env_map()
        self.assertEqual(env['HERMES_SUBSCRIBER_FALLBACK_1_KEY'], 'users-own-value-1')
        self.assertEqual(env['HERMES_SUBSCRIBER_FALLBACK_2_KEY'], KEY1)
        self.assertEqual(self.parsed()['fallback_providers'][0]['key_env'], 'HERMES_SUBSCRIBER_FALLBACK_2_KEY')

    # --- rollback ----------------------------------------------------------------------
    def test_config_write_failure_rolls_back_env(self):
        before = (self.config.read_bytes(), self.env.read_bytes())
        real = fb.atomic_write

        def flaky(path, data, secret=False):
            if path.name == 'config.yaml' and not getattr(flaky, 'rolled', False):
                flaky.rolled = True
                raise OSError('disk full')
            return real(path, data, secret)
        fb.atomic_write = flaky
        result = self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual((self.config.read_bytes(), self.env.read_bytes()), before)

    def test_verification_mismatch_rolls_back_both_files(self):
        before = (self.config.read_bytes(), self.env.read_bytes())
        real = fb.atomic_write

        def corrupting(path, data, secret=False):
            if path.name == 'config.yaml' and b'dahl.example' in data:
                data = data.replace(b'busy_input_mode: steer', b'busy_input_mode: interrupt')
            return real(path, data, secret)
        fb.atomic_write = corrupting
        result = self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual((self.config.read_bytes(), self.env.read_bytes()), before)

    def test_protect_failure_writes_nothing(self):
        before = (self.config.read_bytes(), self.env.read_bytes())

        def deny(path):
            raise Failure('FALLBACK', 'acl')
        fb.protect = deny
        result = self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual((self.config.read_bytes(), self.env.read_bytes()), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['.env', 'config.yaml'])

    def test_concurrent_edit_during_check_aborts(self):
        def editing_probe(base, key, model):
            self.config.write_text(CONFIG + 'extra: 1\n', encoding='utf-8', newline='')
            return model
        result = self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], editing_probe)
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertNotIn('HERMES_SUBSCRIBER_FALLBACK_1_KEY', self.env_map())
        self.assertNotIn('dahl.example', self.config.read_text(encoding='utf-8'))

    def test_non_list_chain_and_bad_config_fail_closed(self):
        for text in ('fallback_providers: {provider: x}\n', '- just\n- a list\n', 'a: [unclosed\n'):
            self.config.write_text(text, encoding='utf-8', newline='')
            result = self.run_stage([entry('dahl', 'https://dahl.example/v1', KEY1)], Probe())
            self.assertEqual(result['results'][0]['status'], 'failed')
            self.assertEqual(self.config.read_text(encoding='utf-8'), text)

    def test_empty_request_is_a_noop(self):
        self.assertEqual(self.run_stage([], Probe()), {'ok': True, 'results': []})
        self.assertEqual(self.run_stage(None, Probe()), {'ok': True, 'results': []})

    # --- process boundary --------------------------------------------------------------
    def test_cli_prints_one_json_line_without_the_key(self):
        """Real entry point, no mock: a refused local port answers NETWORK fast."""
        env = dict(os.environ, NO_PROXY='*', no_proxy='*', PYTHONUTF8='1')
        for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
            env.pop(name, None)
        payload = json.dumps({'fallbacks': [entry('dahl', 'https://127.0.0.1:9/v1', KEY1)]})
        run = subprocess.run([sys.executable, str(BACKEND / 'fallbacks.py'), str(self.home)], input=payload,
                             capture_output=True, text=True, encoding='utf-8', timeout=120, env=env)
        self.assertNotIn(KEY1, run.stdout + run.stderr)
        lines = [line for line in run.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, run.stdout)
        self.assertEqual(json.loads(lines[0]), {'ok': False, 'results': [{'provider_id': 'dahl', 'status': 'network'}]})
        self.assertNotIn('FALLBACK', self.env.read_text(encoding='utf-8'))

    def test_cli_garbage_input_yields_fixed_envelope(self):
        run = subprocess.run([sys.executable, str(BACKEND / 'fallbacks.py'), str(self.home)], input='not json ' + KEY1,
                             capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertNotIn(KEY1, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout.strip()), {'ok': False, 'results': []})
        self.assertEqual(run.returncode, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
