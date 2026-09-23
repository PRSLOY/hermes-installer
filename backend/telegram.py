"""Optional Telegram stage: bot token -> .env, messaging gateway autostart.

JSON {"telegram_bot_token": "..."} on stdin, one JSON report on stdout. The
token is never printed, logged or put on a command line; exceptions are never
serialized. Only %LOCALAPPDATA%/hermes/.env receives the token, through the
same protected atomic write as configure.py.

Access control is upstream's default-deny (pinned c712f06):
gateway/authz_mixin.py:_principal_authorized falls through to
GATEWAY_ALLOW_ALL_USERS (unset) when no allowlist exists, and unknown DMs get a
pairing request (authz_mixin.py:_get_unauthorized_dm_behavior -> "pair") that
the owner approves in Desktop -> "Сообщения" -> "Одобрить". This stage
therefore writes TELEGRAM_BOT_TOKEN only and refuses to proceed when any
allow-all switch is already on.
"""
import http.client
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider import Failure, tls_context

TOKEN_RE = re.compile(r'^[0-9]{1,20}:[A-Za-z0-9_-]{30,64}$')
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{5,64}$')
GETME_TIMEOUT = 15
GATEWAY_CLI_TIMEOUT = 180
CONNECT_TIMEOUT = 120
ALLOW_ALL_KEYS = ('TELEGRAM_ALLOW_ALL_USERS', 'GATEWAY_ALLOW_ALL_USERS')


