"""Minimal safe adapter of scripts/hermes-add-provider.py (2026-09-10).
Retains candidate ordering, fake-answer detection and text-preserving YAML helpers.
No website discovery: credentials are sent ONLY to the exact user-supplied HTTPS
API base. No redirects, no provider error text or secrets in diagnostic output.
"""
import json
import urllib.request
import urllib.error
import http.client
from urllib.parse import urlsplit, urlunsplit

class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def tls_context():
    """TLS context for every installer HTTPS call.

    A fresh Windows lacks some CA roots (Go Daddy/Starfield: Telegram, Wolfram,
    Context7, DeepWiki) until the OS fetches them on demand; Python reads the
    store as-is and fails where PowerShell succeeds. certifi is pinned by Hermes
    itself (certifi==2026.5.20), so it is present in the venv we run under.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# Adapted from FAKE_MARKERS and probe_model in the original provider script.
FAKE_MARKERS = ('prevent abuse of free resource', 'insufficient', 'please recharge',
                'top up', 'quota', 'upgrade your plan', 'недостаточно средств')

def request(base, key, path, payload=None):
    base = base_url_candidates(base)[0]
    req = urllib.request.Request(base + path,
        data=None if payload is None else json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                 'User-Agent': 'hermes-agent/installer'})
    try:
        # Explicitly disable redirects; urllib otherwise forwards Authorization.
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context())).open(req, timeout=45) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return 200, None
            try:
                return response.status, json.loads(raw)
            except (ValueError, UnicodeError):
                return response.status, None
    except urllib.error.HTTPError as exc:
        # Never echo provider-controlled error bodies or headers (can contain keys).
        return exc.code, None
    except (OSError, ValueError, http.client.HTTPException):
        # Broken/garbage transport (proxies, truncated status line) is a transport
        # failure, not a provider verdict: retry, then NETWORK if it persists.
        return 0, None


# The first HTTPS call to a provider can be slow on a cold link: measured live in
# Windows Sandbox (2026-09-18), https://openrouter.ai/api/v1/models took 65 s the
# first time and ~1 s immediately after. A single attempt therefore reported a dead
# network where the provider would have answered -- a bad key surfaced as NETWORK
# instead of the expected 401/AUTH. Transport failures (status 0) are retried; a
# definitive provider verdict (any real HTTP status) is never retried.
TRANSPORT_ATTEMPTS = 3
# Live 2026-09-23: GWarden's nginx answered 502 for a few seconds at a time; the
# owner's GUI install failed its key check with NETWORK after ~20 s and passed on a
# manual retry. A gateway 502/503/504 means the upstream never produced an answer,
# so retrying it spends no quota. Pauses give the gateway time to come back.
RETRY_STATUSES = (0, 502, 503, 504)
RETRY_PAUSES = (3, 8)


def request_with_retry(base, key, path, payload=None, transport=request, sleep=None):
    """Request, retrying only 'no answer' outcomes: transport failure (status 0)
    and gateway 502/503/504, with short pauses in between.

    Every definitive provider answer -- 401/402/429, 500, any other status -- is
    returned on the first attempt so the caller's verdict stays exact and no quota
    is spent twice.
    """
    import time
    sleep = sleep or time.sleep
    status, body = (0, None)
    for attempt in range(TRANSPORT_ATTEMPTS):
        status, body = transport(base, key, path, payload)
        if status not in RETRY_STATUSES or attempt == TRANSPORT_ATTEMPTS - 1:
            return status, body
        sleep(RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)])
    return status, body

def check_status(status):
    if status in (401, 403):
        raise Failure('AUTH', 'Ключ отклонён. Проверьте ключ и разрешения API.')
    if status == 402:
        raise Failure('QUOTA', 'На ключе нет средств. Проверьте баланс в кабинете провайдера. Если токены подарочные, перенесите их на ключ (у Dahl — кнопка Allocate).')
    if status == 429:
        raise Failure('QUOTA', 'Провайдер перегружен или исчерпан лимит. Подождите минуту и повторите; если не помогло — проверьте баланс в кабинете провайдера.')
    if not status or status >= 500:
        raise Failure('NETWORK', 'API не отвечает. Проверьте подключение/VPN и повторите.')
    if status != 200:
        raise Failure('VERIFY', 'API отклонил запрос. Проверьте точный адрес API и имя модели. Перенаправления запрещены.')

def probe_model(base, key, model, transport=None):
    transport = transport or request
    status, body = request_with_retry(base, key, '/chat/completions', {
        'model': model, 'messages': [{'role': 'user', 'content': 'Reply with one word: capital of Japan'}],
        'max_tokens': 3000, 'stream': False}, transport=transport)
    check_status(status)
    try:
        content = body['choices'][0]['message']['content']
        if not isinstance(content, str) or not content.strip():
            raise ValueError()
        if 'fake' in str(body.get('id', '')).lower() or any(m in content.lower() for m in FAKE_MARKERS):
            raise Failure('QUOTA', 'API вернул заглушку вместо ответа. Проверьте баланс и доступ к модели.')
    except (KeyError, TypeError, IndexError, ValueError):
        raise Failure('VERIFY', 'API вернул пустой или некорректный ответ. Проверьте модель.') from None
    return model

def select_model(base, key, model):
    if model:
        return probe_model(base, key, model)
    status, body = request_with_retry(base, key, '/models')
    check_status(status)
    try:
        ids = [m['id'] for m in body['data'] if isinstance(m.get('id'), str) and m['id']]
    except (KeyError, TypeError, AttributeError):
        raise Failure('VERIFY', 'Каталог API некорректен. Укажите модель из инструкции провайдера.') from None
    # Original pick_candidates ordering, bounded to 3 billable probes.
    free = [i for i in ids if ':free' in i or i.endswith('-free')]
    candidates = (free + [i for i in ids if i not in free])[:3]
    last = Failure('VERIFY', 'Каталог пуст. Укажите модель из инструкции провайдера.')
    for candidate in candidates:
        try:
            return probe_model(base, key, candidate)
        except Failure as exc:
            last = exc
            if exc.code in ('AUTH', 'NETWORK', 'QUOTA'):
                raise
    raise last


def check_hermes_result(result):
    text = result.get('final_response')
    if (result.get('completed') is not True or result.get('failed') or result.get('partial')
            or result.get('interrupted') or not isinstance(text, str) or not text.strip()
            or any(m in text.lower() for m in FAKE_MARKERS)):
        raise Failure('VERIFY', 'Hermes не получил полный ответ модели. Настройки не сохранены; проверьте API и повторите.')


def configured_text(src, base, model, allow_template=False):
    """Retains original block_end/find_top_key text approach; validates YAML first.
    Only blank config or our exact previous model block is editable. An audited
    pristine upstream template may be accepted on the *fresh install* path.
    """
    import yaml
    cfg = yaml.safe_load(src) or {}
    if not isinstance(cfg, dict):
        raise Failure('CONFIG', 'Неверный config.yaml; существующие настройки не изменены.')
    old = cfg.get('model') or {}
    desired = {'default': model, 'provider': 'custom', 'base_url': base,
               'api_key': '${HERMES_SUBSCRIBER_API_KEY}', 'api_mode': 'chat_completions',
               'max_tokens': 3000}
    if old == desired:
        return src
    if old and not allow_template:
        raise Failure('CONFIG', 'Найдены существующие настройки модели. Установщик их не перезаписывает.')
    lines = src.splitlines(keepends=True)
    # Reused top-level block boundary logic from original provider script.
    starts = [i for i, line in enumerate(lines) if line.startswith('model:')]
    if len(starts) > 1:
        raise Failure('CONFIG', 'Повторяющийся блок model в config.yaml. Настройки не изменены.')
    block = 'model:\n' + ''.join('  ' + k + ': ' + json.dumps(v, ensure_ascii=False) + '\n' for k, v in desired.items())
    if starts:
        start = starts[0]
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0].isspace() or lines[end].lstrip().startswith('#')):
            end += 1
        while end > start + 1 and lines[end-1].lstrip().startswith('#'):
            end -= 1
        output = ''.join(lines[:start]) + block + ''.join(lines[end:])
    else:
        output = src + ('\n' if src and not src.endswith('\n') else '') + block
    result = yaml.safe_load(output)
    if result.get('model') != desired or {k:v for k,v in cfg.items() if k != 'model'} != {k:v for k,v in result.items() if k != 'model'}:
        raise Failure('CONFIG', 'Проверка сохранности config.yaml не прошла. Настройки не изменены.')
    return output


def guard_runtime_http(base, key):
    """Fail closed if the installed SDK would carry this key to another origin.
    Scoped to this short-lived verifier process; desktop uses upstream security.
    Guard lowest single-request method so automatic redirects are checked too.
    """
    import contextlib
    import httpx
    from unittest.mock import patch
    origin = urlsplit(base)
    allowed = (origin.scheme, origin.hostname, origin.port or 443)
    sync = httpx.Client._send_single_request
    async_send = httpx.AsyncClient._send_single_request
    def check(req):
        url = urlsplit(str(req.url))
        carries_key = any(key in value for value in req.headers.values())
        if carries_key and (url.scheme, url.hostname, url.port or 443) != allowed:
            raise Failure('VERIFY', 'Hermes попытался отправить ключ другому серверу. Запрос остановлен; настройки не сохранены.')
    def guarded(client, req):
        check(req)
        return sync(client, req)
    async def guarded_async(client, req):
        check(req)
        return await async_send(client, req)
    @contextlib.contextmanager
    def guard():
        with patch.object(httpx.Client, '_send_single_request', guarded), patch.object(httpx.AsyncClient, '_send_single_request', guarded_async):
            yield
    return guard()


def base_url_candidates(raw):
    value = raw.strip().rstrip('/')
    if value and '://' not in value:
        value = 'https://' + value
    u = urlsplit(value)
    if (u.scheme != 'https' or not u.hostname or u.username or u.password
            or u.query or u.fragment or any(c.isspace() for c in value)
            or '\\' in value
            or value != value.strip() or u.path.endswith('/')):
        raise ValueError('Укажите точный HTTPS-адрес API без пароля, параметров и #.')
    try:
        u.port
    except ValueError:
        raise ValueError('Неверный порт API.') from None
    if u.path in ('', '/'):
        raise ValueError('Это адрес сайта. Введите полный адрес API из документации провайдера (например /api/v1).')
    return [urlunsplit(u)]
