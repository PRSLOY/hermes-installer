"""Offline extras tests: no network, no PowerShell, no host Hermes writes.

The MCP probe is injected so reachability never touches the wire, and
default_soul is loaded from a throwaway fake repo in a temporary directory.
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
sys.path.insert(0, str(BACKEND))
spec = importlib.util.spec_from_file_location('extras', BACKEND / 'extras.py')
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)

DEFAULT_SOUL = 'upstream default soul text'
INSTALLER_SOUL = 'installer soul text'
LEGACY_SOUL = '# legacy scaffold\n'

ALL_SERVERS = ('wolfram', 'microsoft-learn', 'context7', 'deepwiki')


def purge_hermes():
    for name in list(sys.modules):
        if name == 'hermes_cli' or name.startswith('hermes_cli.'):
            del sys.modules[name]


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        purge_hermes()
        self.tmp = tempfile.TemporaryDirectory(prefix='extras-test-')
        self.base = Path(self.tmp.name)
        package = self.base / 'repo' / 'hermes_cli'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'default_soul.py').write_text(
            'DEFAULT_SOUL_MD = ' + repr(DEFAULT_SOUL) + '\n'
            'def is_legacy_template_soul(text):\n'
            '    return text.strip() == ' + repr(LEGACY_SOUL.strip()) + '\n',
            encoding='utf-8')
        self.assets = self.base / 'assets'
        (self.assets / 'skills' / 'start').mkdir(parents=True)
        (self.assets / 'SOUL.md').write_text(INSTALLER_SOUL, encoding='utf-8')
        (self.assets / 'skills' / 'start' / 'SKILL.md').write_text('skill body', encoding='utf-8')
        self.home = self.base / 'home'
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()
        purge_hermes()

    @property
    def repo(self):
        return self.base / 'repo'

    def run_extras(self, probe):
        return e.main(self.home, self.repo, self.assets, probe=probe)

    def write_soul(self, text):
        (self.home / 'SOUL.md').write_text(text, encoding='utf-8')

    def read_soul(self):
        return (self.home / 'SOUL.md').read_text(encoding='utf-8')

    def write_config(self, text):
        (self.home / 'config.yaml').write_text(text, encoding='utf-8')

    def config_bytes(self):
        path = self.home / 'config.yaml'
        return path.read_bytes() if path.exists() else None

    def test_missing_soul_and_skill_installed(self):
        result = self.run_extras(lambda url: False)
        self.assertIs(result['ok'], True)
        self.assertEqual(result['soul'], 'installed')
        self.assertEqual(self.read_soul(), INSTALLER_SOUL)
        self.assertEqual(result['skills'], 'installed')
        self.assertEqual((self.home / 'skills' / 'start' / 'SKILL.md').read_text(encoding='utf-8'), 'skill body')

    def test_default_soul_replaced(self):
        self.write_soul(DEFAULT_SOUL)
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['soul'], 'installed')
        self.assertEqual(self.read_soul(), INSTALLER_SOUL)

    def test_legacy_soul_replaced(self):
        self.write_soul(LEGACY_SOUL)
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['soul'], 'installed')
        self.assertEqual(self.read_soul(), INSTALLER_SOUL)

    def test_custom_soul_kept(self):
        self.write_soul('my own soul')
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['soul'], 'kept')
        self.assertEqual(self.read_soul(), 'my own soul')

    def test_existing_skill_kept(self):
        target = self.home / 'skills' / 'start'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('user version', encoding='utf-8')
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['skills'], 'kept')
        self.assertEqual((target / 'SKILL.md').read_text(encoding='utf-8'), 'user version')

    def test_all_shipped_skills_installed_each_only_if_absent(self):
        # The package now ships several skills (start, gamedev-start, sites-start,
        # telegram-connect); each is copied only when the user has no such folder.
        (self.assets / 'skills' / 'gamedev-start').mkdir()
        (self.assets / 'skills' / 'gamedev-start' / 'SKILL.md').write_text('game', encoding='utf-8')
        mine = self.home / 'skills' / 'start'
        mine.mkdir(parents=True)
        (mine / 'SKILL.md').write_text('user version', encoding='utf-8')
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['skills'], 'installed')
        self.assertEqual((mine / 'SKILL.md').read_text(encoding='utf-8'), 'user version')
        self.assertEqual((self.home / 'skills' / 'gamedev-start' / 'SKILL.md').read_text(encoding='utf-8'), 'game')

    def test_busy_mode_interrupt_becomes_steer(self):
        # Live Telegram 2026-09-23: with 'interrupt' every "ты тут?" killed a long
        # transcription and the gateway auto-resumed it, so the task restarted in
        # a loop. 'steer' injects the message after the next tool call instead.
        import yaml
        self.write_config('model:\n  default: m\ndisplay:\n  compact: false\n  busy_input_mode: interrupt\n  busy_ack_detail: true\n')
        result = self.run_extras(lambda url: False)
        parsed = yaml.safe_load(self.config_bytes().decode('utf-8'))
        self.assertEqual(result['busy'], 'set')
        self.assertEqual(parsed['display'], {'compact': False, 'busy_input_mode': 'steer', 'busy_ack_detail': True})
        self.assertEqual(parsed['model'], {'default': 'm'})

    def test_stt_language_en_becomes_ru(self):
        # Live Telegram 2026-09-23: Hermes' default stt.language "en" forced Russian
        # voice notes into English ("Tell me, are you crazy?").
        import yaml
        self.write_config('model:\n  default: m\nstt:\n  enabled: true\n  local:\n    model: "base"\n  language: "en"               # GLOBAL hint\n')
        result = self.run_extras(lambda url: False)
        parsed = yaml.safe_load(self.config_bytes().decode('utf-8'))
        self.assertEqual(result['stt_language'], 'set')
        self.assertEqual(parsed['stt'], {'enabled': True, 'local': {'model': 'base'}, 'language': 'ru'})

    def test_stt_language_user_choice_kept(self):
        self.write_config('model:\n  default: m\nstt:\n  language: "de"\n')
        before = self.config_bytes()
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['stt_language'], 'kept')
        self.assertEqual(self.config_bytes(), before)

    def test_busy_mode_user_choice_kept(self):
        self.write_config('model:\n  default: m\ndisplay:\n  busy_input_mode: queue\n')
        before = self.config_bytes()
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['busy'], 'kept')
        self.assertEqual(self.config_bytes(), before)

    def test_unreachable_mcp_not_added(self):
        self.write_config('model:\n  default: m\n')
        before = self.config_bytes()
        result = self.run_extras(lambda url: False)
        self.assertEqual([result['mcp'][name] for name in ALL_SERVERS], ['unreachable'] * 4)
        self.assertEqual(self.config_bytes(), before)

    def test_existing_mcp_untouched_byte_for_byte(self):
        block = 'mcp_servers:\n' + ''.join(
            '  %s:\n    url: https://%s/mcp\n    connect_timeout: 60\n    enabled: true\n' % (n, n)
            for n in ALL_SERVERS)
        self.write_config('model:\n  default: m\n' + block)
        before = self.config_bytes()
        result = self.run_extras(lambda url: True)
        self.assertEqual([result['mcp'][name] for name in ALL_SERVERS], ['exists'] * 4)
        self.assertEqual(self.config_bytes(), before)

    def test_ok_reflects_failed_steps_but_not_unreachable(self):
        # Two independent reviews: ok was always True, so worker.ps1 could never
        # show its "set incomplete" line. 'unreachable'/'kept'/'exists' are
        # deliberate skips, not failures.
        self.write_config('model:\n  default: m\n')
        self.assertIs(self.run_extras(lambda url: False)['ok'], True)
        (Path(self.assets) / 'SOUL.md').unlink()
        (Path(self.home) / 'SOUL.md').unlink(missing_ok=True)
        result = self.run_extras(lambda url: False)
        self.assertEqual(result['soul'], 'failed')
        self.assertIs(result['ok'], False)

    def test_concurrent_config_change_is_not_overwritten(self):
        # configure.py aborts when config.yaml changes under it; extras must not
        # write a candidate built from stale bytes over someone else's edit.
        self.write_config('model:\n  default: m\n')
        edited = b'model:\n  default: m\nedited_meanwhile: true\n'
        def probe(url):
            (Path(self.home) / 'config.yaml').write_bytes(edited)
            return True
        result = self.run_extras(probe)
        self.assertEqual(self.config_bytes(), edited)
        self.assertTrue(all(result['mcp'][name] == 'failed' for name in ALL_SERVERS))

    def test_quoted_mcp_key_keeps_user_servers(self):
        # Security audit 2026-09-22: startswith('mcp_servers:') missed a quoted or
        # spaced key, appended a second mcp_servers block, and yaml.safe_load kept
        # only the last one: the user's own server (with its token) vanished while
        # the step reported 'added'.
        import yaml
        for key in ('"mcp_servers":', "'mcp_servers':", 'mcp_servers :'):
            with self.subTest(key=key):
                self.write_config('model:\n  default: m\n' + key + '\n  mine:\n    url: https://mine.test/mcp\n    headers:\n      Authorization: Bearer t0k\n')
                result = self.run_extras(lambda url: True)
                parsed = yaml.safe_load(self.config_bytes().decode('utf-8'))
                self.assertEqual(parsed['mcp_servers']['mine']['headers']['Authorization'], 'Bearer t0k')
                self.assertEqual([result['mcp'][name] for name in ALL_SERVERS], ['added'] * 4)

    def test_lost_existing_server_triggers_rollback(self):
        # Whatever the merge does, an existing entry must never disappear silently.
        self.write_config('model:\n  default: m\nmcp_servers:\n  mine:\n    url: https://mine.test/mcp\n')
        before = self.config_bytes()
        original = e.merge_mcp_text
        # Additions are written correctly; only the user's server is dropped.
        e.merge_mcp_text = lambda src, servers, additions: 'model:\n  default: m\nmcp_servers:\n' + e.server_block(additions)
        try:
            result = self.run_extras(lambda url: True)
        finally:
            e.merge_mcp_text = original
        self.assertEqual(self.config_bytes(), before)
        self.assertTrue(all(result['mcp'][name] == 'failed' for name in ALL_SERVERS))

    def test_reachable_mcp_added(self):
        self.write_config('model:\n  default: m\n')
        result = self.run_extras(lambda url: True)
        self.assertEqual([result['mcp'][name] for name in ALL_SERVERS], ['added'] * 4)
        import yaml
        parsed = yaml.safe_load(self.config_bytes().decode('utf-8'))
        self.assertEqual(parsed['model'], {'default': 'm'})
        self.assertEqual(parsed['mcp_servers']['wolfram'],
                         {'url': 'https://agenttools.wolfram.com/mcp', 'connect_timeout': 60, 'enabled': True})

    def test_config_write_failure_rolls_back(self):
        self.write_config('model:\n  default: m\n')
        before = self.config_bytes()
        real = e.atomic_write
        state = {'calls': 0}

        def flaky(path, data, secret=False):
            if Path(path).name == 'config.yaml':
                state['calls'] += 1
                if state['calls'] == 1:
                    raise OSError('simulated disk failure')
            return real(path, data, secret=secret)

        e.atomic_write = flaky
        try:
            result = self.run_extras(lambda url: True)
        finally:
            e.atomic_write = real
        self.assertEqual([result['mcp'][name] for name in ALL_SERVERS], ['failed'] * 4)
        self.assertEqual(self.config_bytes(), before)

    def test_second_run_idempotent(self):
        self.write_config('model:\n  default: m\n')
        first = self.run_extras(lambda url: True)
        self.assertEqual([first['mcp'][name] for name in ALL_SERVERS], ['added'] * 4)
        after_first = self.config_bytes()
        second = self.run_extras(lambda url: True)
        self.assertEqual([second['mcp'][name] for name in ALL_SERVERS], ['exists'] * 4)
        self.assertEqual(second['soul'], 'kept')
        self.assertEqual(second['skills'], 'kept')
        self.assertEqual(self.config_bytes(), after_first)

class ApiRetriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='retries-test-')
        self.home = Path(self.tmp.name)
        self.cfg = self.home / 'config.yaml'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        self.cfg.write_bytes(text.encode('utf-8'))

    def fake_config_set(self, value=7):
        def run():
            import yaml
            data = yaml.safe_load(self.cfg.read_text(encoding='utf-8')) or {}
            data.setdefault('agent', {})
            if data['agent'] is None:
                data['agent'] = {}
            data['agent']['api_max_retries'] = value
            self.cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
        return run

    def test_unset_becomes_seven(self):
        self.write('model:\n  default: glm-5.3\nagent:\n  max_turns: 90\n  # api_max_retries: 3\n')
        self.assertEqual(e.step_api_retries(self.home, self.home, runner=self.fake_config_set()), 'set')
        import yaml
        data = yaml.safe_load(self.cfg.read_text(encoding='utf-8'))
        self.assertEqual(data['agent'], {'max_turns': 90, 'api_max_retries': 7})
        self.assertEqual(data['model'], {'default': 'glm-5.3'})

    def test_user_value_kept(self):
        self.write('agent:\n  api_max_retries: 2\n')
        before = self.cfg.read_bytes()
        called = []
        self.assertEqual(e.step_api_retries(self.home, self.home, runner=lambda: called.append(1)), 'kept')
        self.assertEqual(called, [])
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_wrong_write_rolls_back(self):
        self.write('agent:\n  max_turns: 90\n')
        before = self.cfg.read_bytes()
        self.assertEqual(e.step_api_retries(self.home, self.home, runner=self.fake_config_set(value=3)), 'failed')
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_not_a_hermes_checkout_is_skipped(self):
        self.write('agent:\n  max_turns: 90\n')
        self.assertEqual(e.step_api_retries(self.home, self.home), 'kept')


if __name__ == '__main__':
    unittest.main(verbosity=2)
