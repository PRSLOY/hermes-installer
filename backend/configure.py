"""Real runtime/config stage. JSON in, one JSON result out. Never run in tests.
Only credential storage (.env) receives the key; probe sandbox has a protected ACL.
"""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from provider import Failure, base_url_candidates, select_model, configured_text, check_hermes_result, guard_runtime_http


def protect(path):
    ps = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(Path(__file__).with_name('protect.ps1')), str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise Failure('CONFIG', 'Не удалось защитить хранилище ключа правами Windows. Настройки не изменены.')


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


def authorize_checkpoint(home, repo):
    ps = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(Path(__file__).with_name('checkpoint.ps1')), str(home), str(repo)],
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or result.stdout.strip() != b'authorized':
        raise Failure('CONFIG', 'Нет действующего checkpoint установщика; настройки не изменены.')


def rollback(config_path, before, env_path, env_before, changed_config, changed_env):
    """Best-effort restore of pre-write bytes; a failed write stays fail-closed."""
    if changed_config:
        if before is None: config_path.unlink(missing_ok=True)
        else: atomic_write(config_path, before)
    if changed_env:
        if env_before is None: env_path.unlink(missing_ok=True)
        else: atomic_write(env_path, env_before, secret=True)


def main(home, repo, data):
    import yaml
    from dotenv import dotenv_values
    # Validate unresolved paths first: resolve() would hide junctions.
    authorize_checkpoint(home, repo)
    home = Path(home).resolve()
    repo = Path(repo).resolve()
    base = base_url_candidates(data['endpoint'])[0]
    key = data['api_key']
    config_path, env_path = home/'config.yaml', home/'.env'
    before = config_path.read_bytes() if config_path.exists() else None
    env_before = env_path.read_bytes() if env_path.exists() else None
    src = (before or b'').decode('utf-8-sig')
    # Only byte-identical upstream template from this fresh install is replaceable.
    template = repo/'cli-config.yaml.example'
    shipped_template = Path(__file__).parent/'upstream/cli-config.yaml.example'
    pristine = bool(template.exists() and before == template.read_bytes() == shipped_template.read_bytes())
    # Early conflict check precedes billable network traffic.
    old = (yaml.safe_load(src) or {}).get('model') or {}
    if old and not pristine and not (isinstance(old, dict) and old.get('api_key') == '${HERMES_SUBSCRIBER_API_KEY}' and old.get('base_url') == base):
        raise Failure('CONFIG', 'Найдены существующие настройки модели. Чтобы сохранить их, автоматическая настройка остановлена.')
    model = select_model(base, key, data.get('model') or '')
    candidate = configured_text(src, base, model, allow_template=pristine)
    env_text = (env_before or b'').decode('utf-8-sig')
    existing_key = dotenv_values(stream=io.StringIO(env_text)).get('HERMES_SUBSCRIBER_API_KEY')
    if existing_key is not None and existing_key != key:
        raise Failure('CONFIG', 'В хранилище уже другой ключ установщика. Автоматическая перезапись отключена для сохранности данных.')
    # python-dotenv single-quote escaping, not shell interpolation.
    quoted = "'" + key.replace('\\', '\\\\').replace("'", "\\'") + "'"
    env_candidate = env_text if existing_key is not None else env_text + ('\n' if env_text and not env_text.endswith('\n') else '') + 'HERMES_SUBSCRIBER_API_KEY=' + quoted + '\n'

    # Isolate verification from user's plugins, tools, memory, cron, sessions and rules.
    # This calls the installed, real AIAgent, not a synthetic HTTP-only success.
    with tempfile.TemporaryDirectory(prefix='.subscriber-check-', dir=home) as sandbox:
        protect(Path(sandbox))
        safe_cfg = {'model': yaml.safe_load(candidate)['model'], 'mcp_servers': {},
                    'memory': {'memory_enabled': False, 'user_profile_enabled': False},
                    'compression': {'enabled': False}, 'fallback_providers': []}
        Path(sandbox, 'config.yaml').write_text(yaml.safe_dump(safe_cfg), encoding='utf-8')
        os.environ['HERMES_HOME'] = sandbox
        os.environ['HERMES_SUBSCRIBER_API_KEY'] = key
        os.environ['HERMES_INTERACTIVE'] = '0'
        with guard_runtime_http(base, key):
            # Do not import the running host: explicitly prioritize the installed repo.
            sys.path.insert(0, str(repo))
            logging.disable(logging.CRITICAL)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                from hermes_cli.config import load_config
                from run_agent import AIAgent
                cfg = load_config()['model']
                if cfg.get('base_url') != base or cfg.get('api_key') != key or cfg.get('default') != model:
                    raise Failure('CONFIG', 'Hermes не распознал настройки API. Настройки не сохранены.')
                agent = AIAgent(api_key=cfg['api_key'], base_url=cfg['base_url'], model=cfg['default'],
                    provider='custom', api_mode='chat_completions', enabled_toolsets=[],
                    max_iterations=1, max_tokens=3000, quiet_mode=True, save_trajectories=False,
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    load_soul_identity=False, fallback_model=None, run_budget_seconds=90)
                try:
                    check_hermes_result(agent.run_conversation('Reply with one word: capital of Japan'))
                finally:
                    agent.close()
            captured.close()
    authorize_checkpoint(home, repo)
    # Concurrent external edits must not be lost; installer mutex only serializes us.
    if (config_path.read_bytes() if config_path.exists() else None) != before or (env_path.read_bytes() if env_path.exists() else None) != env_before:
        raise Failure('CONFIG', 'Настройки изменились во время проверки. Ничего не записано; повторите.')
    changed_env = changed_config = False
    try:
        atomic_write(env_path, env_candidate.encode('utf-8'), secret=True)
        changed_env = True
        atomic_write(config_path, candidate.encode('utf-8'))
        changed_config = True
        parsed = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        if parsed['model'] != yaml.safe_load(candidate)['model'] or dotenv_values(env_path).get('HERMES_SUBSCRIBER_API_KEY') != key:
            raise Failure('CONFIG', 'Контрольное чтение настроек не прошло; выполняется откат.')
    except Failure as exc:
        # Keep the specific reason (e.g. ACL/protect failure) instead of a generic one.
        rollback(config_path, before, env_path, env_before, changed_config, changed_env)
        raise
    except Exception:
        rollback(config_path, before, env_path, env_before, changed_config, changed_env)
        raise Failure('CONFIG', 'Не удалось сохранить настройки. Выполняется откат; проверьте доступ к папке Hermes.') from None
    return {'ok': True}


if __name__ == '__main__':
    try:
        # Reconfigure first so a decode failure also yields the fixed envelope.
        sys.stdin.reconfigure(encoding='utf-8-sig')
        sys.stdout.reconfigure(encoding='utf-8')
        data = json.load(sys.stdin)
        output = main(Path(sys.argv[1]), Path(sys.argv[2]), data)
        code = 0
    except Failure as exc:
        output = {'ok': False, 'code': exc.code, 'message': str(exc)}
        code = 1
    except Exception:
        # Never serialize exceptions: provider/SDK errors may contain credentials.
        output = {'ok': False, 'code': 'VERIFY', 'message': 'Не удалось проверить Hermes или сохранить настройки. Успех не подтверждён; проверьте совместимость официальной установки и повторите.'}
        code = 1
    print(json.dumps(output, ensure_ascii=False), flush=True)
    # Runtime extensions may own background executors; verified artifact must not hang.
    os._exit(code)