class Skip(Exception):
    """Internal reason code (never user text): worker.ps1 maps it to a fixed Russian line."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_getme(token):
    """Live getMe. Returns ('ok', username) | ('token', None) | ('network', None); never raises."""
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/getMe', method='GET')
    try:
        # The token is in the URL path: a redirect must never carry it elsewhere.
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context())).open(request, timeout=GETME_TIMEOUT) as response:
            body = json.loads(response.read(65536).decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return ('token', None) if exc.code in (401, 404) else ('network', None)
    except (OSError, ValueError, http.client.HTTPException):
        return ('network', None)
    result = body.get('result') if isinstance(body, dict) else None
    if not (isinstance(body, dict) and body.get('ok') is True and isinstance(result, dict) and result.get('is_bot') is True):
        return ('token', None)
    username = result.get('username')
    return ('ok', username if isinstance(username, str) and USERNAME_RE.match(username) else None)


def protect(path):
    ps = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(Path(__file__).with_name('protect.ps1')), str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise Failure('TELEGRAM', 'Не удалось защитить хранилище токена правами Windows. Изменение отменено.')


def atomic_write(path, data, secret=False):
    tmp = path.with_name(path.name + '.subscriber-' + uuid.uuid4().hex)
    try:
        tmp.touch(exist_ok=False)
        if secret:
            protect(tmp)
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def env_values(data):
    from dotenv import dotenv_values
    return dotenv_values(stream=io.StringIO((data or b'').decode('utf-8-sig')))


def truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def check_env(values, token):
    """Raise Skip for settings we must not touch; True when the token is already stored."""
    if any(truthy(values.get(key)) for key in ALLOW_ALL_KEYS):
        # Someone opened the bot to everyone: adding our token would expose the agent.
        raise Skip('open')
    existing = values.get('TELEGRAM_BOT_TOKEN')
    if existing and existing != token:
        # Another bot is configured: never replace the user's own setup.
        raise Skip('exists')
    return existing == token


def write_token(env_path, token):
    """Protected atomic write + read-back. Returns True if bytes changed. Byte-exact rollback on failure."""
    before = env_path.read_bytes() if env_path.exists() else None
    values = env_values(before)
    if check_env(values, token):
        return False
    text = (before or b'').decode('utf-8-sig')
    # Token charset is [0-9A-Za-z_:-] (TOKEN_RE): no quoting or escaping needed.
    candidate = text + ('\n' if text and not text.endswith('\n') else '') + 'TELEGRAM_BOT_TOKEN=' + token + '\n'
    current = env_path.read_bytes() if env_path.exists() else None
    if current != before:
        raise Skip('failed')
    changed = False
    try:
        atomic_write(env_path, candidate.encode('utf-8'), secret=True)
        changed = True
        after = env_values(env_path.read_bytes())
        if after.get('TELEGRAM_BOT_TOKEN') != token or {k: v for k, v in after.items() if k != 'TELEGRAM_BOT_TOKEN'} != {k: v for k, v in values.items() if k != 'TELEGRAM_BOT_TOKEN'}:
            raise Skip('failed')
    except Exception:
        if changed:
            try:
                if before is None:
                    env_path.unlink(missing_ok=True)
                else:
                    atomic_write(env_path, before, secret=True)
            except Exception:
                pass
        raise
    return True


def ensure_deps(home, repo):
    """Install python-telegram-bot now via upstream's own allowlisted lazy installer
    (tools/lazy_deps.py "platform.telegram"), so a failure shows at install time."""
    os.environ['HERMES_HOME'] = str(home)
    sys.path.insert(0, str(repo))
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from tools.lazy_deps import ensure
        ensure('platform.telegram', prompt=False)


def gateway_env(home):
    env = {k: v for k, v in os.environ.items() if k not in ('HERMES_PROFILE', 'HERMES_INFERENCE_MODEL', 'HERMES_INFERENCE_PROVIDER')}
    env.update({'HERMES_HOME': str(home), 'HERMES_NONINTERACTIVE': '1', 'PYTHONUTF8': '1',
                'HERMES_GATEWAY_INSTALL_START_NOW': '1', 'HERMES_GATEWAY_INSTALL_START_ON_LOGIN': '1'})
    return env


def run_gateway_cli(home, repo, args):
    # Output is captured and discarded: never relayed. hermes_cli/gateway_windows.py
    # install(): an ELEVATED process (e.g. Windows Sandbox's admin account) registers a
    # Scheduled Task "Hermes_Gateway" (logon trigger, no Startup file, no Run key);
    # a non-elevated one, non-interactive, falls back to Startup\Hermes_Gateway.vbs.
    # Both start the gateway detached right away (--start-now).
    result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'gateway', *args], cwd=str(repo),
                            env=gateway_env(home), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=GATEWAY_CLI_TIMEOUT,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode == 0


def enable_gateway(home, repo, restart):
    if not run_gateway_cli(home, repo, ['install', '--start-now', '--start-on-login']):
        return False
    # A gateway that was already running read .env before the token existed.
    return run_gateway_cli(home, repo, ['restart']) if restart else True


def wait_connected(home, since, timeout=CONNECT_TIMEOUT, sleep=time.sleep, clock=time.time):
    """True once gateway_state.json (gateway/status.py write_runtime_status) reports
    platforms.telegram.state == 'connected' in a write made after ``since``."""
    path = Path(home) / 'gateway_state.json'
    deadline = clock() + timeout
    while True:
        try:
            if path.stat().st_mtime >= since:
                state = json.loads(path.read_text(encoding='utf-8'))
                telegram = (state.get('platforms') or {}).get('telegram') or {}
                if telegram.get('state') == 'connected':
                    return True
        except (OSError, ValueError, AttributeError):
            pass
        if clock() >= deadline:
            return False
        sleep(2)


TASK_NAME = 'Hermes_Gateway'   # gateway_windows._TASK_NAME_DEFAULT (default profile: no suffix)


def autostart_state(home):
    """Where login autostart lives: 'task' | 'startup' | None. Mirrors
    gateway_windows.is_task_registered() / is_startup_entry_installed(), plus the
    launcher both of them run (<HERMES_HOME>/gateway-service/Hermes_Gateway.vbs)."""
    if not (Path(home) / 'gateway-service' / (TASK_NAME + '.vbs')).is_file():
        return None
    appdata = os.environ.get('APPDATA', '').strip()
    if appdata and (Path(appdata) / 'Microsoft/Windows/Start Menu/Programs/Startup' / (TASK_NAME + '.vbs')).is_file():
        return 'startup'
    schtasks = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32' / 'schtasks.exe'
    try:
        result = subprocess.run([str(schtasks), '/Query', '/TN', TASK_NAME], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return 'task' if result.returncode == 0 else None


def main(home, repo, token, probe=None, deps=None, gateway=None, connected=None, clock=time.time, autostart=None):
    probe = probe or probe_getme
    deps = deps or ensure_deps
    gateway = gateway or enable_gateway
    connected = connected or wait_connected
    autostart = autostart or autostart_state
    home, repo = Path(home), Path(repo)
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return {'ok': False, 'status': 'token'}
    try:
        status, username = probe(token)
    except Exception:
        status, username = 'network', None
    if status != 'ok':
        return {'ok': False, 'status': status if status in ('token', 'network') else 'failed'}
    env_path = home / '.env'
    try:
        # Conflicts first: no package install for a bot we would refuse to store.
        check_env(env_values(env_path.read_bytes() if env_path.exists() else None), token)
        deps(home, repo)
        changed = write_token(env_path, token)
    except Skip as exc:
        reason = str(exc)
        return {'ok': False, 'status': reason if reason in ('exists', 'open') else 'failed'}
    except Exception:
        return {'ok': False, 'status': 'failed'}
    # Re-run with the same token: a gateway already connected earlier counts.
    since = clock() - 1 if changed else 0
    had_gateway = (home / 'gateway.pid').exists()
    try:
        started = bool(gateway(home, repo, changed and had_gateway))
    except Exception:
        started = False
    if not started or not connected(home, since):
        return {'ok': False, 'status': 'saved', 'bot': username}
    try:
        where = autostart(home)
    except Exception:
        where = None
    return {'ok': True, 'status': 'connected', 'bot': username, 'autostart': where if where in ('task', 'startup') else None}


# --- Owner approval (Done screen) -------------------------------------------
# Hermes' own structured source: gateway/pairing.py PairingStore.list_pending() /
# approve_request() -- exactly what `hermes pairing list|approve` (hermes_cli/pairing.py)
# and Desktop's /api/pairing (hermes_cli/web_routers/ops.py) call. No text scraping.

REQUEST_ID_RE = re.compile(r'^[0-9a-f]{16}$')      # PairingStore.looks_like_request_id
USER_ID_RE = re.compile(r'^[0-9]{1,20}$')          # Telegram private chat id == user id
NAME_DROP_RE = re.compile(r'[^\w .\-]', re.UNICODE)
WELCOME = ('Готово! Это ваш Hermes. Напишите, что нужно сделать, — я отвечу здесь. '
           'Можно присылать текст, фото, документы и голосовые.')


def read_token(home):
    env_path = Path(home) / '.env'
    token = env_values(env_path.read_bytes() if env_path.exists() else None).get('TELEGRAM_BOT_TOKEN')
    return token if isinstance(token, str) and TOKEN_RE.match(token) else None


def clean_name(value):
    """Untrusted Telegram display name -> short safe text (no quotes, controls or markup)."""
    text = ' '.join(NAME_DROP_RE.sub('', str(value or '')).split())
    return text[:64]


def load_store(home, repo):
    os.environ['HERMES_HOME'] = str(home)
    os.environ.pop('HERMES_PROFILE', None)
    sys.path.insert(0, str(repo))
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from gateway.pairing import PairingStore
        return PairingStore()


def bot_api(token, method, params, timeout=10):
    """Bot API call; result dict/value or None. Never raises, never follows redirects."""
    body = json.dumps(params).encode('utf-8')
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method, data=body, method='POST',
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context())).open(request, timeout=timeout) as response:
            data = json.loads(response.read(65536).decode('utf-8'))
    except Exception:
        return None
    return data.get('result') if isinstance(data, dict) and data.get('ok') is True else None


def lookup_username(token, user_id):
    # getChat does not compete with the gateway's long polling (only getUpdates does).
    result = bot_api(token, 'getChat', {'chat_id': int(user_id)})
    username = result.get('username') if isinstance(result, dict) else None
    return username if isinstance(username, str) and USERNAME_RE.match(username) else ''


def send_welcome(token, user_id):
    return bot_api(token, 'sendMessage', {'chat_id': int(user_id), 'text': WELCOME}) is not None


def telegram_pending(store):
    rows = []
    for item in store.list_pending('telegram'):
        request_id = str(item.get('request_id') or '').lower()
        user_id = str(item.get('user_id') or '')
        if REQUEST_ID_RE.match(request_id) and USER_ID_RE.match(user_id):
            rows.append((request_id, user_id, item.get('user_name')))
    return rows


def pending(home, repo, store=None, lookup=None):
    home = Path(home)
    token = read_token(home)
    if not token:
        return {'ok': True, 'status': 'off', 'requests': []}
    store = store or load_store(home, repo)
    lookup = lookup or lookup_username
    requests = []
    for request_id, user_id, name in telegram_pending(store)[:10]:
        try:
            username = lookup(token, user_id) or ''
        except Exception:
            username = ''
        requests.append({'id': request_id, 'user_id': user_id, 'name': clean_name(name),
                         'username': username if USERNAME_RE.match(username) else ''})
    if requests:
        return {'ok': True, 'status': 'pending', 'requests': requests}
    status = 'done' if store.list_approved('telegram') else 'none'
    return {'ok': True, 'status': status, 'requests': []}


def update_env(env_path, pairs):
    """Protected atomic append of KEY=value pairs, like write_token. Keys already holding the
    same value are skipped; a different existing value raises Skip('exists') (never overwrite
    the user's setting). Returns the pre-write bytes (None = file absent) when something was
    written, else False. Byte-exact rollback on any failure after the write."""
    before = env_path.read_bytes() if env_path.exists() else None
    values = env_values(before)
    for key, value in pairs:
        if values.get(key) not in (None, '', value):
            raise Skip('exists')
    missing = [(k, v) for k, v in pairs if values.get(k) != v]
    if not missing:
        return False
    text = (before or b'').decode('utf-8-sig')
    # Values are digits or clean_name() output: no quote/backslash can occur inside.
    candidate = text + ('\n' if text and not text.endswith('\n') else '') + ''.join(
        key + "='" + value + "'\n" for key, value in missing)
    changed = False
    try:
        atomic_write(env_path, candidate.encode('utf-8'), secret=True)
        changed = True
        after = env_values(env_path.read_bytes())
        keys = {k for k, _ in pairs}
        if any(after.get(k) != v for k, v in pairs) or {k: v for k, v in after.items() if k not in keys} != {k: v for k, v in values.items() if k not in keys}:
            raise Skip('failed')
    except Exception:
        if changed:
            restore_env(env_path, before)
        raise
    return before if before is not None else None


def restore_env(env_path, before):
    try:
        if before is None:
            env_path.unlink(missing_ok=True)
        else:
            atomic_write(env_path, before, secret=True)
    except Exception:
        pass


def restart_gateway(home, repo):
    return run_gateway_cli(home, repo, ['restart'])


def approve(home, repo, request_id, user_id, store=None, restart=None, connected=None, send=None, clock=time.time):
    """Owner clicked «Да, это я»: approve exactly that pending request and make that private
    chat the Telegram home channel (what /sethome stores: gateway/slash_commands.py
    _handle_set_home_command -> TELEGRAM_HOME_CHANNEL; config_env._env_home_channel reads
    it with _NAME at gateway start), then restart the gateway so it is picked up."""
    home, repo = Path(home), Path(repo)
    if not (isinstance(request_id, str) and REQUEST_ID_RE.match(request_id)
            and isinstance(user_id, str) and USER_ID_RE.match(user_id)):
        return {'ok': False, 'status': 'failed'}
    token = read_token(home)
    if not token:
        return {'ok': False, 'status': 'off'}
    store = store or load_store(home, repo)
    restart = restart or restart_gateway
    connected = connected or wait_connected
    send = send or send_welcome
    match = [row for row in telegram_pending(store) if row[0] == request_id and row[1] == user_id]
    if not match:
        return {'ok': False, 'status': 'expired'}
    name = clean_name(match[0][2]) or 'Telegram'
    env_path = home / '.env'
    wrote, before = False, None
    try:
        result = update_env(env_path, [('TELEGRAM_HOME_CHANNEL', user_id), ('TELEGRAM_HOME_CHANNEL_NAME', name)])
        if result is not False:
            wrote, before = True, result
    except Skip as exc:
        if str(exc) != 'exists':
            return {'ok': False, 'status': 'failed'}
        # The user already chose a home channel: keep it, still approve.
    except Exception:
        return {'ok': False, 'status': 'failed'}
    try:
        granted = store.approve_request('telegram', request_id)
    except Exception:
        granted = None
    if not granted or str(granted.get('user_id')) != user_id:
        if wrote:
            restore_env(env_path, before)
        return {'ok': False, 'status': 'expired'}
    # Approval is live immediately (the store is re-read per message); the restart is only
    # for the home channel env. Windows restart = planned stop + start (gateway_windows.restart).
    settled = True
    if wrote:
        since = clock() - 1
        try:
            settled = bool(restart(home, repo)) and bool(connected(home, since))
        except Exception:
            settled = False
    try:
        welcomed = bool(send(token, user_id))
    except Exception:
        welcomed = False
    return {'ok': True, 'status': 'approved' if settled else 'approved_partial', 'welcomed': welcomed}


def run_cli(argv, stdin):
    home, repo = Path(argv[1]), Path(argv[2])
    mode = argv[3] if len(argv) > 3 else 'connect'
    data = json.load(stdin)
    if not isinstance(data, dict):
        raise ValueError('input')
    if mode == 'connect':
        return main(home, repo, data.get('telegram_bot_token'))
    if mode == 'pending':
        return pending(home, repo)
    if mode == 'approve':
        return approve(home, repo, data.get('request_id'), data.get('user_id'))
    raise ValueError('mode')


if __name__ == '__main__':
    code = 1
    output = {'ok': False, 'status': 'failed'}
    try:
        sys.stdin.reconfigure(encoding='utf-8-sig')
        sys.stdout.reconfigure(encoding='utf-8')
        output = run_cli(sys.argv, sys.stdin)
        code = 0 if output.get('ok') is True else 1
    except Exception:
        output = {'ok': False, 'status': 'failed'}
        code = 1
    print(json.dumps(output, ensure_ascii=False), flush=True)
    # Imported runtime modules may own background threads; the result is final.
    os._exit(code)
