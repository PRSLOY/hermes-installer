"""Out-of-box extras stage: SOUL, first-step skills, keyless MCP catalogs.

`extras.py HOME REPO ASSETS --marketplaces` runs only the marketplaces MCP step
(download + uv sync + stdio probe), a separate worker step with its own budget.

One JSON report on stdout, fixed Russian messages, no exception escapes the
boundary. Each part is independent: one failure never cancels the others. This
module writes SOUL.md, skills and config.yaml only; it never touches .env or any
credential. Style mirrors configure.py (Failure, atomic_write, protect).
"""
import http.client
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider import Failure, tls_context

# Keyless endpoints verified by the orchestrator on 2026-09-22 (initialize -> HTTP 200).
MCP_SERVERS = (
    ('wolfram', 'https://agenttools.wolfram.com/mcp'),
    ('microsoft-learn', 'https://learn.microsoft.com/api/mcp'),
    ('context7', 'https://mcp.context7.com/mcp'),
    ('deepwiki', 'https://mcp.deepwiki.com/mcp'),
)
MCP_TIMEOUT = 15


def protect(path):
    ps = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(Path(__file__).with_name('protect.ps1')), str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise Failure('EXTRAS', 'Не удалось защитить файл настройки правами Windows. Изменение отменено.')


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


def probe_initialize(url):
    """Live MCP initialize probe. True only on a 200 answer; never raises."""
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                                  'clientInfo': {'name': 'hermes-installer', 'version': '1.0'}}}).encode('utf-8')
    request = urllib.request.Request(url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'Accept': 'application/json, text/event-stream'})
    try:
        # Redirects are never followed: a moved endpoint must not silently 200.
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context())).open(request, timeout=MCP_TIMEOUT) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return exc.code == 200
    except (OSError, ValueError, http.client.HTTPException):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_default_soul(repo):
    """Import default_soul from the installed repo (configure.py sys.path pattern)."""
    sys.path.insert(0, str(repo))
    importlib.invalidate_caches()
    from hermes_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul
    return DEFAULT_SOUL_MD, is_legacy_template_soul


def step_soul(home, assets, repo):
    source = Path(assets) / 'SOUL.md'
    target = Path(home) / 'SOUL.md'
    if not source.is_file():
        return 'failed'
    payload = source.read_bytes()
    if not target.exists():
        atomic_write(target, payload)
        return 'installed'
    default_soul, is_legacy = load_default_soul(repo)
    existing = target.read_text(encoding='utf-8')
    if existing == default_soul or is_legacy(existing):
        atomic_write(target, payload)
        return 'installed'
    return 'kept'


def step_skills(home, assets):
    # Every shipped skill folder is copied only when the user has no folder of
    # that name: an existing one, identical or not, is the user's and never
    # overwritten. 'installed' if at least one landed, 'kept' if all existed.
    root = Path(assets) / 'skills'
    sources = sorted(p for p in root.iterdir() if p.is_dir() and (p / 'SKILL.md').is_file()) if root.is_dir() else []
    if not sources:
        return 'failed'
    installed = False
    for source in sources:
        target = Path(home) / 'skills' / source.name
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        installed = True
    return 'installed' if installed else 'kept'


def yaml_scalar(value):
    """One YAML scalar. Plain for safe URL-like text; JSON double quotes
    (a valid YAML double-quoted scalar) for anything else, e.g. Windows paths."""
    import re
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9._~:/?&=%-]+', text):
        return text
    return json.dumps(text, ensure_ascii=True)


def server_block(additions, indent='  '):
    lines = []
    for name, entry in additions.items():
        lines.append(indent + name + ':\n')
        for key, value in entry.items():
            if isinstance(value, dict):
                lines.append(indent + '  ' + key + ':\n')
                for sub, subvalue in value.items():
                    lines.append(indent + '    ' + sub + ': ' + yaml_scalar(subvalue) + '\n')
            elif isinstance(value, (list, tuple)):
                lines.append(indent + '  ' + key + ': [' + ', '.join(json.dumps(str(v), ensure_ascii=True) for v in value) + ']\n')
            else:
                lines.append(indent + '  ' + key + ': ' + yaml_scalar(value) + '\n')
    return ''.join(lines)


