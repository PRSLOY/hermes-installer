"""MCP server: Wildberries product/price search driven through the user's real Chrome.

Why: WB blocks non-browser clients (403 on search.wb.ru/card.wb.ru, 498 on the site)
but renders fine in a logged-in Chrome, which carries the anti-bot token. So this
server opens a tab in the already-running Chrome (CDP on 127.0.0.1:9222), lets the
site do its own search, and reads the rendered product cards.

Tools:
    wb_search(query, limit, page)  - products with prices from the WB search page
    wb_browser_status()            - whether the CDP browser is reachable

Vendored from the owner's mcp-marketplaces/wb_browser_mcp.py (verified live
2026-09-23: 40 results with real prices through the warmed profile). Only the
configuration is made portable; the search logic is unchanged. It shares the
ru-marketplace-mcp scraping browser (same CDP port and profile), so one
`direct_launcher.py --warmup` covers Wildberries too. Launched through
direct_launcher.py -s, so with a VPN the autostarted browser gets the direct
proxy (CHROME_BINARY is then the launcher's browser-direct.cmd wrapper).

Env (first set wins):
    CDP port: WB_CDP_PORT, CHROME_CDP_PORT, 9222
    browser:  WB_CDP_CHROME, CHROME_BINARY, Chrome/Edge in the standard places
    profile:  WB_CDP_PROFILE, CHROME_SCRAPING_PROFILE, %LOCALAPPDATA%\\Chrome-Scraping

stdlib + websockets (present in the ru-marketplace-mcp venv). Nothing but
JSON-RPC goes to stdout.
"""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websockets  # noqa: E402

CDP_HOST = "127.0.0.1"
SERVER_NAME = "wb-browser"
SERVER_VERSION = "1.0.0"


def _first(env, *names):
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_port(env=None):
    env = os.environ if env is None else env
    try:
        port = int(_first(env, "WB_CDP_PORT", "CHROME_CDP_PORT") or 9222)
    except ValueError:
        return 9222
    return port if 0 < port < 65536 else 9222


def resolve_chrome(env=None, exists=os.path.exists):
    """WB_CDP_CHROME, then CHROME_BINARY (the launcher's choice, possibly its
    proxy wrapper), then the launcher's own Chrome/Edge lookup."""
    env = os.environ if env is None else env
    chosen = _first(env, "WB_CDP_CHROME", "CHROME_BINARY")
    if chosen:
        return chosen
    try:
        from direct_common import browser_candidates
        candidates = browser_candidates(env)
    except ImportError:
        candidates = []
    for path in candidates:
        if exists(path):
            return path
    return candidates[0] if candidates else "chrome.exe"


def resolve_profile(env=None):
    env = os.environ if env is None else env
    return _first(env, "WB_CDP_PROFILE", "CHROME_SCRAPING_PROFILE") or os.path.join(
        env.get("LOCALAPPDATA", "") or os.path.expanduser("~"), "Chrome-Scraping")


CDP_PORT = resolve_port()
CHROME = resolve_chrome()
PROFILE = resolve_profile()

SEARCH_URL = "https://www.wildberries.ru/catalog/0/search.aspx?search={q}"

EXTRACT_JS = """
(() => {
  const cards = [...document.querySelectorAll('article.product-card')].slice(0, %d);
  const items = cards.map(c => {
    const a = c.querySelector('a[href*="/catalog/"]');
    const href = a ? a.href : '';
    const m = href.match(/catalog\\/(\\d+)\\/detail/);
    const nameEl = c.querySelector('[class*="productName--"]') || c.querySelector('.product-card__brand-wrap');
    const brandEl = c.querySelector('[class*="brand--"]');
    const priceEl = c.querySelector('.price__lower-price') || c.querySelector('ins');
    const txt = c.innerText || '';
    const rating = (txt.match(/(\\d+[.,]\\d+)\\s*[\\n\\s]*[·•]/) || [])[1] || null;
    const reviews = (txt.match(/[·•]\\s*([\\d\\s\\u00a0]+)\\s*оцен/) || [])[1] || null;
    return {
      nm_id: m ? m[1] : null,
      url: href.split('?')[0],
      name: nameEl ? nameEl.innerText.replace(/\\n/g, ' ').trim() : '',
      brand: brandEl ? brandEl.innerText.trim() : '',
      price: priceEl ? priceEl.innerText.replace(/\\s+/g, ' ').trim() : '',
      rating: rating ? rating.replace(',', '.') : null,
      reviews: reviews ? reviews.replace(/\\D/g, '') : null,
    };
  });
  return JSON.stringify({found: document.querySelectorAll('article.product-card').length, items: items});
})()
"""

