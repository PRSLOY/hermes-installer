"""Optional backup providers (issue #10): Hermes' own `fallback_providers` chain.

`fallbacks.py HOME` reads {"fallbacks": [{provider_id, endpoint, model, api_key}]}
(0-2 entries) on stdin and prints ONE JSON line:
    {"ok": bool, "results": [{"provider_id": id, "status": s}]}
with s in added | exists | auth | quota | network | verify | failed.

worker.ps1 runs it only after the primary provider is verified and written and
the out-of-box set ran; it is never fatal. Keys are never printed, logged or put
on a command line, provider bodies are never relayed (provider.py returns fixed
codes only) and exceptions are never serialized.

Each entry gets the same live check as the primary (provider.select_model). Only
verified entries are written:
  * .env: HERMES_SUBSCRIBER_FALLBACK_<n>_KEY='<key>' (python-dotenv quoting as in
    configure.py; protected atomic write);
  * config.yaml, top-level list: {provider: custom, model, base_url,
    api_mode: chat_completions, key_env: HERMES_SUBSCRIBER_FALLBACK_<n>_KEY}.
Hermes resolves `key_env` through agent.secret_scope.get_secret
(hermes_cli/fallback_config.py resolve_entry_api_key, pinned c712f06).

Both files are written together, re-read and compared with the expected parse
(every other config key and every other .env value unchanged), and both are
rolled back on any mismatch. An entry whose base_url is already in the chain is
'exists' and nothing is touched (idempotent re-run).
"""
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider import Failure, base_url_candidates, select_model

MAX_FALLBACKS = 2
ENV_NAME = 'HERMES_SUBSCRIBER_FALLBACK_{}_KEY'
KEY_RE = re.compile(r'^[\x21-\x7E]{8,8192}$')
ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
STATUS_BY_CODE = {'AUTH': 'auth', 'QUOTA': 'quota', 'NETWORK': 'network', 'VERIFY': 'verify'}
# Top-level key in any YAML-legal spelling (see extras.merge_mcp_text).
SECTION = re.compile(r'''^(["']?)fallback_providers\1[ \t]*:''')


def protect(path):
    ps = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(Path(__file__).with_name('protect.ps1')), str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise Failure('FALLBACK', 'Не удалось защитить хранилище запасных ключей правами Windows. Изменение отменено.')


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


def read_bytes(path):
    return path.read_bytes() if path.exists() else None


def env_values(data):
    from dotenv import dotenv_values
    return dict(dotenv_values(stream=io.StringIO((data or b'').decode('utf-8-sig'))))


def dotenv_quote(key):
    # python-dotenv single-quote escaping, not shell interpolation (configure.py).
    return "'" + key.replace('\\', '\\\\').replace("'", "\\'") + "'"


def normalized(url):
    return url.strip().rstrip('/').lower() if isinstance(url, str) else ''


def safe_id(raw):
    value = raw.get('provider_id') if isinstance(raw, dict) else None
    return value if isinstance(value, str) and ID_RE.match(value) else ''


def parse_entry(raw):
    """(base_url, model, key) for a well-formed entry, else None."""
    if not isinstance(raw, dict) or not safe_id(raw):
        return None
    key, model, endpoint = raw.get('api_key'), raw.get('model'), raw.get('endpoint')
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None
    model = '' if model is None else model
    if not isinstance(model, str) or len(model) > 256 or any(ord(c) < 32 for c in model):
        return None
    if not isinstance(endpoint, str):
        return None
    try:
        base = base_url_candidates(endpoint)[0]
    except ValueError:
        return None
    return base, model.strip(), key


def with_chain(src, chain):
    """config.yaml text with `fallback_providers` set to chain.

    Everything outside that one top-level section is kept byte-for-byte; the
    section itself is re-emitted by yaml.safe_dump (a comment inside it is
    lost, its data is not: the caller compares the whole parse)."""
    import yaml
    newline = '\r\n' if '\r\n' in src else '\n'
    block = yaml.safe_dump({'fallback_providers': chain}, sort_keys=False,
                           default_flow_style=False, allow_unicode=True).replace('\n', newline)
    lines = src.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if SECTION.match(line)]
    if len(starts) > 1:
        raise Failure('FALLBACK', 'Повторяющийся блок fallback_providers в config.yaml. Настройки не изменены.')
    if not starts:
        prefix = src if (not src or src.endswith('\n')) else src + newline
        return prefix + block
    start = starts[0]
    end = start + 1
    # The section: indented lines, blank/comment lines and a top-level "- item" list.
    while end < len(lines) and (not lines[end].strip() or lines[end][0].isspace()
                                or lines[end].startswith('#') or lines[end].startswith('- ')):
        end += 1
    # Trailing blank/comment lines belong to whatever follows.
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith('#')):
        end -= 1
    return ''.join(lines[:start]) + block + ''.join(lines[end:])


def rollback(config_path, before, env_path, env_before, changed_config, changed_env):
    """Best-effort restore of both files' pre-write bytes."""
    for path, data, changed, secret in ((config_path, before, changed_config, False),
                                        (env_path, env_before, changed_env, True)):
        if not changed:
            continue
        try:
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data, secret=secret)
        except Exception:
            pass