def merge_mcp_text(src, servers, additions):
    """Text-preserving merge: keep comments and unrelated keys untouched."""
    import re
    lines = src.splitlines(keepends=True)
    # Top-level key in any YAML-legal spelling: mcp_servers:, "mcp_servers":,
    # 'mcp_servers':, mcp_servers : (a miss appends a duplicate block and
    # safe_load keeps only the last one, silently dropping user servers).
    key = re.compile(r'''^(["']?)mcp_servers\1[ \t]*:''')
    for index, line in enumerate(lines):
        match = key.match(line)
        if match:
            rest = line[match.end():].strip()
            if rest == '' or rest.startswith('#'):
                return ''.join(lines[:index + 1]) + server_block(additions) + ''.join(lines[index + 1:])
            # Inline/flow value: re-emit the merged mapping as a block (data kept).
            import yaml
            merged = {**servers, **additions}
            dumped = yaml.safe_dump({'mcp_servers': merged}, sort_keys=False,
                                    default_flow_style=False, allow_unicode=True)
            return ''.join(lines[:index]) + dumped + ''.join(lines[index + 1:])
    prefix = src if (not src or src.endswith('\n')) else src + '\n'
    return prefix + 'mcp_servers:\n' + server_block(additions)


def rollback_config(path, before):
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, before)
    except Exception:
        pass


def step_mcp(home, probe):
    import yaml
    config_path = Path(home) / 'config.yaml'
    before = config_path.read_bytes() if config_path.exists() else None
    src = (before or b'').decode('utf-8-sig')
    cfg = yaml.safe_load(src) or {}
    if not isinstance(cfg, dict):
        raise Failure('EXTRAS', 'Неверный config.yaml; набор MCP не изменён.')
    servers = cfg.get('mcp_servers')
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        return {name: 'failed' for name, _ in MCP_SERVERS}
    result = {}
    additions = {}
    for name, url in MCP_SERVERS:
        if name in servers:
            result[name] = 'exists'
            continue
        try:
            reachable = bool(probe(url))
        except Exception:
            reachable = False
        if reachable:
            additions[name] = {'url': url, 'connect_timeout': 60, 'enabled': True}
        else:
            result[name] = 'unreachable'
    if not additions:
        return result
    status = 'added' if commit_mcp_additions(config_path, before, src, cfg, servers, additions) else 'failed'
    for name in additions:
        result[name] = status
    return result


def read_mcp_config(home):
    """(config_path, before_bytes, src_text, cfg, servers). Raises Failure on a
    non-mapping config; servers is None when mcp_servers is not a mapping."""
    import yaml
    config_path = Path(home) / 'config.yaml'
    before = config_path.read_bytes() if config_path.exists() else None
    src = (before or b'').decode('utf-8-sig')
    cfg = yaml.safe_load(src) or {}
    if not isinstance(cfg, dict):
        raise Failure('EXTRAS', 'Неверный config.yaml; набор MCP не изменён.')
    servers = cfg.get('mcp_servers')
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        servers = None
    return config_path, before, src, cfg, servers


def commit_mcp_additions(config_path, before, src, cfg, servers, additions):
    """Write the merged config; True only if it landed and verified. Never raises."""
    import yaml
    candidate = merge_mcp_text(src, servers, additions)
    # Probes can take up to a minute: never write a candidate built from stale
    # bytes over an edit made meanwhile (configure.py aborts the same way).
    current = config_path.read_bytes() if config_path.exists() else None
    if current != before:
        return False
    try:
        atomic_write(config_path, candidate.encode('utf-8'))
        parsed = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
        written = parsed.get('mcp_servers') or {}
        # Whole section, not just our additions: an existing user server must
        # never disappear while the step reports 'added'.
        if written != {**servers, **additions}:
            raise Failure('EXTRAS', 'Контрольное чтение набора MCP не прошло; выполняется откат.')
        if {k: v for k, v in parsed.items() if k != 'mcp_servers'} != {k: v for k, v in cfg.items() if k != 'mcp_servers'}:
            raise Failure('EXTRAS', 'Проверка сохранности config.yaml не прошла; выполняется откат.')
    except Exception:
        rollback_config(config_path, before)
        return False
    return True


