"""Maintenance mode: a second run of the installer is never a dead end.

Offline real worker Main (as test_retry): the vendor install, the provider API, the
live Hermes check, the MCP probes and Telegram are doubles; configure.py, fallbacks.py,
extras.py (--status and the set itself), checkpoint.ps1 and the DPAPI journal are real.
Everything happens under a temporary LOCALAPPDATA, never the host's Hermes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
PS = str(Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
sys.path.insert(0, str(BACKEND))
import extras  # noqa: E402

HARNESS = r'''
param($Backend, $Root)
. (Join-Path $Backend 'worker.ps1')
$env:LOCALAPPDATA=$Root
if ($env:MAINT_HERMES_HOME) { $env:HERMES_HOME=$env:MAINT_HERMES_HOME } else { $env:HERMES_HOME='' }
function Run-Child($Exe, $Arguments, $InputText='', $Seconds=1200, [switch]$StreamStages) {
    if ($StreamStages) {
        [IO.File]::AppendAllText((Join-Path $Root 'calls.txt'), "install`n")
        $h=Join-Path $Root 'hermes'; $r=Join-Path $h 'hermes-agent'
        foreach ($p in @('bin\hermes.exe','hermes-agent\venv\Scripts\python.exe','hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe','hermes-agent\apps\desktop\release\win-unpacked\resources\app.asar','hermes-agent\apps\desktop\release\win-unpacked\icudtl.dat')) {
            $f=Join-Path $h $p; [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($f)); [IO.File]::WriteAllText($f,'MZ-double')
        }
        [void][IO.Directory]::CreateDirectory((Join-Path $r '.git'))
        [IO.File]::WriteAllText((Join-Path $r '.git\HEAD'), (Get-Content (Join-Path $Backend 'upstream\commit.txt') -Raw).Trim())
        Copy-Item (Join-Path $Backend 'upstream\cli-config.yaml.example') (Join-Path $r 'cli-config.yaml.example')
        Copy-Item (Join-Path $r 'cli-config.yaml.example') (Join-Path $h 'config.yaml')
        $lines=@('{"protocol_version":1,"ok":true}')
        foreach ($s in @('node','desktop','dependencies','repository','venv')) { $lines += (@{stage=$s;ok=$true;skipped=$false}|ConvertTo-Json -Compress) }
        return @{Code=0;Text=($lines -join "`n")}
    }
    $name=[IO.Path]::GetFileName([string]$Arguments[0])
    $rest=@($Arguments | Select-Object -Skip 1)
    $flag=@($rest | Where-Object { ([string]$_).StartsWith('--') })
    [IO.File]::AppendAllText((Join-Path $Root 'calls.txt'), (($name + ' ' + ($flag -join ' ')).Trim() + "`n"))
    $text = $InputText | & $env:MAINT_PYTHON (Join-Path $Root 'double.py') $Backend $name @rest
    return @{Code=$LASTEXITCODE;Text=($text -join "`n")}
}
exit (Main)
'''

DOUBLE = r'''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
backend, name, args = sys.argv[1], sys.argv[2], sys.argv[3:]
sys.path.insert(0, backend)
from provider import Failure
mode = os.environ.get('MAINT_MODE', 'OK')
stdin = sys.stdin.read()
if name == 'extras.py':
    import extras
    if args[3:4] == ['--status']:
        print(json.dumps(extras.status_report(Path(args[0])))); sys.exit(0)
    if args[3:4] == ['--marketplaces']:
        print(json.dumps({'ok': True, 'marketplaces': 'exists', 'wildberries': 'exists'})); sys.exit(0)
    # The set itself, offline: MCP probes answer "unreachable"; the test's own assets.
    print(json.dumps(extras.main(Path(args[0]), Path(args[1]), Path(os.environ['MAINT_ASSETS']), probe=lambda url: False))); sys.exit(0)
if name == 'configure.py':
    import configure
    data = json.loads(stdin)
    def select(base, key, model):
        if mode in ('AUTH', 'QUOTA', 'NETWORK'):
            raise Failure(mode, 'offline API double')
        Path(os.environ['MAINT_ROOT'], 'api.txt').write_text(base + ' ' + (model or '-'), encoding='utf-8')
        return model or 'double-model'
    def verify(*a):
        if mode == 'VERIFY':
            raise Failure('VERIFY', 'offline Hermes double')
    real = configure.atomic_write
    def flaky(path, data, secret=False):
        if mode == 'WRITEFAIL' and Path(path).name == 'config.yaml':
            raise OSError('simulated disk failure')
        if mode == 'KILL' and Path(path).name == 'config.yaml':
            print(json.dumps({'ok': False, 'code': 'VERIFY', 'message': 'убит'}), flush=True)
            os._exit(9)   # the process dies between the two writes
        return real(path, data, secret=secret)
    try:
        with patch.object(configure, 'select_model', select), patch.object(configure, 'verify_with_hermes', verify), patch.object(configure, 'atomic_write', flaky):
            out = configure.main(Path(args[0]), Path(args[1]), data, change='--change' in args)
        print(json.dumps(out)); sys.exit(0)
    except Failure as e:
        print(json.dumps({'ok': False, 'code': e.code, 'message': str(e)})); sys.exit(1)
if name == 'fallbacks.py':
    import fallbacks
    data = json.loads(stdin)
    def probe(base, key, model):
        if 'rejected' in key:
            raise Failure('AUTH', 'offline API double')
        return model or 'fb-model'
    print(json.dumps(fallbacks.main(Path(args[0]), data['fallbacks'], probe=probe, replace=data.get('replace') is True))); sys.exit(0)
if name == 'telegram.py':
    if args[2:3] == ['pending']:
        print(json.dumps({'ok': True, 'status': 'none', 'requests': []})); sys.exit(0)
    print(json.dumps({'ok': True, 'status': 'connected', 'bot': 'maint_test_bot', 'autostart': 'task'})); sys.exit(0)
sys.exit(3)
'''

PIN = (BACKEND / 'upstream/commit.txt').read_text().strip()
KEY = 'initial-fake-key-1'
NEW_KEY = 'changed-fake-key-2'
TOKEN = '123456789:' + 'A' * 35


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='subscriber-maint-')
        self.root = Path(self.tmp.name)
        (self.root / 'harness.ps1').write_text(HARNESS, encoding='utf-8-sig')
        (self.root / 'double.py').write_text(DOUBLE, encoding='utf-8')
        self.assets = self.root / 'assets'
        (self.assets / 'skills' / 'start').mkdir(parents=True)
        (self.assets / 'SOUL.md').write_text('installer soul', encoding='utf-8')
        (self.assets / 'skills' / 'start' / 'SKILL.md').write_text('start v1', encoding='utf-8')
        self.home = self.root / 'hermes'

    def tearDown(self):
        self.tmp.cleanup()

    def run_worker(self, payload, mode='OK', hermes_home=None):
        env = {**os.environ, 'MAINT_PYTHON': sys.executable, 'MAINT_MODE': mode, 'MAINT_ASSETS': str(self.assets),
               'MAINT_ROOT': str(self.root), 'MAINT_HERMES_HOME': hermes_home or 'unset'}
        if not hermes_home:
            env.pop('MAINT_HERMES_HOME')
        p = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(self.root / 'harness.ps1'),
                            str(BACKEND), str(self.root)], input=json.dumps(payload), capture_output=True, text=True,
                           encoding='utf-8', timeout=180, env=env)
        self.assertFalse(p.stderr, p.stderr)
        for secret in (KEY, NEW_KEY, TOKEN, 'backup-fake-key'):
            self.assertNotIn(secret, p.stdout, 'a secret reached the protocol stream')
        return [json.loads(line) for line in p.stdout.splitlines()]

    def calls(self):
        path = self.root / 'calls.txt'
        return path.read_text().splitlines() if path.exists() else []

    def install(self):
        events = self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY})
        self.assertEqual(events[-1]['type'], 'success', events[-1])
        (self.root / 'calls.txt').unlink()
        (self.root / 'api.txt').unlink()

    def status(self, **kw):
        final = self.run_worker({'protocol': 1, 'action': 'status'}, **kw)[-1]
        self.assertEqual(final['type'], 'status', final)
        self.assertEqual(set(final), {'type', 'message', 'state', 'launch_path', 'set_supported', 'set_version', 'package_set_version',
                                      'model_ours', 'base_url', 'model', 'telegram', 'backups', 'change_interrupted'})
        return final

    def journal(self, change=None):
        """Read (and optionally mutate + rewrite) the DPAPI journal through checkpoint.ps1 itself."""
        cmd = (f". '{BACKEND / 'worker.ps1'}'; $h=Assert-SafePath '{self.home}'; $r=Assert-SafePath '{self.home / 'hermes-agent'}'; "
               f"$s=Read-Journal $h $r '{PIN}' ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value) -AnyRevision; "
               + (change + '; Write-Journal $s; ' if change else '') + '$s | ConvertTo-Json -Compress')
        done = subprocess.run([PS, '-NoProfile', '-Command', cmd], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def files(self):
        return {str(f.relative_to(self.home)): f.read_bytes() for f in self.home.rglob('*') if f.is_file()}

    def foreign_home(self, python=True):
        (self.home / 'hermes-agent' / 'venv' / 'Scripts').mkdir(parents=True)
        if python:
            (self.home / 'hermes-agent' / 'venv' / 'Scripts' / 'python.exe').write_text('MZ-double')
        (self.home / 'config.yaml').write_text('model:\n  default: their-model\n  provider: openrouter\n  api_key: sk-their-own\n', encoding='utf-8')
        (self.home / '.env').write_text('OPENROUTER_API_KEY=their-secret\n', encoding='utf-8')

    # --- status: the four states ---------------------------------------------------------
    def test_status_none_on_a_fresh_computer(self):
        final = self.status()
        self.assertEqual(final['state'], 'none')
        self.assertIs(final['set_supported'], False)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.home.exists(), 'status must never create the home')

    def test_status_ours_incomplete_after_a_rejected_key(self):
        self.assertEqual(self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY}, mode='AUTH')[-1]['code'], 'AUTH')
        final = self.status()
        self.assertEqual(final['state'], 'ours_incomplete')
        self.assertEqual((final['base_url'], final['model']), ('', ''))

    def test_status_ours_completed_reports_provider_without_secrets(self):
        self.install()
        final = self.status()
        self.assertEqual(final['state'], 'ours_completed')
        self.assertEqual((final['base_url'], final['model'], final['model_ours']), ('https://example.invalid/v1', 'double-model', True))
        self.assertEqual((final['telegram'], final['backups'], final['set_supported']), (False, 0, True))
        self.assertEqual(final['set_version'], extras.SET_VERSION, 'the install records the set version')
        self.assertEqual(final['package_set_version'], extras.SET_VERSION)
        self.assertTrue(final['launch_path'].endswith('Hermes.exe'))
        self.assertEqual(self.calls(), ['extras.py --status'])

    def test_status_reads_an_install_made_by_an_older_package(self):
        """Finding 1: another package pin is not a foreign journal; maintenance still works."""
        self.install()
        self.journal("$s.revision='" + '0' * 40 + "'")
        self.assertEqual(self.status()['state'], 'ours_completed')
        final = self.run_worker({'protocol': 1, 'action': 'telegram_pending'})[-1]
        self.assertEqual((final['type'], final['status']), ('telegram', 'none'), final)

    def test_status_foreign_home_custom_profile_and_no_python(self):
        self.foreign_home()
        final = self.status()
        self.assertEqual((final['state'], final['set_supported']), ('foreign', True))
        self.assertEqual((final['base_url'], final['model'], final['model_ours']), ('', '', False), 'no provider details of a foreign Hermes')
        shutil.rmtree(self.home / 'hermes-agent')
        final = self.status()
        self.assertEqual((final['state'], final['set_supported']), ('foreign', False))
        custom = self.root / 'elsewhere'
        custom.mkdir()
        final = self.status(hermes_home=str(custom))
        self.assertEqual((final['state'], final['set_supported']), ('foreign', False))
        self.assertIn('HERMES_HOME', final['message'])

    # --- update_set / foreign_add_set -----------------------------------------------------
    def test_update_set_replaces_untouched_skill_keeps_edited_one(self):
        self.install()
        (self.assets / 'skills' / 'mine').mkdir()
        (self.assets / 'skills' / 'mine' / 'SKILL.md').write_text('mine v1', encoding='utf-8')
        final = self.run_worker({'protocol': 1, 'action': 'update_set'})[-1]
        self.assertEqual((final['type'], final['status']), ('done', 'ok'), final)
        (self.home / 'skills' / 'mine' / 'SKILL.md').write_text('edited by the user', encoding='utf-8')
        # The package ships new versions of both skills.
        (self.assets / 'skills' / 'start' / 'SKILL.md').write_text('start v2', encoding='utf-8')
        (self.assets / 'skills' / 'mine' / 'SKILL.md').write_text('mine v2', encoding='utf-8')
        config = (self.home / 'config.yaml').read_bytes()
        env = (self.home / '.env').read_bytes()
        final = self.run_worker({'protocol': 1, 'action': 'update_set'})[-1]
        self.assertEqual(final['status'], 'ok', final)
        self.assertEqual((self.home / 'skills' / 'start' / 'SKILL.md').read_text(encoding='utf-8'), 'start v2')
        self.assertEqual((self.home / 'skills' / 'mine' / 'SKILL.md').read_text(encoding='utf-8'), 'edited by the user')
        self.assertEqual((self.home / '.env').read_bytes(), env)
        self.assertEqual((self.home / 'config.yaml').read_bytes(), config, 'a repeated set run is idempotent for config.yaml')
        self.assertEqual(self.calls()[-2:], ['extras.py', 'extras.py --marketplaces'])
        self.assertEqual(self.journal()['phase'], 'completed')

    def test_update_set_refused_for_a_foreign_hermes(self):
        self.foreign_home()
        final = self.run_worker({'protocol': 1, 'action': 'update_set'})[-1]
        self.assertEqual(final['code'], 'CONFIG')
        self.assertEqual(self.calls(), [])

    def test_foreign_add_set_never_touches_model_env_or_journal(self):
        self.foreign_home()
        before_env = (self.home / '.env').read_bytes()
        before_model = yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8'))['model']
        final = self.run_worker({'protocol': 1, 'action': 'foreign_add_set'})[-1]
        self.assertEqual((final['type'], final['status']), ('done', 'ok'), final)
        self.assertEqual((self.home / '.env').read_bytes(), before_env)
        self.assertEqual(yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8'))['model'], before_model)
        self.assertFalse((self.root / 'hermes.subscriber-checkpoint.json').exists(), 'no journal for a Hermes we did not install')
        self.assertEqual((self.home / 'skills' / 'start' / 'SKILL.md').read_text(encoding='utf-8'), 'start v1')
        self.assertEqual(self.status()['set_version'], extras.SET_VERSION)

    def test_foreign_add_set_refused_for_our_install(self):
        self.install()
        self.assertEqual(self.run_worker({'protocol': 1, 'action': 'foreign_add_set'})[-1]['code'], 'CONFIG')

    # --- change_provider -----------------------------------------------------------------
    def change(self, mode='OK', endpoint='https://changed.invalid/api/v1', model='new-model'):
        return self.run_worker({'protocol': 1, 'action': 'change_provider', 'endpoint': endpoint, 'api_key': NEW_KEY, 'model': model}, mode=mode)[-1]

    def test_change_provider_replaces_our_block_and_key_only(self):
        self.install()
        with open(self.home / '.env', 'a', encoding='utf-8') as fh:
            fh.write('TELEGRAM_BOT_TOKEN=' + TOKEN + '\n')
        other = {k: v for k, v in yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8')).items() if k != 'model'}
        final = self.change()
        self.assertEqual((final['type'], final['status']), ('done', 'ok'), final)
        cfg = yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8'))
        self.assertEqual(cfg['model'], {'default': 'new-model', 'provider': 'custom', 'base_url': 'https://changed.invalid/api/v1',
                                        'api_key': '${HERMES_SUBSCRIBER_API_KEY}', 'api_mode': 'chat_completions', 'max_tokens': 3000})
        self.assertEqual({k: v for k, v in cfg.items() if k != 'model'}, other, 'nothing but the model block changed')
        env = dotenv_values(self.home / '.env')
        self.assertEqual(env['HERMES_SUBSCRIBER_API_KEY'], NEW_KEY)
        self.assertEqual(env['TELEGRAM_BOT_TOKEN'], TOKEN)
        self.assertEqual(list(env), ['HERMES_SUBSCRIBER_API_KEY', 'TELEGRAM_BOT_TOKEN'], 'the key line is replaced in place')
        self.assertEqual(self.status()['base_url'], 'https://changed.invalid/api/v1')

    def test_change_provider_failures_leave_both_files_byte_exact(self):
        """A rejected key, a failed live check and a failed write (rollback) change nothing."""
        self.install()
        before = {name: (self.home / name).read_bytes() for name in ('config.yaml', '.env')}
        for mode, code in (('AUTH', 'AUTH'), ('VERIFY', 'VERIFY'), ('WRITEFAIL', 'CONFIG')):
            with self.subTest(mode=mode):
                final = self.change(mode=mode)
                self.assertEqual((final['type'], final['code']), ('error', code), final)
                self.assertEqual({name: (self.home / name).read_bytes() for name in before}, before)
        self.assertEqual(self.journal()['phase'], 'completed')

    def test_killed_change_is_detected_and_can_be_saved_again(self):
        """Killed between the .env and config.yaml writes: the next status says so."""
        self.install()
        self.assertIs(self.status()['change_interrupted'], False)
        final = self.change(mode='KILL')
        self.assertEqual(final['type'], 'error', final)
        self.assertEqual(dotenv_values(self.home / '.env')['HERMES_SUBSCRIBER_API_KEY'], NEW_KEY, 'the kill landed between the writes')
        self.assertEqual(yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8'))['model']['base_url'], 'https://example.invalid/v1')
        status = self.status()
        self.assertIs(status['change_interrupted'], True)
        self.assertIs(status['model_ours'], True, '«Сменить провайдера» stays available')
        self.assertEqual(self.change()['status'], 'ok')
        self.assertIs(self.status()['change_interrupted'], False)
        self.assertEqual(yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8'))['model']['base_url'], 'https://changed.invalid/api/v1')

    def test_install_announces_configured_before_the_optional_steps(self):
        events = self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY})
        kinds = [e['type'] for e in events]
        self.assertEqual(kinds.count('configured'), 1)
        configured = events[kinds.index('configured')]
        self.assertEqual(set(configured), {'type', 'message', 'launch_path'})
        self.assertTrue(configured['launch_path'].endswith('Hermes.exe'))
        set_step = next(i for i, e in enumerate(events) if 'Настраиваю набор' in e.get('message', ''))
        self.assertLess(kinds.index('configured'), set_step, 'announced before the optional set')
        self.assertEqual(kinds[-1], 'success')

    def test_change_provider_refuses_a_model_block_that_is_not_ours(self):
        self.install()
        cfg = (self.home / 'config.yaml').read_text(encoding='utf-8')
        edited = cfg.replace("\"${HERMES_SUBSCRIBER_API_KEY}\"", 'sk-user-own-key')
        self.assertNotEqual(cfg, edited)
        (self.home / 'config.yaml').write_text(edited, encoding='utf-8')
        before = self.files()
        final = self.change()
        self.assertEqual(final['code'], 'CONFIG')
        self.assertFalse((self.root / 'api.txt').exists(), 'refused before any billable API call')
        self.assertEqual(self.files(), before)
        self.assertIs(self.status()['model_ours'], False)

    def test_change_provider_refused_for_a_foreign_hermes(self):
        self.foreign_home()
        before = self.files()
        self.assertEqual(self.change()['code'], 'CONFIG')
        self.assertEqual(self.files(), before)
        self.assertEqual(self.calls(), [])

    def test_change_provider_input_is_validated_like_an_install(self):
        self.install()
        final = self.run_worker({'protocol': 1, 'action': 'change_provider', 'endpoint': 'http://plain.invalid/v1', 'api_key': NEW_KEY})[-1]
        self.assertEqual(final['code'], 'INPUT')
        final = self.run_worker({'protocol': 1, 'action': 'change_provider', 'endpoint': 'https://x.invalid/v1', 'api_key': NEW_KEY, 'telegram_bot_token': TOKEN})[-1]
        self.assertEqual(final['code'], 'INPUT', 'a provider change carries no other secret')

    # --- add_backups / telegram_connect ---------------------------------------------------
    def test_add_backups_adds_then_replaces_ours(self):
        self.install()
        entry = {'provider_id': 'dahl', 'endpoint': 'https://dahl.invalid/v1', 'model': 'm-1', 'api_key': 'backup-fake-key-1'}
        final = self.run_worker({'protocol': 1, 'action': 'add_backups', 'fallbacks': [entry]})[-1]
        self.assertEqual((final['type'], final['status']), ('done', 'ok'), final)
        self.assertEqual(dotenv_values(self.home / '.env')['HERMES_SUBSCRIBER_FALLBACK_1_KEY'], 'backup-fake-key-1')
        final = self.run_worker({'protocol': 1, 'action': 'add_backups', 'fallbacks': [dict(entry, api_key='backup-fake-key-2', model='m-2')]})[-1]
        self.assertEqual(final['status'], 'ok', final)
        cfg = yaml.safe_load((self.home / 'config.yaml').read_text(encoding='utf-8'))
        self.assertEqual(cfg['fallback_providers'], [{'provider': 'custom', 'model': 'm-2', 'base_url': 'https://dahl.invalid/v1',
                                                      'api_mode': 'chat_completions', 'key_env': 'HERMES_SUBSCRIBER_FALLBACK_1_KEY'}])
        self.assertEqual(dotenv_values(self.home / '.env')['HERMES_SUBSCRIBER_FALLBACK_1_KEY'], 'backup-fake-key-2')
        self.assertEqual(self.status()['backups'], 1)
        final = self.run_worker({'protocol': 1, 'action': 'add_backups', 'fallbacks': [dict(entry, api_key='rejected-backup-fake-key')]})[-1]
        self.assertEqual(final['status'], 'partial')
        self.assertIn('ключ отклонён', final['message'])
        self.assertEqual(dotenv_values(self.home / '.env')['HERMES_SUBSCRIBER_FALLBACK_1_KEY'], 'backup-fake-key-2')
        self.assertEqual(self.run_worker({'protocol': 1, 'action': 'add_backups', 'fallbacks': []})[-1]['code'], 'INPUT')

    def test_telegram_connect_on_installed_hermes(self):
        self.install()
        final = self.run_worker({'protocol': 1, 'action': 'telegram_connect', 'telegram_bot_token': TOKEN})[-1]
        self.assertEqual((final['type'], final['status'], final['telegram_bot']), ('done', 'ok', 'maint_test_bot'), final)
        self.assertEqual(self.run_worker({'protocol': 1, 'action': 'telegram_connect'})[-1]['code'], 'INPUT')

    def test_maintenance_actions_reject_unknown_fields(self):
        for payload in ({'protocol': 1, 'action': 'status', 'api_key': KEY}, {'protocol': 1, 'action': 'update_set', 'endpoint': 'https://x.invalid/v1'}):
            self.assertEqual(self.run_worker(payload)[-1]['code'], 'INPUT')
        self.assertEqual(self.calls(), [])

    # --- unfinished installs: park and reinstall instead of a dead end --------------------
    def test_unfinished_install_of_an_older_package_is_parked_and_reinstalled(self):
        """Finding 1: key never accepted on v0.1.2, v0.1.3 opened: a clean install, not CONFIG."""
        self.assertEqual(self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY}, mode='AUTH')[-1]['code'], 'AUTH')
        self.journal("$s.revision='" + '0' * 40 + "'")
        final = self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY})[-1]
        self.assertEqual(final['type'], 'success', final)
        self.assertEqual(self.journal()['revision'], PIN)
        self.assertEqual(len([p for p in self.root.iterdir() if p.name.startswith('hermes.subscriber-preserved-')]), 1)

    def test_deleted_home_is_a_fresh_install(self):
        """Finding 2: the user deleted %LOCALAPPDATA%\\hermes to start over; the journal lives on."""
        for completed in (False, True):
            with self.subTest(completed=completed):
                if completed:
                    self.install()
                else:
                    self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY}, mode='AUTH')
                shutil.rmtree(self.home)
                self.assertEqual(self.status()['state'], 'none')
                final = self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY})[-1]
                self.assertEqual(final['type'], 'success', final)
                self.assertEqual(self.journal()['phase'], 'completed')
                shutil.rmtree(self.home)
                (self.root / 'hermes.subscriber-checkpoint.json').unlink()

    def test_completed_install_points_to_the_maintenance_screen(self):
        self.install()
        final = self.run_worker({'protocol': 1, 'action': 'install', 'endpoint': 'https://example.invalid/v1', 'api_key': KEY})[-1]
        self.assertEqual(final['code'], 'CONFIG')
        self.assertIn('обновить набор', final['message'])


class CheckpointMaintainTests(unittest.TestCase):
    def test_maintain_authorization_needs_a_completed_journal(self):
        with tempfile.TemporaryDirectory(prefix='subscriber-auth-') as tmp:
            home = Path(tmp) / 'hermes'
            repo = home / 'hermes-agent'
            repo.mkdir(parents=True)
            def authorize(*extra):
                return subprocess.run([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(BACKEND / 'checkpoint.ps1'),
                                       str(home), str(repo), *extra], capture_output=True, text=True, timeout=30)
            self.assertEqual(authorize('maintain').returncode, 1, 'no journal')
            for phase, maintain in (('configuring', 1), ('completed', 0)):
                cmd = (f". '{BACKEND / 'worker.ps1'}'; $s=[pscustomobject]@{{schema=1;owner='HermesSubscriberSetup';sid=([Security.Principal.WindowsIdentity]::GetCurrent().User.Value);"
                       f"home=(Assert-SafePath '{home}');repo=(Assert-SafePath '{repo}');revision='{'1' * 40}';phase='{phase}';fingerprints=(Journal-Fingerprints '{home}' '{repo}')}}; "
                       f"if (Test-Path -LiteralPath (Journal-Path $s.home)) {{ Write-Journal $s }} else {{ Write-Journal $s -New }}")
                made = subprocess.run([PS, '-NoProfile', '-Command', cmd], capture_output=True, text=True, timeout=30)
                self.assertEqual(made.returncode, 0, made.stderr)
                self.assertEqual(authorize('maintain').returncode, maintain, phase + ' maintain')
                # The install authorization keeps its exact pin: an older package's journal is not 'configuring' for us.
                self.assertEqual(authorize().returncode, 1, phase + ' plain (other pin)')
            self.assertEqual(authorize('bogus').returncode, 1)


class EnvironmentHardeningTests(unittest.TestCase):
    def test_uv_env_drops_index_config_and_ca_overrides(self):
        names = ('UV_INDEX_URL', 'UV_INDEX', 'UV_INDEX_MYCORP_USERNAME', 'UV_EXTRA_INDEX_URL', 'UV_DEFAULT_INDEX',
                 'UV_INSECURE_HOST', 'UV_CONFIG_FILE', 'SSL_CERT_FILE', 'UV_FIND_LINKS')
        saved = {n: os.environ.get(n) for n in names + ('UV_NATIVE_TLS',)}
        try:
            for n in names:
                os.environ[n] = 'https://evil.invalid/simple'
            os.environ['UV_NATIVE_TLS'] = '1'
            env = extras.uv_env()
            self.assertFalse([n for n in names if n in env])
            self.assertEqual(env.get('UV_NATIVE_TLS'), '1', 'the Windows store stays allowed')
        finally:
            for n, v in saved.items():
                if v is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = v
        src = (BACKEND / 'extras.py').read_text(encoding='utf-8')
        self.assertIn("[uv, 'sync', '--frozen', '--no-config'", src)

    def test_configure_forgets_the_key_after_the_live_check(self):
        import configure
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='configure-env-') as tmp:
            os.environ.pop(configure.KEY_NAME, None)
            before_home = os.environ.get('HERMES_HOME')
            seen = {}
            def check(repo, base, key, model):
                seen['key'] = os.environ.get(configure.KEY_NAME)
            # protect() is patched: on Python >= 3.12 tempfile gives the sandbox a descriptor that
            # Set-Acl cannot rewrite without SeSecurityPrivilege (not this test's subject).
            with patch.object(configure, 'run_agent_check', check), patch.object(configure, 'protect', lambda path: None):
                configure.verify_with_hermes(Path(tmp), Path(tmp), {'default': 'm'}, 'https://p.invalid/v1', 'env-fake-key-1', 'm')
            self.assertEqual(seen['key'], 'env-fake-key-1', 'the agent still sees the key during the check')
            self.assertNotIn(configure.KEY_NAME, os.environ, 'later children must not inherit the key')
            self.assertEqual(os.environ.get('HERMES_HOME'), before_home)

    def ps(self, script):
        run = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, encoding='utf-8', timeout=60)
        return run

    def test_certificate_failures_get_the_antivirus_clock_advice(self):
        for reason in ('npm ERR! code SELF_SIGNED_CERT_IN_CHAIN self signed certificate in certificate chain',
                       'unable to get local issuer certificate', 'CERT_HAS_EXPIRED'):
            event = json.dumps({'stage': 'node-deps', 'ok': False, 'reason': reason}).replace("'", "''")
            run = self.ps(f". '{BACKEND / 'worker.ps1'}'; try {{ Check-InstallResult @{{Code=1;Text='{event}'}} }} catch {{ [Console]::WriteLine($_.Exception.Message) }}")
            self.assertIn('Антивирус проверяет защищённые соединения или сбиты дата и время', run.stdout, reason)
            self.assertNotIn(reason, run.stdout, 'the raw reason is never echoed')

    def test_links_above_localappdata_are_the_profiles_own(self):
        with tempfile.TemporaryDirectory(prefix='safe-path-') as tmp:
            real = Path(tmp) / 'D-drive-profile'
            (real / 'Local' / 'hermes' / 'real-bin').mkdir(parents=True)
            link = Path(tmp) / 'AppData'
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            inner = real / 'Local' / 'hermes' / 'bin'
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(inner), str(real / 'Local' / 'hermes' / 'real-bin')], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            try:
                local = link / 'Local'
                script = (f"$env:LOCALAPPDATA='{local}'; . '{BACKEND / 'worker.ps1'}'; "
                          f"try {{ Assert-SafePath '{local / 'hermes' / 'config.yaml'}' | Out-Null; [Console]::WriteLine('above-ok') }} catch {{ [Console]::WriteLine('above-refused') }}; "
                          f"try {{ Assert-SafePath '{local / 'hermes' / 'bin' / 'hermes.exe'}' | Out-Null; [Console]::WriteLine('below-ok') }} catch {{ [Console]::WriteLine('below-refused') }}; "
                          f"$env:LOCALAPPDATA='{Path(tmp) / 'elsewhere'}'; "
                          f"try {{ Assert-SafePath '{local / 'hermes' / 'config.yaml'}' | Out-Null; [Console]::WriteLine('custom-ok') }} catch {{ [Console]::WriteLine('custom-refused') }}")
                out = self.ps(script).stdout.split()
                self.assertEqual(out, ['above-ok', 'below-refused', 'custom-refused'])
            finally:
                os.rmdir(inner)
                os.rmdir(link)

    def test_vc_elevation_is_bounded(self):
        text = (BACKEND / 'worker.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('Wait-Job -Job $job -Timeout ($InstallSeconds + $PromptSeconds)', text)
        self.assertIn('может мигать на панели задач', text)
        self.assertIn('«Одобрить»', text)


class FallbackReplaceTests(unittest.TestCase):
    """«Запасные ключи» on an installed Hermes: only OUR chain entries may take a new key."""
    def test_only_our_entry_is_replaced(self):
        import fallbacks
        with tempfile.TemporaryDirectory(prefix='fallback-replace-') as tmp:
            home = Path(tmp)
            (home / 'config.yaml').write_text(
                'model:\n  default: m\n  base_url: https://primary.invalid/v1\nfallback_providers:\n'
                '- provider: custom\n  model: old\n  base_url: https://ours.invalid/v1\n  api_mode: chat_completions\n  key_env: HERMES_SUBSCRIBER_FALLBACK_1_KEY\n'
                '- provider: custom\n  model: theirs\n  base_url: https://theirs.invalid/v1\n  key_env: THEIR_KEY\n', encoding='utf-8')
            (home / '.env').write_text("THEIR_KEY=their-secret-1\nHERMES_SUBSCRIBER_FALLBACK_1_KEY='old-backup-key'\n", encoding='utf-8')
            probed = []
            def probe(base, key, model):
                probed.append(base)
                return model or 'probed'
            entries = [{'provider_id': 'ours', 'endpoint': 'https://ours.invalid/v1', 'model': 'new', 'api_key': 'new-backup-key'},
                       {'provider_id': 'theirs', 'endpoint': 'https://theirs.invalid/v1', 'model': 'x', 'api_key': 'not-their-key'}]
            self.assertEqual([r['status'] for r in fallbacks.main(home, entries, probe=probe)['results']], ['exists', 'exists'],
                             'without replace nothing existing is touched (install behaviour)')
            result = fallbacks.main(home, entries, probe=probe, replace=True)
            self.assertEqual([r['status'] for r in result['results']], ['replaced', 'exists'])
            self.assertEqual(probed, ['https://ours.invalid/v1'], 'the new key is checked live before it is written')
            env = dotenv_values(home / '.env')
            self.assertEqual((env['HERMES_SUBSCRIBER_FALLBACK_1_KEY'], env['THEIR_KEY']), ('new-backup-key', 'their-secret-1'))
            chain = yaml.safe_load((home / 'config.yaml').read_text(encoding='utf-8'))['fallback_providers']
            self.assertEqual([e['model'] for e in chain], ['new', 'theirs'])
            again = fallbacks.main(home, entries[:1], probe=probe, replace=True)
            self.assertEqual(again['results'][0]['status'], 'exists', 'same key and model: nothing to do')
            self.assertEqual(len(probed), 1)


class ExtrasOwnershipTests(unittest.TestCase):
    """What the set may replace on an existing install (Python level, no PowerShell)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='extras-owned-')
        base = Path(self.tmp.name)
        self.home, self.assets, self.repo = base / 'home', base / 'assets', base / 'repo'
        self.home.mkdir()
        (self.assets / 'skills').mkdir(parents=True)
        (self.repo / 'hermes_cli').mkdir(parents=True)
        (self.repo / 'hermes_cli' / '__init__.py').write_text('', encoding='utf-8')
        (self.repo / 'hermes_cli' / 'default_soul.py').write_text("DEFAULT_SOUL_MD = 'default'\ndef is_legacy_template_soul(t):\n    return False\n", encoding='utf-8')
        (self.assets / 'SOUL.md').write_bytes(b'soul v1\r\n')
        self.ship('a', 'a v1')
        self.ship('b', 'b v1')

    def tearDown(self):
        for name in list(sys.modules):
            if name == 'hermes_cli' or name.startswith('hermes_cli.'):
                del sys.modules[name]
        self.tmp.cleanup()

    def ship(self, name, text):
        (self.assets / 'skills' / name).mkdir(exist_ok=True)
        (self.assets / 'skills' / name / 'SKILL.md').write_text(text, encoding='utf-8')

    def run_set(self):
        return extras.main(self.home, self.repo, self.assets, probe=lambda url: False)

    def skill(self, name):
        path = self.home / 'skills' / name / 'SKILL.md'
        return path.read_text(encoding='utf-8') if path.exists() else None

    def test_marker_records_version_and_what_the_set_owns(self):
        result = self.run_set()
        self.assertTrue(result['ok'], result)
        marker = json.loads((self.home / extras.SET_MARKER).read_text(encoding='utf-8'))
        self.assertEqual(marker['set_version'], extras.SET_VERSION)
        self.assertEqual(set(marker['skills']), {'a', 'b'})
        self.assertEqual(marker['skills']['a'], extras.tree_digest(self.assets / 'skills' / 'a'))
        self.assertEqual(marker['soul'], extras.content_hash(b'soul v1\n'), 'CRLF is folded: an autocrlf package matches')

    def test_untouched_is_updated_edited_kept_deleted_not_restored(self):
        self.run_set()
        (self.home / 'skills' / 'b' / 'SKILL.md').write_text('my edit', encoding='utf-8')
        (self.home / 'skills' / 'b' / '__pycache__').mkdir()
        shutil.rmtree(self.home / 'skills' / 'a')
        self.ship('a', 'a v2')
        self.ship('b', 'b v2')
        self.ship('c', 'c v1')
        (self.assets / 'SOUL.md').write_bytes(b'soul v2\r\n')
        result = self.run_set()
        self.assertTrue(result['ok'], result)
        self.assertIsNone(self.skill('a'), 'a skill the user removed is not brought back')
        self.assertEqual(self.skill('b'), 'my edit')
        self.assertEqual(self.skill('c'), 'c v1', 'a new skill is added')
        self.assertEqual((self.home / 'SOUL.md').read_bytes(), b'soul v2\r\n', 'an untouched SOUL of ours is updated')
        (self.home / 'SOUL.md').write_text('my soul', encoding='utf-8')
        (self.assets / 'SOUL.md').write_bytes(b'soul v3\r\n')
        self.run_set()
        self.assertEqual((self.home / 'SOUL.md').read_text(encoding='utf-8'), 'my soul')

    def test_runtime_byproducts_do_not_make_a_skill_the_users(self):
        self.run_set()
        cache = self.home / 'skills' / 'a' / 'scripts' / '__pycache__'
        cache.mkdir(parents=True)
        (cache / 'x.cpython-311.pyc').write_bytes(b'\0')
        self.ship('a', 'a v2')
        self.run_set()
        self.assertEqual(self.skill('a'), 'a v2')

    def test_install_without_marker_uses_shipped_history(self):
        """An install older than the marker: a copy equal to ANY shipped version is ours."""
        target = self.home / 'skills' / 'a'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_bytes(b'a v0\r\n')
        other = self.home / 'skills' / 'b'
        other.mkdir(parents=True)
        (other / 'SKILL.md').write_text('users own b', encoding='utf-8')
        (self.assets / 'skills' / extras.HISTORY).write_text(json.dumps({'schema': 1, 'soul': [], 'skills': {
            'a': [extras.digest_rows({'SKILL.md': extras.content_hash(b'a v0\n')})]}}), encoding='utf-8')
        self.run_set()
        self.assertEqual(self.skill('a'), 'a v1')
        self.assertEqual(self.skill('b'), 'users own b')

    def test_second_run_changes_nothing(self):
        self.run_set()
        snapshot = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.run_set()
        self.assertEqual((result['soul'], result['skills']), ('kept', 'kept'))
        self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, snapshot)

    def test_locked_replacement_keeps_old_copy_and_version(self):
        self.run_set()
        self.ship('a', 'a v2')
        real = extras.replace_tree
        def locked(*args):
            raise PermissionError('in use')
        extras.replace_tree = locked
        try:
            result = self.run_set()
        finally:
            extras.replace_tree = real
        self.assertEqual(result['skills'], 'failed')
        self.assertEqual(self.skill('a'), 'a v1')
        marker = json.loads((self.home / extras.SET_MARKER).read_text(encoding='utf-8'))
        self.assertEqual(marker['set_version'], extras.SET_VERSION, 'the version recorded by the first complete run stays')
        (self.home / extras.SET_MARKER).write_text(json.dumps(dict(marker, set_version='0.1.2')), encoding='utf-8')
        extras.replace_tree = locked
        try:
            self.run_set()
        finally:
            extras.replace_tree = real
        self.assertEqual(json.loads((self.home / extras.SET_MARKER).read_text(encoding='utf-8'))['set_version'], '0.1.2',
                         'an incomplete update keeps offering «Обновить набор»')

    def test_junction_skill_is_never_replaced(self):
        self.run_set()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'SKILL.md').write_text('a v1', encoding='utf-8')
        shutil.rmtree(self.home / 'skills' / 'a')
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.home / 'skills' / 'a'), str(outside)], capture_output=True)
        self.assertEqual(made.returncode, 0, made.stderr)
        try:
            self.ship('a', 'a v2')
            self.run_set()
            self.assertEqual((outside / 'SKILL.md').read_text(encoding='utf-8'), 'a v1')
        finally:
            os.rmdir(self.home / 'skills' / 'a')

    def test_status_report_has_no_secrets(self):
        (self.home / 'config.yaml').write_text('model:\n  default: m\n  provider: custom\n  base_url: https://p.invalid/v1\n  api_key: ${HERMES_SUBSCRIBER_API_KEY}\n'
                                               'fallback_providers:\n- provider: custom\n  base_url: https://b.invalid/v1\n', encoding='utf-8')
        (self.home / '.env').write_text("HERMES_SUBSCRIBER_API_KEY='status-secret-1'\nTELEGRAM_BOT_TOKEN=" + TOKEN + '\n', encoding='utf-8')
        report = extras.status_report(self.home)
        self.assertEqual(report, {'ok': True, 'set_version': '', 'package_set_version': extras.SET_VERSION, 'model_ours': True,
                                  'base_url': 'https://p.invalid/v1', 'model': 'm', 'telegram': True, 'backups': 1,
                                  'change_interrupted': False})
        self.assertNotIn('status-secret-1', json.dumps(report))
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_shipped_history_is_readable_and_shipped_with_the_skills(self):
        # Generated once (tools/shipped_history.py) for installs older than the marker; it lives
        # in assets/skills/, which package-dist.ps1 copies as a whole.
        history = extras.read_history(ROOT / 'assets')
        self.assertTrue(history['soul'])
        self.assertIn('start', history['skills'])
        self.assertTrue(all(len(d) == 64 for digests in history['skills'].values() for d in digests))


if __name__ == '__main__':
    unittest.main(verbosity=2)