TOOLS = [
    {
        "name": "wb_search",
        "description": (
            "Search Wildberries products with prices by running the query in the user's "
            "real Chrome (WB blocks non-browser clients). Returns product cards: nm_id, "
            "name, brand, price, rating, review count and URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text, e.g. 'датчик протечки zigbee'"},
                "limit": {"type": "integer", "description": "Max products to return (default 30, max 100)"},
                "page": {"type": "integer", "description": "Result page (default 1)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wb_browser_status",
        "description": "Check whether the CDP-controlled browser is reachable and which version it is.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def log(msg):
    sys.stderr.write("[wb-browser] %s\n" % msg)
    sys.stderr.flush()


def http_json(path):
    with urllib.request.urlopen("http://%s:%d%s" % (CDP_HOST, CDP_PORT, path), timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def cdp_ready():
    try:
        http_json("/json/version")
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_browser(timeout=25.0):
    """Start the scraping Chrome with CDP if it is not already up."""
    if cdp_ready():
        return True
    if not os.path.exists(CHROME):
        raise RuntimeError("CDP browser is not reachable and Chrome was not found at %s" % CHROME)
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [
            CHROME,
            "--remote-debugging-port=%d" % CDP_PORT,
            "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=%s" % PROFILE,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--window-position=-32000,-32000",
            "--window-size=1280,720",
            "--start-minimized",
            "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_ready():
            return True
        time.sleep(0.5)
    raise RuntimeError("started Chrome but CDP on %s:%d did not come up" % (CDP_HOST, CDP_PORT))


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def cmd(self, method, params=None, session=None, timeout=90):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("id") == mid:
                return data


async def search_async(query, limit, page):
    limit = max(1, min(int(limit or 30), 100))
    page = max(1, int(page or 1))
    url = SEARCH_URL.format(q=urllib.parse.quote(query))
    if page > 1:
        url += "&page=%d" % page

    ensure_browser()
    ver = http_json("/json/version")
    async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        cdp = Cdp(ws)
        created = await cdp.cmd("Target.createTarget", {"url": "about:blank"})
        target_id = created["result"]["targetId"]
        try:
            attached = await cdp.cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session = attached["result"]["sessionId"]
            await cdp.cmd("Page.enable", session=session)
            await cdp.cmd("Page.navigate", {"url": url}, session=session)

            deadline = 40
            waited = 0.0
            while waited < deadline:
                await asyncio.sleep(1.0)
                waited += 1.0
                probe = await cdp.cmd(
                    "Runtime.evaluate",
                    {"expression": "document.querySelectorAll('article.product-card').length", "returnByValue": True},
                    session=session,
                )
                if probe.get("result", {}).get("result", {}).get("value"):
                    break

            res = await cdp.cmd(
                "Runtime.evaluate",
                {"expression": EXTRACT_JS % limit, "returnByValue": True},
                session=session,
            )
            value = res.get("result", {}).get("result", {}).get("value")
            if value is None:
                raise RuntimeError("could not read the WB page: %s" % json.dumps(res)[:300])
            data = json.loads(value)
        finally:
            try:
                await cdp.cmd("Target.closeTarget", {"targetId": target_id})
            except Exception:  # noqa: BLE001
                pass

    return {
        "query": query,
        "page": page,
        "found": data.get("found"),
        "returned": len(data.get("items") or []),
        "source": "wildberries (browser/CDP)",
        "items": data.get("items") or [],
    }


async def handle_tool(name, args):
    if name == "wb_search":
        query = (args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        result = await search_async(query, args.get("limit"), args.get("page"))
        return json.dumps(result, ensure_ascii=False, indent=2)
    if name == "wb_browser_status":
        try:
            ver = http_json("/json/version")
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {
                    "reachable": False,
                    "error": str(exc),
                    "hint": "start Chrome with --remote-debugging-port=%d" % CDP_PORT,
                },
                ensure_ascii=False,
            )
        return json.dumps({"reachable": True, "browser": ver.get("Browser")}, ensure_ascii=False)
    raise ValueError("unknown tool: %s" % name)


def respond(mid, result):
    _write({"jsonrpc": "2.0", "id": mid, "result": result})


def respond_error(mid, code, message):
    _write({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def _write(obj):
    # MCP stdio is UTF-8; the Windows console default (cp1251) would mangle it.
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main():
    inbox = queue.Queue()

    def reader():
        try:
            for raw in sys.stdin.buffer:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    inbox.put(line)
        finally:
            inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    log("ready (CDP %s:%d)" % (CDP_HOST, CDP_PORT))

    while True:
        line = inbox.get()
        if line is None:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            respond(
                mid,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        elif method == "tools/list":
            respond(mid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                text = asyncio.run(handle_tool(name, args))
                respond(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as exc:  # noqa: BLE001
                log("tool %s failed: %s" % (name, exc))
                respond(mid, {"content": [{"type": "text", "text": "error: %s" % exc}], "isError": True})
        elif method in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
            continue
        elif mid is not None:
            respond_error(mid, -32601, "method not found: %s" % method)


if __name__ == "__main__":
    main()