# --- Marketplaces: ru-marketplace-mcp (MIT) over stdio -----------------------
# Read-only product/price search on Ozon, Avito, Yandex Market, Detsky Mir plus
# cross-marketplace comparison. No keys. Pinned by full commit SHA.
MARKETPLACES_NAME = 'marketplaces'
MARKETPLACES_REPO = 'Vladimir-Human/ru-marketplace-mcp'
MARKETPLACES_COMMIT = 'c17bd360de60780a9e8d3690288b70181bd07355'
MARKETPLACES_URL = 'https://codeload.github.com/%s/zip/%s' % (MARKETPLACES_REPO, MARKETPLACES_COMMIT)
# github.com is blocked on many Russian links: the same archive through the GitHub proxies
# install.ps1 uses ($script:GitHubZipProxies). A proxy can serve anything, so every source
# is accepted only if the extracted tree matches the manifest built from the pinned commit
# (assets/marketplaces/tree-manifest.sha256, tools/tree_manifest.py).
MARKETPLACES_ZIP_SOURCES = (
    MARKETPLACES_URL,
    'https://ghproxy.net/https://github.com/%s/archive/%s.zip' % (MARKETPLACES_REPO, MARKETPLACES_COMMIT),
    'https://gh-proxy.com/https://github.com/%s/archive/%s.zip' % (MARKETPLACES_REPO, MARKETPLACES_COMMIT),
)
MARKETPLACES_MANIFEST = 'tree-manifest.sha256'
# ru-marketplace-mcp needs Python >= 3.12; Hermes brings only 3.11. uv downloads CPython from
# github.com/astral-sh/python-build-standalone (after its own releases.astral.sh mirror), so a
# blocked GitHub gets proxy retries. Safe: uv checks every managed-Python archive against the
# SHA-256 in its embedded download metadata, whatever URL served it (uv-python downloads.rs).
MARKETPLACES_PYTHON = '3.12'
PYTHON_BUILDS_URL = 'https://github.com/astral-sh/python-build-standalone/releases/download'
MARKETPLACES_PYTHON_MIRRORS = (None, 'https://ghproxy.net/' + PYTHON_BUILDS_URL, 'https://gh-proxy.com/' + PYTHON_BUILDS_URL)
MARKETPLACES_PYTHON_ATTEMPT_TIMEOUT = 240
# Inherited settings that would redirect the download or replace uv's hash source.
UV_ENV_DROP = ('VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT', 'PYTHONHOME', 'PYTHONPATH',
               'UV_PYTHON_INSTALL_MIRROR', 'UV_PYTHON_DOWNLOADS_JSON_URL', 'UV_PYTHON_DOWNLOADS')
MARKETPLACES_SOURCES = 'ozon,avito,yandex_market,detsky_mir,compare'
MARKETPLACES_ENTRY = 'marketplace_connector.__main__:main'
MARKETPLACES_LAUNCHER = ('direct_launcher.py', 'direct_common.py', 'direct_proxy.py', 'wb_browser_mcp.py')
# Wildberries: the owner's browser/CDP server (upstream WB connector returns
# no_results), second stdio MCP from the same venv and launcher.
WILDBERRIES_NAME = 'wildberries'
WILDBERRIES_SCRIPT = 'wb_browser_mcp.py'
MARKETPLACES_ZIP_LIMIT = 64 * 1024 * 1024
MARKETPLACES_DOWNLOAD_TIMEOUT = 120
# The worker gives this whole mode 900 s; Python download + sync share this budget.
MARKETPLACES_SYNC_TIMEOUT = 660
MARKETPLACES_PROBE_TIMEOUT = 120
MARKER = '.hermes-installed'


def marketplaces_paths(home):
    root = Path(home) / 'mcp'
    return {'repo': root / 'ru-marketplace-mcp', 'launcher': root / 'marketplaces',
            'state': root / 'marketplaces' / 'state'}


def marketplaces_entry(python, launcher, state):
    return {'command': str(python),
            'args': [str(Path(launcher) / 'direct_launcher.py'), '-e', MARKETPLACES_ENTRY],
            'env': {'MARKETPLACE_SOURCES': MARKETPLACES_SOURCES, 'MP_STATE_DIR': str(state)},
            'connect_timeout': 120,
            'enabled': True}


