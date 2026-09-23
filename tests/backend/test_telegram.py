"""Offline Telegram stage tests: no network, no processes, no host Hermes writes.

getMe, dependency install, gateway enable and the connected-wait are injected;
protect() (a PowerShell ACL call) is replaced by a no-op so nothing is spawned.
Every token below is a fake built at runtime, never a real credential.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
sys.path.insert(0, str(BACKEND))
spec = importlib.util.spec_from_file_location('telegram_stage', BACKEND / 'telegram.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

TOKEN = '1234567' + ':' + 'A' * 20 + 'b_c-d' * 3        # fake, shape-valid
OTHER = '7654321' + ':' + 'Z' * 35                      # fake, shape-valid


class Recorder:
    def __init__(self, result=None, exc=None):
        self.calls, self.result, self.exc = [], result, exc

    def __call__(self, *args):
        self.calls.append(args)
        if self.exc:
            raise self.exc
        return self.result


class TelegramStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telegram-test-')
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.repo = Path(self.tmp.name) / 'repo'
        self.env = self.home / '.env'
        self._protect = t.protect
        self._atomic = t.atomic_write
        self.protected = []
        t.protect = lambda path: self.protected.append(Path(path).name)
        self.probe = Recorder(('ok', 'my_hermes_bot'))
        self.deps = Recorder()
        self.gateway = Recorder(True)
        self.connected = Recorder(True)
        self.autostart = Recorder('task')

    def tearDown(self):
        t.protect = self._protect
        t.atomic_write = self._atomic
        self.tmp.cleanup()

    def run_stage(self, token=TOKEN):
        result = t.main(self.home, self.repo, token, probe=self.probe, deps=self.deps,
                        gateway=self.gateway, connected=self.connected, clock=lambda: 1000.0,
                        autostart=self.autostart)
        if isinstance(token, str) and token:
            self.assertNotIn(token, json.dumps(result), 'token leaked into the report')
        return result

    def env_bytes(self):
        return self.env.read_bytes() if self.env.exists() else None

    # --- input / probe -------------------------------------------------------
    def test_malformed_token_skips_everything(self):
        for bad in ('', 'abc', '123:short', TOKEN + ' ', 'x' + TOKEN, None, 12345):
            self.assertEqual(self.run_stage(bad), {'ok': False, 'status': 'token'})
        self.assertEqual(self.probe.calls, [])
        self.assertIsNone(self.env_bytes())

    def test_rejected_token_writes_nothing(self):
        self.probe.result = ('token', None)
        self.assertEqual(self.run_stage(), {'ok': False, 'status': 'token'})
        self.assertEqual((self.deps.calls, self.gateway.calls), ([], []))
        self.assertIsNone(self.env_bytes())

    def test_network_failure_and_probe_exception_write_nothing(self):
        self.probe.result = ('network', None)
        self.assertEqual(self.run_stage()['status'], 'network')
        self.probe.exc = RuntimeError('boom ' + TOKEN)
        self.assertEqual(self.run_stage()['status'], 'network')
        self.assertIsNone(self.env_bytes())

    # --- happy path / idempotence ---------------------------------------------
    def test_fresh_install_writes_protected_env_and_enables_gateway(self):
        result = self.run_stage()
        self.assertEqual(result, {'ok': True, 'status': 'connected', 'bot': 'my_hermes_bot', 'autostart': 'task'})
        self.assertEqual(self.env_bytes(), ('TELEGRAM_BOT_TOKEN=' + TOKEN + '\n').encode())
        self.assertEqual(len(self.protected), 1, 'the .env temp file must be ACL-protected before the write')
        self.assertEqual(self.gateway.calls, [(self.home, self.repo, False)])
        self.assertEqual(self.connected.calls, [(self.home, 999.0)])
        self.assertEqual([p.name for p in self.home.iterdir()], ['.env'], 'temp files left behind')

    def test_existing_env_is_preserved_and_appended(self):
        before = b"HERMES_SUBSCRIBER_API_KEY='k\\'ey'\r\nOTHER=1"
        self.env.write_bytes(before)
        self.assertTrue(self.run_stage()['ok'])
        self.assertEqual(self.env_bytes(), before + b'\nTELEGRAM_BOT_TOKEN=' + TOKEN.encode() + b'\n')

    def test_rerun_same_token_is_idempotent(self):
        self.assertTrue(self.run_stage()['ok'])
        first = self.env_bytes()
        self.protected.clear()
        self.assertTrue(self.run_stage()['ok'])
        self.assertEqual(self.env_bytes(), first)
        self.assertEqual(self.protected, [], 'nothing may be rewritten on a re-run')
        self.assertEqual(self.gateway.calls[-1], (self.home, self.repo, False))
        self.assertEqual(self.connected.calls[-1], (self.home, 0), 're-run accepts an already-connected gateway')

    def test_running_gateway_is_restarted_after_new_token(self):
        (self.home / 'gateway.pid').write_text('{}', encoding='utf-8')
        self.assertTrue(self.run_stage()['ok'])
        self.assertEqual(self.gateway.calls, [(self.home, self.repo, True)])

    # --- conflicts: never touch the user's own setup ---------------------------
    def test_other_bot_is_kept_byte_for_byte(self):
        before = ('TELEGRAM_BOT_TOKEN=' + OTHER + '\n').encode()
        self.env.write_bytes(before)
        self.assertEqual(self.run_stage(), {'ok': False, 'status': 'exists'})
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual((self.deps.calls, self.gateway.calls), ([], []))

    def test_allow_all_switch_refuses(self):
        for key in ('TELEGRAM_ALLOW_ALL_USERS', 'GATEWAY_ALLOW_ALL_USERS'):
            before = (key + '=true\n').encode()
            self.env.write_bytes(before)
            self.assertEqual(self.run_stage()['status'], 'open')
            self.assertEqual(self.env_bytes(), before)

    def test_installer_never_sets_access_switches(self):
        self.run_stage()
        text = self.env.read_text(encoding='utf-8')
        for key in ('ALLOW_ALL', 'ALLOWED_USERS'):
            self.assertNotIn(key, text)

    # --- failures and rollback -----------------------------------------------
    def test_readback_mismatch_rolls_back_byte_exact(self):
        for before in (b'KEEP=1\r\n# comment\n', None):
            if before is None:
                self.env.unlink(missing_ok=True)
            else:
                self.env.write_bytes(before)
            real = self._atomic
            calls = []

            def corrupting(path, data, secret=False):
                calls.append(data)
                real(path, b'TELEGRAM_BOT_TOKEN=' + OTHER.encode() + b'\n' if len(calls) == 1 else data, secret)
            t.atomic_write = corrupting
            self.assertEqual(self.run_stage()['status'], 'failed')
            t.atomic_write = real
            self.assertEqual(self.env_bytes(), before)
            self.assertEqual(self.gateway.calls, [])

    def test_protect_failure_leaves_env_untouched(self):
        before = b'KEEP=1\n'
        self.env.write_bytes(before)

        def deny(path):
            raise t.Failure('TELEGRAM', 'x')
        t.protect = deny
        self.assertEqual(self.run_stage()['status'], 'failed')
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['.env'])

    def test_dependency_failure_writes_nothing(self):
        self.deps.exc = RuntimeError('pip said ' + TOKEN)
        self.assertEqual(self.run_stage(), {'ok': False, 'status': 'failed'})
        self.assertIsNone(self.env_bytes())

    def test_gateway_failure_keeps_verified_token_and_reports_saved(self):
        for gateway in (Recorder(False), Recorder(exc=OSError('spawn ' + TOKEN))):
            self.gateway = gateway
            self.assertEqual(self.run_stage(), {'ok': False, 'status': 'saved', 'bot': 'my_hermes_bot'})
        self.assertIn(TOKEN.encode(), self.env_bytes())

    def test_not_connected_in_time_reports_saved(self):
        self.connected.result = False
        self.assertEqual(self.run_stage()['status'], 'saved')


class ProbeAndWaitTests(unittest.TestCase):
    def setUp(self):
        self._build = t.urllib.request.build_opener

    def tearDown(self):
        t.urllib.request.build_opener = self._build

    def fake_opener(self, body=None, exc=None):
        seen = {}

        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): self.close()

        class Opener:
            def open(self, request, timeout=None):
                seen['url'], seen['timeout'] = request.full_url, timeout
                if exc:
                    raise exc
                return Response(json.dumps(body).encode())

        def build(*handlers):
            seen['handlers'] = handlers
            return Opener()
        t.urllib.request.build_opener = build
        return seen

    def test_getme_ok_bounded_and_no_redirect(self):
        seen = self.fake_opener({'ok': True, 'result': {'is_bot': True, 'username': 'my_hermes_bot'}})
        self.assertEqual(t.probe_getme(TOKEN), ('ok', 'my_hermes_bot'))
        self.assertEqual(seen['url'], 'https://api.telegram.org/bot' + TOKEN + '/getMe')
        self.assertEqual(seen['timeout'], t.GETME_TIMEOUT)
        self.assertTrue(any(isinstance(h, t.NoRedirect) for h in seen['handlers']))
        self.assertIsNone(t.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test/'))

    def test_getme_outcomes(self):
        self.fake_opener(exc=urllib.error.HTTPError('u', 401, 'x', {}, None))
        self.assertEqual(t.probe_getme(TOKEN), ('token', None))
        self.fake_opener(exc=urllib.error.HTTPError('u', 502, 'x', {}, None))
        self.assertEqual(t.probe_getme(TOKEN), ('network', None))
        self.fake_opener(exc=urllib.error.URLError('down'))
        self.assertEqual(t.probe_getme(TOKEN), ('network', None))
        self.fake_opener({'ok': True, 'result': {'is_bot': False}})
        self.assertEqual(t.probe_getme(TOKEN), ('token', None))
        self.fake_opener({'ok': True, 'result': {'is_bot': True, 'username': 'bad name<script>'}})
        self.assertEqual(t.probe_getme(TOKEN), ('ok', None), 'untrusted username must be dropped')

    def test_wait_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / 'gateway_state.json'
            now = [0.0]
            clock = lambda: now[0]
            def sleep(seconds): now[0] += seconds
            self.assertFalse(t.wait_connected(home, 0, timeout=5, sleep=sleep, clock=clock), 'no state file')
            state.write_text(json.dumps({'platforms': {'telegram': {'state': 'retrying'}}}), encoding='utf-8')
            now[0] = 0.0
            self.assertFalse(t.wait_connected(home, 0, timeout=5, sleep=sleep, clock=clock))
            state.write_text(json.dumps({'platforms': {'telegram': {'state': 'connected'}}}), encoding='utf-8')
            now[0] = 0.0
            self.assertTrue(t.wait_connected(home, 0, timeout=5, sleep=sleep, clock=clock))
            now[0] = 0.0
            future = state.stat().st_mtime + 3600
            self.assertFalse(t.wait_connected(home, future, timeout=5, sleep=sleep, clock=clock),
                             'a state written before this run must not count')

    def test_gateway_env_is_non_interactive_and_profile_free(self):
        os.environ['HERMES_PROFILE'] = 'other'
        try:
            env = t.gateway_env(Path('C:/h'))
        finally:
            del os.environ['HERMES_PROFILE']
        self.assertNotIn('HERMES_PROFILE', env)
        for key in ('HERMES_NONINTERACTIVE', 'HERMES_GATEWAY_INSTALL_START_NOW', 'HERMES_GATEWAY_INSTALL_START_ON_LOGIN'):
            self.assertEqual(env[key], '1')
        self.assertEqual(env['HERMES_HOME'], str(Path('C:/h')))
        self.assertFalse(any(TOKEN in v for v in env.values()))


class WorkerWiringTests(unittest.TestCase):
    """Static checks of worker.ps1 (read as text; nothing is executed)."""
    def setUp(self):
        self.text = (BACKEND / 'worker.ps1').read_text(encoding='utf-8-sig')

    def test_token_removed_before_configure_and_passed_only_on_stdin(self):
        remove = self.text.index("Properties.Remove('telegram_bot_token')")
        self.assertLess(remove, self.text.index("'configure.py'"))
        call = [l for l in self.text.splitlines() if "'telegram.py'" in l and 'telegram_bot_token' in l]
        self.assertEqual(len(call), 1)
        for line in self.text.splitlines():
            if "'telegram.py'" in line:
                self.assertNotIn('Token', line.split('@((')[1].split(') ')[0], 'no secret on a command line')
        self.assertIn('(@{telegram_bot_token=$telegramToken} | ConvertTo-Json -Compress)', call[0])
        args = call[0].split('@((')[1].split(') (')[0]
        self.assertNotIn('telegramToken', args, 'token must never be a command-line argument')

    def test_telegram_step_is_after_extras_and_before_success(self):
        extras = self.text.index("'extras.py'")
        telegram = self.text.index("telegram_bot_token=$telegramToken")
        success = self.text.index("type='success'")
        self.assertLess(extras, telegram)
        self.assertLess(telegram, success)

    def test_every_status_has_fixed_russian_text(self):
        for status in ('connected', 'token', 'network', 'exists', 'open', 'saved', 'failed'):
            self.assertRegex(self.text, r"'" + status + r"'\s+=\s+[\"'][^\"']*[А-Яа-я]")
        self.assertIn('Напишите вашему боту', self.text)
        self.assertIn('он ответит', self.text)

    def test_package_ships_the_helper(self):
        self.assertIn("'telegram.py'", (ROOT / 'package-dist.ps1').read_text(encoding='utf-8-sig'))

    def test_telegram_actions_are_strict_and_journal_gated(self):
        self.assertIn("@('telegram_pending','telegram_approve')", self.text)
        self.assertIn("'^[0-9a-f]{16}$'", self.text)
        self.assertIn("$state.phase -ne 'completed'", self.text)
        self.assertIn("Send-Event @{type='telegram'", self.text)
        for status in ('pending', 'none', 'done', 'approved', 'approved_partial', 'expired', 'off'):
            self.assertRegex(self.text, r"'" + status + r"'\s+=\s+'[^']*[А-Яа-я]")
        self.assertIn('Пока сообщений нет — напишите боту и нажмите «Проверить» ещё раз.', self.text)

    def test_success_carries_bot_only_when_connected_and_autostart_is_checked(self):
        self.assertIn("$successEvent['telegram_bot'] = $telegramBot", self.text)
        self.assertIn("[string]$tgReply.autostart -cin @('task','startup')", self.text)
        self.assertIn('автозапуск после перезагрузки не настроен', self.text)


class VcRuntimeWiringTests(unittest.TestCase):
    """Static checks of the VC++ runtime step in worker.ps1 (read as text; nothing runs)."""
    def setUp(self):
        self.text = (BACKEND / 'worker.ps1').read_text(encoding='utf-8-sig')

    def test_signer_pattern_requires_exact_microsoft_organisation(self):
        import re
        pattern = re.search(r"Subject -cmatch '([^']+)'", self.text).group(1)
        good = 'CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US'
        self.assertTrue(re.search(pattern, good))
        for bad in ('CN=Microsoft Corporation, O=Microsoft Corporation Evil, C=US',
                    'CN=x, O=microsoft corporation, C=US', 'CN=O=Microsoft Corporation Ltd',
                    'CN=Evil, OU=O=Microsoft Corporation, C=US', 'CN=Evil, O=Evil Corp, C=US'):
            self.assertFalse(re.search(pattern, bad), bad)
        self.assertIn("[string]$Signature.Status -cne 'Valid'", self.text)

    def test_download_and_elevation_contract(self):
        self.assertIn("'https://aka.ms/vs/17/release/vc_redist.x64.exe'", self.text)
        self.assertIn('[Net.SecurityProtocolType]::Tls12', self.text)
        self.assertIn("-ArgumentList '/install','/quiet','/norestart' -Verb RunAs", self.text)
        self.assertIn('@(0, 3010, 1638)', self.text)
        self.assertIn("@('vcruntime140.dll','vcruntime140_1.dll','msvcp140.dll')", self.text)
        self.assertIn('Remove-Item -LiteralPath $dir -Recurse -Force', self.text)
        # Signature is checked before anything is executed.
        self.assertLess(self.text.index('Test-MicrosoftSignature (Get-AuthenticodeSignature'),
                        self.text.index('$p = Start-Process -FilePath $file'))

    def test_step_runs_before_telegram_and_is_never_terminal(self):
        self.assertLess(self.text.index('$vc = Install-VcRuntime'), self.text.index('telegram_bot_token=$telegramToken'))
        block = self.text[self.text.index('if (-not (Test-VcRuntime))'):self.text.index('$telegramNote = ')]
        self.assertNotIn('Fail ', block)
        for key in ('start', 'installed', 'declined', 'network', 'signature', 'failed'):
            self.assertRegex(self.text, r"'" + key + r"'\s+=\s+'[^']*[А-Яа-я]")
        self.assertIn('Распознавание голоса не включено', self.text)


class FakeStore:
    """Stand-in for gateway.pairing.PairingStore (only the methods telegram.py uses)."""
    def __init__(self, pending=(), approved=()):
        self.pending = [dict(p) for p in pending]
        self.approved = list(approved)
        self.approve_calls = []
        self.approve_result = 'auto'

    def list_pending(self, platform=None):
        assert platform == 'telegram'
        return [dict(p) for p in self.pending]

    def list_approved(self, platform=None):
        return list(self.approved)

    def approve_request(self, platform, request_id):
        self.approve_calls.append((platform, request_id))
        if self.approve_result != 'auto':
            return self.approve_result
        for p in self.pending:
            if p['request_id'] == request_id:
                self.pending.remove(p)
                self.approved.append(p)
                return {'user_id': p['user_id'], 'user_name': p['user_name']}
        return None


RID = 'a1b2c3d4e5f60718'
UID = '424242'
OWNER = {'platform': 'telegram', 'request_id': RID, 'user_id': UID, 'user_name': 'Павел "Owner" <b>', 'age_minutes': 1}


class OwnerApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telegram-owner-')
        self.home = Path(self.tmp.name)
        self.env = self.home / '.env'
        self.base_env = ('HERMES_SUBSCRIBER_API_KEY=\'k\'\nTELEGRAM_BOT_TOKEN=' + TOKEN + '\n').encode()
        self.env.write_bytes(self.base_env)
        self._protect, self._atomic = t.protect, t.atomic_write
        t.protect = lambda path: None
        self.restart = Recorder(True)
        self.connected = Recorder(True)
        self.send = Recorder(True)

    def tearDown(self):
        t.protect, t.atomic_write = self._protect, self._atomic
        self.tmp.cleanup()

    def approve(self, store, request_id=RID, user_id=UID):
        result = t.approve(self.home, self.home / 'repo', request_id, user_id, store=store, restart=self.restart,
                           connected=self.connected, send=self.send, clock=lambda: 500.0)
        self.assertNotIn(TOKEN, json.dumps(result))
        return result

    # --- pending -------------------------------------------------------------
    def test_pending_lists_structured_requests_with_username(self):
        stranger = dict(OWNER, request_id='ffffffffffffffff', user_id='777', user_name='Stranger')
        broken = dict(OWNER, request_id='', user_id='x')   # legacy entry without request id
        store = FakeStore([OWNER, stranger, broken])
        lookups = []

        def lookup(token, user_id):
            lookups.append(user_id)
            return {'424242': 'pavel_x', '777': 'bad name!'}[user_id]
        result = t.pending(self.home, None, store=store, lookup=lookup)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['requests'], [
            {'id': RID, 'user_id': UID, 'name': 'Павел Owner b', 'username': 'pavel_x'},
            {'id': 'ffffffffffffffff', 'user_id': '777', 'name': 'Stranger', 'username': ''}])
        self.assertEqual(lookups, [UID, '777'])
        self.assertEqual(store.approve_calls, [], 'listing must never approve anyone')
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_pending_none_done_off_and_lookup_failure(self):
        self.assertEqual(t.pending(self.home, None, store=FakeStore(), lookup=None)['status'], 'none')
        self.assertEqual(t.pending(self.home, None, store=FakeStore(approved=[OWNER]))['status'], 'done')

        def boom(token, user_id):
            raise OSError('network')
        self.assertEqual(t.pending(self.home, None, store=FakeStore([OWNER]), lookup=boom)['requests'][0]['username'], '')
        self.env.write_bytes(b'OTHER=1\n')
        self.assertEqual(t.pending(self.home, None, store=FakeStore([OWNER])), {'ok': True, 'status': 'off', 'requests': []})

    # --- approve -------------------------------------------------------------
    def test_approve_sets_home_restarts_and_welcomes(self):
        store = FakeStore([OWNER])
        result = self.approve(store)
        self.assertEqual(result, {'ok': True, 'status': 'approved', 'welcomed': True})
        self.assertEqual(store.approve_calls, [('telegram', RID)])
        self.assertEqual(self.env.read_bytes(), self.base_env +
                         ("TELEGRAM_HOME_CHANNEL='" + UID + "'\nTELEGRAM_HOME_CHANNEL_NAME='Павел Owner b'\n").encode())
        values = t.env_values(self.env.read_bytes())
        self.assertEqual((values['TELEGRAM_HOME_CHANNEL'], values['TELEGRAM_HOME_CHANNEL_NAME']), (UID, 'Павел Owner b'))
        self.assertEqual(len(self.restart.calls), 1)
        self.assertEqual(self.connected.calls, [(self.home, 499.0)])
        self.assertEqual(self.send.calls, [(TOKEN, UID)])

    def test_approve_rejects_bad_ids_and_unknown_requests(self):
        for rid, uid in (('A1B2C3D4E5F60718', UID), ('short', UID), (RID, '12a'), (None, UID), (RID, None)):
            self.assertEqual(self.approve(FakeStore([OWNER]), rid, uid)['status'], 'failed')
        store = FakeStore([OWNER])
        self.assertEqual(self.approve(store, 'b' * 16, UID)['status'], 'expired')
        self.assertEqual(self.approve(store, RID, '999')['status'], 'expired', 'id and user must match the shown row')
        self.assertEqual(store.approve_calls, [])
        self.assertEqual(self.env.read_bytes(), self.base_env)
        self.assertEqual((self.restart.calls, self.send.calls), ([], []))

    def test_race_lost_rolls_env_back_byte_exact(self):
        store = FakeStore([OWNER])
        store.approve_result = None          # expired between list and approve
        self.assertEqual(self.approve(store)['status'], 'expired')
        self.assertEqual(self.env.read_bytes(), self.base_env)
        self.assertEqual(self.restart.calls, [])

    def test_existing_home_channel_is_kept_and_no_restart(self):
        before = self.base_env + b"TELEGRAM_HOME_CHANNEL='-100500'\n"
        self.env.write_bytes(before)
        store = FakeStore([OWNER])
        self.assertEqual(self.approve(store)['status'], 'approved')
        self.assertEqual(self.env.read_bytes(), before)
        self.assertEqual(store.approve_calls, [('telegram', RID)])
        self.assertEqual(self.restart.calls, [])

    def test_same_home_already_set_is_idempotent(self):
        before = self.base_env + ("TELEGRAM_HOME_CHANNEL='" + UID + "'\nTELEGRAM_HOME_CHANNEL_NAME='Павел Owner b'\n").encode()
        self.env.write_bytes(before)
        self.assertEqual(self.approve(FakeStore([OWNER]))['status'], 'approved')
        self.assertEqual(self.env.read_bytes(), before)
        self.assertEqual(self.restart.calls, [])

    def test_restart_failure_keeps_approval_and_reports_partial(self):
        for restart in (Recorder(False), Recorder(exc=OSError('x'))):
            self.env.write_bytes(self.base_env)
            self.restart = restart
            store = FakeStore([OWNER])
            self.assertEqual(self.approve(store)['status'], 'approved_partial')
            self.assertEqual(len(store.approve_calls), 1)
        self.connected = Recorder(False)
        self.restart = Recorder(True)
        self.env.write_bytes(self.base_env)
        self.assertEqual(self.approve(FakeStore([OWNER]))['status'], 'approved_partial')

    def test_welcome_failure_is_reported_not_fatal(self):
        self.send = Recorder(exc=OSError('blocked'))
        self.assertEqual(self.approve(FakeStore([OWNER])), {'ok': True, 'status': 'approved', 'welcomed': False})

    def test_env_write_failure_approves_nobody(self):
        real = self._atomic
        calls = []

        def corrupting(path, data, secret=False):
            calls.append(1)
            real(path, b'GARBAGE=1\n' if len(calls) == 1 else data, secret)
        t.atomic_write = corrupting
        store = FakeStore([OWNER])
        self.assertEqual(self.approve(store)['status'], 'failed')
        self.assertEqual(self.env.read_bytes(), self.base_env)
        self.assertEqual(store.approve_calls, [])

    def test_no_token_means_off(self):
        self.env.write_bytes(b'')
        self.assertEqual(self.approve(FakeStore([OWNER]))['status'], 'off')

    def test_clean_name(self):
        self.assertEqual(t.clean_name("Ann'\n\"Bob\\\" <script>x</script>\x00"), 'AnnBob scriptxscript')
        self.assertEqual(t.clean_name(None), '')
        self.assertEqual(len(t.clean_name('я' * 200)), 64)

    def test_cli_pending_without_token_needs_no_store(self):
        self.env.write_bytes(b'')
        result = t.run_cli(['telegram.py', str(self.home), str(self.home / 'repo'), 'pending'], io.StringIO('{}'))
        self.assertEqual(result, {'ok': True, 'status': 'off', 'requests': []})
        with self.assertRaises(ValueError):
            t.run_cli(['telegram.py', str(self.home), str(self.home), 'drop-all'], io.StringIO('{}'))


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telegram-autostart-')
        self.home = Path(self.tmp.name) / 'home'
        (self.home / 'gateway-service').mkdir(parents=True)
        self.appdata = Path(self.tmp.name) / 'appdata'
        self.startup = self.appdata / 'Microsoft/Windows/Start Menu/Programs/Startup'
        self.startup.mkdir(parents=True)
        self._env = os.environ.get('APPDATA')
        os.environ['APPDATA'] = str(self.appdata)
        self._run = t.subprocess.run
        self.queries = []
        self.task_code = 1

        def fake_run(argv, **kwargs):   # no process is ever spawned
            self.queries.append(argv)
            return type('R', (), {'returncode': self.task_code})()
        t.subprocess.run = fake_run

    def tearDown(self):
        t.subprocess.run = self._run
        if self._env is None:
            os.environ.pop('APPDATA', None)
        else:
            os.environ['APPDATA'] = self._env
        self.tmp.cleanup()

    def test_no_launcher_means_no_autostart(self):
        (self.startup / 'Hermes_Gateway.vbs').write_text('x', encoding='utf-8')
        self.assertIsNone(t.autostart_state(self.home))

    def test_startup_folder_entry(self):
        (self.home / 'gateway-service' / 'Hermes_Gateway.vbs').write_text('x', encoding='utf-8')
        (self.startup / 'Hermes_Gateway.vbs').write_text('x', encoding='utf-8')
        self.assertEqual(t.autostart_state(self.home), 'startup')
        self.assertEqual(self.queries, [])

    def test_scheduled_task(self):
        (self.home / 'gateway-service' / 'Hermes_Gateway.vbs').write_text('x', encoding='utf-8')
        self.task_code = 0
        self.assertEqual(t.autostart_state(self.home), 'task')
        self.assertEqual(self.queries[0][1:], ['/Query', '/TN', 'Hermes_Gateway'])
        self.task_code = 1
        self.assertIsNone(t.autostart_state(self.home))


if __name__ == '__main__':
    unittest.main(verbosity=2)