def free_slot(env, chain, key, taken):
    """First HERMES_SUBSCRIBER_FALLBACK_<n>_KEY not used by another entry.

    A slot already holding this very key is reused; a slot holding another
    value (e.g. a user's own) or referenced from the chain is never touched."""
    referenced = {str(e.get('key_env') or e.get('api_key_env') or '') for e in chain if isinstance(e, dict)}
    for n in range(1, MAX_FALLBACKS + 1):
        name = ENV_NAME.format(n)
        if name in taken or name in referenced:
            continue
        if name in env and env[name] != key:
            continue
        return name
    return None


def commit(home, before, env_before, src, cfg, chain, additions):
    """Write .env then config.yaml; True only if both landed and verified."""
    import yaml
    config_path, env_path = home / 'config.yaml', home / '.env'
    new_chain = copy.deepcopy(chain) + [entry for entry, _, _ in additions]
    expected = copy.deepcopy(cfg)
    expected['fallback_providers'] = new_chain
    candidate = with_chain(src, new_chain)
    if (yaml.safe_load(candidate) or {}) != expected:
        return False
    env_text = (env_before or b'').decode('utf-8-sig')
    env_old = env_values(env_before)
    lines = ''.join(name + '=' + dotenv_quote(key) + '\n' for _, name, key in additions if env_old.get(name) != key)
    env_candidate = env_text + ('\n' if lines and env_text and not env_text.endswith('\n') else '') + lines
    expected_env = dict(env_old)
    expected_env.update({name: key for _, name, key in additions})
    # Live checks can take minutes: never write over an edit made meanwhile.
    if read_bytes(config_path) != before or read_bytes(env_path) != env_before:
        return False
    changed_env = changed_config = False
    try:
        if lines:
            atomic_write(env_path, env_candidate.encode('utf-8'), secret=True)
            changed_env = True
        atomic_write(config_path, candidate.encode('utf-8'))
        changed_config = True
        if (yaml.safe_load(config_path.read_text(encoding='utf-8-sig')) or {}) != expected:
            raise Failure('FALLBACK', 'Контрольное чтение config.yaml не прошло; выполняется откат.')
        if env_values(env_path.read_bytes()) != expected_env:
            raise Failure('FALLBACK', 'Контрольное чтение хранилища ключей не прошло; выполняется откат.')
    except Exception:
        rollback(config_path, before, env_path, env_before, changed_config, changed_env)
        return False
    return True


def main(home, entries, probe=None):
    probe = probe or select_model
    home = Path(home)
    entries = entries if isinstance(entries, list) else []
    results = [{'provider_id': safe_id(raw), 'status': 'failed'} for raw in entries]
    if len(entries) > MAX_FALLBACKS:
        return {'ok': False, 'results': results[:MAX_FALLBACKS]}
    if not entries:
        return {'ok': True, 'results': []}
    try:
        import yaml
        before = read_bytes(home / 'config.yaml')
        env_before = read_bytes(home / '.env')
        src = (before or b'').decode('utf-8-sig')
        cfg = yaml.safe_load(src) or {}
        chain = cfg.get('fallback_providers') if isinstance(cfg, dict) else None
        if chain is None and isinstance(cfg, dict):
            chain = []
        if not isinstance(cfg, dict) or not isinstance(chain, list):
            return {'ok': False, 'results': results}
        env = env_values(env_before)
    except Exception:
        return {'ok': False, 'results': results}
    model_cfg = cfg.get('model')
    primary = normalized(model_cfg.get('base_url')) if isinstance(model_cfg, dict) else ''
    known = {normalized(e.get('base_url')) for e in chain if isinstance(e, dict)}
    requested, taken, additions, indexes = set(), set(), [], []
    for index, raw in enumerate(entries):
        parsed = parse_entry(raw)
        if parsed is None:
            continue
        base, model, key = parsed
        if normalized(base) in known:
            results[index]['status'] = 'exists'
            continue
        # The primary itself or a duplicate within this request is never a backup.
        if normalized(base) == primary or normalized(base) in requested:
            continue
        requested.add(normalized(base))
        try:
            model = probe(base, key, model)
        except Failure as exc:
            results[index]['status'] = STATUS_BY_CODE.get(exc.code, 'failed')
            continue
        except Exception:
            continue
        if not isinstance(model, str) or not model:
            continue
        name = free_slot(env, chain, key, taken)
        if name is None:
            continue
        taken.add(name)
        additions.append(({'provider': 'custom', 'model': model, 'base_url': base,
                           'api_mode': 'chat_completions', 'key_env': name}, name, key))
        indexes.append(index)
    if additions:
        try:
            landed = commit(home, before, env_before, src, cfg, chain, additions)
        except Exception:
            landed = False
        for index in indexes:
            results[index]['status'] = 'added' if landed else 'failed'
    return {'ok': all(r['status'] in ('added', 'exists') for r in results), 'results': results}


if __name__ == '__main__':
    code = 0
    entries = []
    try:
        # Reconfigure first so a decode failure also yields the fixed envelope.
        sys.stdin.reconfigure(encoding='utf-8-sig')
        sys.stdout.reconfigure(encoding='utf-8')
        data = json.load(sys.stdin)
        entries = data.get('fallbacks') if isinstance(data, dict) else []
        output = main(Path(sys.argv[1]), entries)
    except Exception:
        # Never serialize exceptions: provider/SDK errors may contain credentials.
        listed = entries if isinstance(entries, list) else []
        output = {'ok': False, 'results': [{'provider_id': safe_id(raw), 'status': 'failed'}
                                           for raw in listed[:MAX_FALLBACKS]]}
        code = 1
    entries = data = None
    print(json.dumps(output, ensure_ascii=False), flush=True)
    os._exit(code)