def wildberries_entry(python, launcher, state):
    return {'command': str(python),
            'args': [str(Path(launcher) / 'direct_launcher.py'), '-s', str(Path(launcher) / WILDBERRIES_SCRIPT)],
            'env': {'MP_STATE_DIR': str(state)},
            'connect_timeout': 120,
            'enabled': True}


def find_uv(home):
    """The uv Hermes installed: HERMES_HOME\\bin\\uv.exe (upstream Install-Uv),
    then PATH, then the astral default %USERPROFILE%\\.local\\bin\\uv.exe."""
    candidates = [Path(home) / 'bin' / 'uv.exe', shutil.which('uv')]
    if os.environ.get('USERPROFILE'):
        candidates.append(Path(os.environ['USERPROFILE']) / '.local' / 'bin' / 'uv.exe')
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def download_file(url, dest, limit=MARKETPLACES_ZIP_LIMIT, timeout=MARKETPLACES_DOWNLOAD_TIMEOUT):
    request = urllib.request.Request(url, headers={'User-Agent': 'hermes-installer'})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()))
    total = 0
    with opener.open(request, timeout=timeout) as response, open(dest, 'wb') as fh:
        if response.status != 200:
            raise Failure('EXTRAS', 'Архив поиска по маркетплейсам не скачался.')
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise Failure('EXTRAS', 'Архив поиска по маркетплейсам слишком большой.')
            fh.write(chunk)


def safe_extract(archive, dest):
    """Extract a GitHub zipball into dest; returns its single top-level folder.
    Rejects absolute paths, drive letters and '..' (zip-slip)."""
    import zipfile
    dest = Path(dest).resolve()
    with zipfile.ZipFile(archive) as zf:
        tops = set()
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            parts = [p for p in name.split('/') if p]
            if not parts or name.startswith('/') or ':' in parts[0] or '..' in parts:
                raise Failure('EXTRAS', 'Архив поиска по маркетплейсам повреждён.')
            target = (dest / Path(*parts)).resolve()
            if dest not in target.parents and target != dest:
                raise Failure('EXTRAS', 'Архив поиска по маркетплейсам повреждён.')
            tops.add(parts[0])
        if len(tops) != 1:
            raise Failure('EXTRAS', 'Архив поиска по маркетплейсам повреждён.')
        zf.extractall(dest)
    return dest / tops.pop()


def read_tree_manifest(path, commit):
    """{relative posix path: sha256} from a tools/tree_manifest.py manifest of `commit`."""
    expected, manifest_commit, declared = {}, None, None
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        lines = []
    for line in lines:
        if line.startswith('# commit '):
            manifest_commit = line[len('# commit '):]
        elif line.startswith('# files '):
            declared = line[len('# files '):]
        elif not line.startswith('#'):
            digest, sep, rel = line.partition('  ')
            if not sep or len(digest) != 64 or not rel or rel in expected:
                raise Failure('EXTRAS', 'Список проверенных файлов поиска по маркетплейсам повреждён. Скачайте пакет заново.')
            expected[rel] = digest
    if manifest_commit != commit or declared != str(len(expected)) or not expected:
        raise Failure('EXTRAS', 'Список проверенных файлов поиска по маркетплейсам повреждён. Скачайте пакет заново.')
    return expected


def verify_tree(root, expected):
    """None when root holds exactly the expected files, byte for byte; otherwise a reason."""
    import hashlib
    root = Path(root)
    matched = 0
    for folder, _, names in os.walk(root):
        for name in names:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            want = expected.get(rel)
            if want is None:
                return 'unexpected file ' + rel
            if hashlib.sha256(path.read_bytes()).hexdigest() != want:
                return 'content mismatch ' + rel
            matched += 1
    if matched != len(expected):
        return '%d file(s) missing' % (len(expected) - matched)
    return None


def fetch_verified(fetch, work, expected):
    """Try every source in order; return the extracted top folder of the first one whose
    tree matches the manifest. A failed download or a wrong tree moves on to the next."""
    tampered = False
    for index, url in enumerate(MARKETPLACES_ZIP_SOURCES):
        archive, out = work / 'repo.zip', work / ('x%d' % index)
        try:
            fetch(url, archive)
            top = safe_extract(archive, out)
        except Exception:
            shutil.rmtree(out, ignore_errors=True)
            continue
        if verify_tree(top, expected) is None:
            return top
        tampered = True
        shutil.rmtree(out, ignore_errors=True)
    if tampered:
        raise Failure('EXTRAS', 'Скачанный код поиска по маркетплейсам не совпал с проверенной версией. Установка остановлена.')
    raise Failure('EXTRAS', 'Архив поиска по маркетплейсам не скачался.')


def ensure_python(uv, cwd, env, runner, deadline):
    """Install the managed CPython once: as is (releases.astral.sh, then github.com), then
    through the GitHub proxies. uv verifies the archive hash on every route."""
    for mirror in MARKETPLACES_PYTHON_MIRRORS:
        remaining = deadline - time.monotonic()
        if remaining < 30:
            return False
        attempt_env = dict(env)
        if mirror:
            attempt_env['UV_PYTHON_INSTALL_MIRROR'] = mirror
        try:
            code = runner([uv, 'python', 'install', '--no-config', MARKETPLACES_PYTHON], cwd,
                          min(remaining, MARKETPLACES_PYTHON_ATTEMPT_TIMEOUT), attempt_env)
        except subprocess.TimeoutExpired:
            code = None
        if code == 0:
            return True
    return False


def run_quiet(cmd, cwd, timeout, env=None):
    """Run a helper without touching our stdout (it carries the JSON report)."""
    result = subprocess.run([str(c) for c in cmd], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode


def install_marketplaces(home, assets, fetch=None, runner=None, uv=None, manifest=None):
    """Download the pinned repo, sync its venv, copy the launcher.
    Returns (venv_python, launcher_dir, state_dir). Raises on any failure.
    Idempotent: a complete install of the same commit is reused as is."""
    fetch = fetch or download_file
    runner = runner or run_quiet
    manifest = manifest or Path(assets) / 'marketplaces' / MARKETPLACES_MANIFEST
    paths = marketplaces_paths(home)
    repo, launcher, state = paths['repo'], paths['launcher'], paths['state']
    python = repo / '.venv' / 'Scripts' / 'python.exe'
    marker = repo / MARKER
    done = marker.is_file() and marker.read_text(encoding='utf-8').strip() == MARKETPLACES_COMMIT and python.is_file()
    if not done:
        if repo.exists():
            # Only ever remove a folder this step created (it carries the marker).
            if not marker.is_file():
                raise Failure('EXTRAS', 'Папка поиска по маркетплейсам занята чужими файлами.')
            shutil.rmtree(repo)
        uv = uv or find_uv(home)
        if not uv:
            raise Failure('EXTRAS', 'Не найден uv для поиска по маркетплейсам.')
        expected = read_tree_manifest(manifest, MARKETPLACES_COMMIT)
        # Short work dir: HOME\mcp\.dl-XXXXXXXX\x0\ru-marketplace-mcp-<40 hex>\ plus the
        # deepest repo path (79 chars) stays ~220 chars with a 32-char profile name. The old
        # '.download-<32 hex>\x' form reached ~250, too close to MAX_PATH (260).
        work = repo.parent / ('.dl-' + uuid.uuid4().hex[:8])
        work.mkdir(parents=True)
        try:
            top = fetch_verified(fetch, work, expected)
            if not (top / 'uv.lock').is_file() or not (top / 'packages' / 'marketplace-connector').is_dir():
                raise Failure('EXTRAS', 'Архив поиска по маркетплейсам неполный.')
            os.replace(top, repo)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        # Marker first ('pending'): a later retry may remove this folder as ours.
        marker.write_text('pending', encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k.upper() not in UV_ENV_DROP}
        env['UV_PYTHON_PREFERENCE'] = 'managed'
        deadline = time.monotonic() + MARKETPLACES_SYNC_TIMEOUT
        ensure_python(uv, repo, env, runner, deadline)
        # The interpreter step owns every download route; sync must not start a second,
        # proxy-less download (it can still use a system 3.12 if the install failed).
        sync_env = dict(env, UV_PYTHON_DOWNLOADS='never')
        try:
            code = runner([uv, 'sync', '--frozen', '--no-dev', '--package', 'marketplace-connector', '--python', MARKETPLACES_PYTHON],
                          repo, max(30, deadline - time.monotonic()), sync_env)
        except subprocess.TimeoutExpired:
            code = None
        if code != 0 or not python.is_file():
            raise Failure('EXTRAS', 'Не удалось установить зависимости поиска по маркетплейсам.')
        marker.write_text(MARKETPLACES_COMMIT, encoding='utf-8')
    source = Path(assets) / 'marketplaces'
    launcher.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    for name in MARKETPLACES_LAUNCHER:
        payload = (source / name).read_bytes()
        target = launcher / name
        if not target.is_file() or target.read_bytes() != payload:
            atomic_write(target, payload)
    return python, launcher, state


# Same allowlist Hermes applies to stdio MCP children (tools/mcp_tool.py
# _build_safe_env): the probe must see the environment Hermes will give it.
_SAFE_ENV = {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TERM', 'SHELL', 'TMPDIR'}
_SAFE_ENV_CI = {'ALLUSERSPROFILE', 'APPDATA', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)', 'COMMONPROGRAMW6432',
                'COMPUTERNAME', 'COMSPEC', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'NUMBER_OF_PROCESSORS', 'OS',
                'PATHEXT', 'PROCESSOR_ARCHITECTURE', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
                'PUBLIC', 'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 'USERDOMAIN', 'USERNAME', 'USERPROFILE', 'WINDIR'}


def stdio_env(extra):
    env = {k: v for k, v in os.environ.items()
           if k in _SAFE_ENV or k.upper() in _SAFE_ENV_CI or k.startswith('XDG_')}
    env.update(extra or {})
    return env


class StdioSession:
    """Minimal newline-delimited JSON-RPC client for a stdio MCP server."""

    def __init__(self, command, args, env, cwd=None):
        import queue
        import threading
        self.proc = subprocess.Popen([str(command)] + [str(a) for a in args], env=stdio_env(env), cwd=cwd,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.lines = queue.Queue()
        self.next_id = 0

        def pump():
            for raw in self.proc.stdout:
                self.lines.put(raw)
            self.lines.put(None)
        threading.Thread(target=pump, daemon=True).start()

    def send(self, message):
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
        self.proc.stdin.flush()

    def request(self, method, params, timeout):
        import queue
        import time
        self.next_id += 1
        ident = self.next_id
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            try:
                raw = self.lines.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(method)
            if raw is None:
                raise EOFError(method)
            try:
                message = json.loads(raw.decode('utf-8', errors='replace'))
            except ValueError:
                continue
            if isinstance(message, dict) and message.get('id') == ident and 'method' not in message:
                if 'error' in message:
                    raise RuntimeError(str(message['error'])[:200])
                return message.get('result') or {}

    def initialize(self, timeout):
        result = self.request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                             'clientInfo': {'name': 'hermes-installer', 'version': '1.0'}}, timeout)
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return result

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            # The whole tree: a server may have spawned helpers.
            subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass


def probe_stdio(command, args, env, timeout=MARKETPLACES_PROBE_TIMEOUT):
    """initialize + tools/list over stdio. Returns tool names ([] on any failure)."""
    import time
    session = None
    try:
        deadline = time.monotonic() + timeout
        session = StdioSession(command, args, env)
        session.initialize(timeout)
        listed = session.request('tools/list', {}, max(5, deadline - time.monotonic()))
        return [t.get('name') for t in listed.get('tools') or [] if isinstance(t, dict) and t.get('name')]
    except Exception:
        return []
    finally:
        if session is not None:
            session.close()


def _marketplaces_tools_ok(tools):
    return any(str(name).endswith('_search') or str(name) == 'compare_prices' for name in tools or [])


def _wildberries_tools_ok(tools):
    return 'wb_search' in [str(name) for name in tools or []]


def step_marketplaces(home, assets, installer=None, probe=None):
    """{'marketplaces': status, 'wildberries': status}, each 'exists' | 'added'
    | 'failed'. Each server is independent: never written unless it answered
    initialize + tools/list with its search tool; an existing entry (ours or
    the user's) is never touched. One config write for both."""
    installer = installer or install_marketplaces
    probe = probe or probe_stdio
    config_path, before, src, cfg, servers = read_mcp_config(home)
    if servers is None:
        return {MARKETPLACES_NAME: 'failed', WILDBERRIES_NAME: 'failed'}
    wanted = [(MARKETPLACES_NAME, marketplaces_entry, _marketplaces_tools_ok),
              (WILDBERRIES_NAME, wildberries_entry, _wildberries_tools_ok)]
    result = {name: 'exists' for name, _, _ in wanted if name in servers}
    missing = [item for item in wanted if item[0] not in servers]
    if not missing:
        return result
    python, launcher, state = installer(home, assets)
    additions = {}
    for name, make_entry, tools_ok in missing:
        entry = make_entry(python, launcher, state)
        try:
            tools = probe(entry['command'], entry['args'], entry['env'])
        except Exception:
            tools = []
        if tools_ok(tools):
            additions[name] = entry
        else:
            result[name] = 'failed'
    if additions:
        status = 'added' if commit_mcp_additions(config_path, before, src, cfg, servers, additions) else 'failed'
        for name in additions:
            result[name] = status
    return result


def main_marketplaces(home, assets, installer=None, probe=None):
    """'ok' follows the main marketplaces server (the worker reads
    'marketplaces'); Wildberries is reported next to it and never fails the step."""
    try:
        result = step_marketplaces(Path(home), Path(assets), installer=installer, probe=probe)
    except Exception:
        result = {}
    status = result.get(MARKETPLACES_NAME, 'failed')
    return {'ok': status != 'failed', 'marketplaces': status,
            'wildberries': result.get(WILDBERRIES_NAME, 'failed')}


def _retune_default(home, section, key, old, new):
    """Change Hermes' own default for ONE direct child key of a top-level section.

    Only the exact default value is changed; any other value is the user's choice
    and is kept. Text-preserving (quote style, trailing comment, CRLF), limited
    to the section's direct children (other sections may reuse the key name),
    verified by re-parsing that nothing else changed, rolled back otherwise.
    """
    import copy
    import re
    import yaml
    config_path = Path(home) / 'config.yaml'
    if not config_path.exists():
        return 'kept'
    before = config_path.read_bytes()
    src = before.decode('utf-8-sig')
    cfg = yaml.safe_load(src) or {}
    block = cfg.get(section) if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or block.get(key) != old:
        return 'kept'
    lines = src.splitlines(keepends=True)
    head = re.compile(r'^' + re.escape(section) + r'[ \t]*:')
    starts = [i for i, l in enumerate(lines) if head.match(l)]
    if len(starts) != 1:
        return 'kept'
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if re.match(r'^[^\s#]', lines[i])), len(lines))
    child = next((re.match(r'^([ \t]+)', lines[i]).group(1) for i in range(start + 1, end)
                  if lines[i].strip() and not lines[i].lstrip().startswith('#')), None)
    if not child:
        return 'kept'
    pat = re.compile(r'^(' + re.escape(child) + re.escape(key) + r'[ \t]*:[ \t]*)(["\']?)'
                     + re.escape(old) + r'\2([ \t]*(?:#[^\r\n]*)?\r?\n?)$')
    hits = [i for i in range(start + 1, end) if pat.match(lines[i])]
    if len(hits) != 1:
        return 'kept'
    lines[hits[0]] = pat.sub(lambda m: m.group(1) + m.group(2) + new + m.group(2) + m.group(3), lines[hits[0]])
    candidate = ''.join(lines)
    expected = copy.deepcopy(cfg)
    expected[section][key] = new
    if (config_path.read_bytes() if config_path.exists() else None) != before:
        return 'failed'
    try:
        atomic_write(config_path, candidate.encode('utf-8'))
        if yaml.safe_load(config_path.read_text(encoding='utf-8')) != expected:
            raise Failure('EXTRAS', 'Проверка настройки не прошла; выполняется откат.')
    except Exception:
        rollback_config(config_path, before)
        return 'failed'
    return 'set'


def step_busy_mode(home):
    """display.busy_input_mode: interrupt -> steer (phone users write "ты тут?").

    Live Telegram 2026-09-23: each new message interrupted a long task and the
    gateway auto-resumed it, so it restarted in a loop.
    """
    return _retune_default(home, 'display', 'busy_input_mode', 'interrupt', 'steer')


def step_stt_language(home):
    """stt.language: "en" -> "ru" for a Russian-first pack.

    Live Telegram 2026-09-23: Hermes' default "en" forced Russian voice notes
    into English ("Tell me, are you crazy?"). Auto-detect ("") misfires on
    2-3 second clips, so the pack sets Russian explicitly.
    """
    return _retune_default(home, 'stt', 'language', 'en', 'ru')


API_RETRIES = 7


def step_api_retries(home, repo, runner=None):
    """agent.api_max_retries: unset -> 7, through Hermes' own `config set`.

    Live 2026-09-23: GWarden answered nginx 502 for a few seconds twice; the
    default 3 attempts (~8 s of backoff) ended the turn mid-task ("the clock
    icon"). Long prompts were not the cause (180k tokens -> HTTP 200). Seven
    attempts wait ~2 min (2, 4, 8, 16, 32, 60 s) before giving up. A value the
    user set is kept; anything else in config.yaml must stay byte-for-byte
    equal after parsing, else rollback.
    """
    import copy
    import subprocess
    import yaml
    config_path = Path(home) / 'config.yaml'
    if not config_path.exists():
        return 'kept'
    before = config_path.read_bytes()
    cfg = yaml.safe_load(before.decode('utf-8-sig')) or {}
    agent = cfg.get('agent') if isinstance(cfg, dict) else None
    if agent is not None and not isinstance(agent, dict):
        return 'kept'
    if isinstance(agent, dict) and agent.get('api_max_retries') is not None:
        return 'kept'
    if runner is None:
        if not (Path(repo) / 'hermes_cli' / 'main.py').is_file():
            return 'kept'   # not a Hermes checkout (offline tests)
        def runner():
            env = dict(os.environ, HERMES_HOME=str(home))
            subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'config', 'set',
                            'agent.api_max_retries', str(API_RETRIES)],
                           cwd=str(repo), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120, check=False)
    expected = copy.deepcopy(cfg)
    expected.setdefault('agent', {})
    if expected['agent'] is None:
        expected['agent'] = {}
    expected['agent']['api_max_retries'] = API_RETRIES
    try:
        runner()
        after = yaml.safe_load(config_path.read_text(encoding='utf-8-sig')) or {}
        if after != expected:
            raise Failure('EXTRAS', 'Проверка настройки не прошла; выполняется откат.')
    except Exception:
        rollback_config(config_path, before)
        return 'failed'
    return 'set'


def main(home, repo, assets, probe=None):
    probe = probe or probe_initialize
    home = Path(home)
    result = {'ok': True, 'soul': 'failed', 'skills': 'failed',
              'mcp': {name: 'failed' for name, _ in MCP_SERVERS}}
    try:
        result['soul'] = step_soul(home, assets, repo)
    except Exception:
        result['soul'] = 'failed'
    try:
        result['skills'] = step_skills(home, assets)
    except Exception:
        result['skills'] = 'failed'
    try:
        result['mcp'].update(step_mcp(home, probe))
    except Exception:
        pass
    try:
        result['busy'] = step_busy_mode(home)
    except Exception:
        result['busy'] = 'failed'
    try:
        result['stt_language'] = step_stt_language(home)
    except Exception:
        result['stt_language'] = 'failed'
    try:
        result['api_retries'] = step_api_retries(home, repo)
    except Exception:
        result['api_retries'] = 'failed'
    # ok = the set landed; deliberate skips (unreachable/kept/exists) are fine,
    # any 'failed' surfaces worker.ps1's "set incomplete" line.
    result['ok'] = (result['soul'] != 'failed' and result['skills'] != 'failed'
                    and result.get('busy') != 'failed' and result.get('stt_language') != 'failed'
                    and result.get('api_retries') != 'failed'
                    and all(v != 'failed' for v in result['mcp'].values()))
    return result


if __name__ == '__main__':
    code = 0
    try:
        # Fixed envelope even if stdout starts on a non-UTF-8 Windows locale.
        sys.stdout.reconfigure(encoding='utf-8')
        if sys.argv[4:5] == ['--marketplaces']:
            # Own worker step with its own budget: download + uv sync can take minutes.
            output = main_marketplaces(Path(sys.argv[1]), Path(sys.argv[3]))
        else:
            output = main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    except Exception:
        output = {'ok': False, 'code': 'EXTRAS',
                  'message': 'Не удалось настроить набор. Hermes работает; повторите позже.'}
        code = 1
    print(json.dumps(output, ensure_ascii=False), flush=True)
    os._exit(code)
