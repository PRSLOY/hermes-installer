"""Launch the marketplaces MCP server, choosing the network path automatically.

Adapted from the owner's mcp-marketplaces/direct_launcher.py (Windows-only,
stdlib-only). Marketplaces block or throttle foreign exit IPs, so with a VPN on
their traffic must leave through the physical adapter:

  * No VPN/TUN detected  -> run the MCP directly: no proxy, no socket patch.
  * VPN/TUN detected      -> make sure a PERSISTENT detached forward proxy
    bound to the physical adapter serves a fixed 127.0.0.1 port (34567, reused
    when a compatible direct proxy already listens there, else 34568/34569;
    the choice is kept in <state>/proxy_port.txt), advertise it via
    HTTP(S)_PROXY, bind plain sockets to the physical source address, and
    point CHROME_BINARY at <state>/browser-direct.cmd so the browser the MCP
    autostarts carries --proxy-server too (the browser tier goes direct).
    If the persistent proxy cannot start, an in-process proxy serves the MCP's
    own traffic and the browser is started plain.
  * Direct path broken at startup (no physical address, bind fails, or a quick
    connect to ozon.ru:443 through the physical IP fails) -> fall back to the
    default route and log why. The MCP itself is always started.

Usage:
    python direct_launcher.py -e module:func [args...]
    python direct_launcher.py -m module [args...]
    python direct_launcher.py -s script.py [args...]   # e.g. wb_browser_mcp.py
    python direct_launcher.py --warmup            # visible browser for the anti-bot check
    python direct_launcher.py --decide            # print the decision (stderr)

Env:
    MP_STATE_DIR   writable dir for direct_ip.txt, launcher.log, proxy.log
    MP_PROXY_PORT  preferred local proxy port (default 34567, then 34568, 34569)
    MP_DIRECT_IP   pin the physical source address (skips auto-detection)
    MP_MODE        auto (default) | direct | proxy
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import direct_common as dc  # noqa: E402

LOG_NAME = 'launcher.log'


def log(message):
    dc.log(LOG_NAME, message)
    dc.stderr(message)


def plan():
    """Return (mode, source_ip, reason) with mode 'direct' or 'proxy'."""
    forced = os.environ.get('MP_MODE', 'auto').strip().lower()
    pinned = os.environ.get('MP_DIRECT_IP', '').strip()
    if forced == 'direct':
        return 'direct', '', 'MP_MODE=direct'
    net = dc.collect_network()
    if net is None:
        # Detection unavailable: the cached physical IP is still safe to try
        # (it is validated by direct_path_works before use).
        cached = pinned or dc.read_cached_ip()
        if cached and forced != 'direct':
            return 'proxy', cached, 'network detection failed; trying cached address'
        return 'direct', '', 'network detection failed'
    decision = dc.decide_route(net.get('adapters'), net.get('routes'), net.get('metrics'), net.get('addresses'))
    ip = pinned or decision['direct_ip']
    if ip:
        dc.write_cached_ip(ip)
    if forced == 'proxy' or decision['vpn']:
        return 'proxy', ip, decision['reason']
    return 'direct', '', decision['reason']


def open_listener():
    preferred = os.environ.get('MP_PROXY_PORT', str(dc.DEFAULT_PORT))
    from direct_proxy import bind_listener
    held = {}

    def can_bind(port):
        try:
            held['srv'] = bind_listener(port)
            return True
        except OSError:
            return False

    port = dc.choose_port(preferred, can_bind)
    if 'srv' in held:
        return held['srv']
    if port == 0:
        log('proxy port %s is busy; using a free port' % preferred)
    return bind_listener(0)


def use_proxy_env(port):
    url = 'http://127.0.0.1:%d' % port
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = url
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost,::1'
    os.environ.pop('ALL_PROXY', None)
    os.environ.pop('all_proxy', None)


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_SOURCE = {'ip': ''}


def _is_loopback(address):
    if not isinstance(address, tuple) or not address or not isinstance(address[0], str):
        return False
    host = address[0].strip('[]').lower()
    return host in ('localhost', '::1', '0.0.0.0', '') or host.startswith('127.')


def _bind(sock, address):
    ip = _SOURCE['ip']
    if not ip or sock.family != socket.AF_INET or _is_loopback(address):
        return
    try:
        sock.bind((ip, 0))
    except OSError:
        pass


def _connect(self, address):
    _bind(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _bind(self, address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _real_getaddrinfo(host, port, family, type, proto, flags)
    if _SOURCE['ip'] and family in (0, socket.AF_UNSPEC):
        v4 = [r for r in res if r[0] == socket.AF_INET]
        if v4:
            return v4
    return res


def patch_sockets(ip):
    _SOURCE['ip'] = ip
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.getaddrinfo = _getaddrinfo


def ensure_persistent_proxy(ip, spawn=None, wait=8.0):
    """Reuse or start the detached direct proxy on a fixed port.
    Returns (port, how) with port 0 on failure. Never raises."""
    import time
    here = os.path.dirname(os.path.abspath(__file__))

    def start(port):
        if spawn is not None:
            spawn(port, ip)
        else:
            import subprocess
            subprocess.run([sys.executable, os.path.join(here, 'direct_proxy.py'), '--daemon', str(port), ip],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if dc.port_listening(port):
                return dc.proxy_works(port)
            time.sleep(0.2)
        return False

    try:
        candidates = dc.proxy_port_candidates(os.environ.get('MP_PROXY_PORT'), dc.read_recorded_port())
        port, how = dc.pick_proxy_port(candidates, dc.proxy_port_state, start)
    except Exception as exc:  # noqa: BLE001
        return 0, 'persistent proxy failed: %s' % exc
    if port:
        dc.write_recorded_port(port)
    return port, how


def network_plan():
    """(mode, ip, reason): 'direct' (no VPN), 'fallback' (VPN, but the physical
    path is unusable) or 'proxy' (VPN and the physical path works). Never raises."""
    try:
        mode, ip, reason = plan()
    except Exception as exc:  # noqa: BLE001
        return 'direct', '', 'decision failed (%s)' % exc
    if mode == 'direct':
        return 'direct', '', reason
    if not ip:
        return 'fallback', '', '%s; no physical address' % reason
    ok, why = dc.direct_path_works(ip)
    if not ok:
        return 'fallback', ip, '%s; direct path via %s failed (%s)' % (reason, ip, why)
    return 'proxy', ip, reason


def prepare():
    """Pick and set up the network path. Never raises; returns a short status:
    'direct' | 'fallback' | 'persistent' (browser may use _SOURCE['port']) | 'proxy'
    (in-process only: dies with this MCP, so the browser is not pointed at it)."""
    mode, ip, reason = network_plan()
    if mode == 'direct':
        log('no VPN detected (%s): default route' % reason)
        return 'direct'
    if mode == 'fallback':
        log('VPN detected (%s): falling back to the default route' % reason)
        return 'fallback'
    port, how = ensure_persistent_proxy(ip)
    if port:
        use_proxy_env(port)
        patch_sockets(ip)
        _SOURCE['port'] = port
        log('VPN detected (%s): direct via %s, persistent proxy 127.0.0.1:%d (%s)' % (reason, ip, port, how))
        return 'persistent'
    log('persistent proxy unavailable (%s); in-process proxy for the MCP, browser without proxy' % how)
    try:
        from direct_proxy import start_proxy
        port = start_proxy(open_listener(), ip)
    except Exception as exc:  # noqa: BLE001
        log('VPN detected (%s) but the local proxy did not start (%s): falling back to the default route' % (reason, exc))
        return 'fallback'
    use_proxy_env(port)
    patch_sockets(ip)
    _SOURCE['port'] = port
    log('VPN detected (%s): direct via %s, proxy 127.0.0.1:%d' % (reason, ip, port))
    return 'proxy'


# Chrome first, then Edge (including Program Files (x86)); shared with wb_browser_mcp.
browser_candidates = dc.browser_candidates


def choose_browser(environ=None, exists=os.path.exists):
    """Set CHROME_BINARY for the MCP unless the user already chose one."""
    env = os.environ if environ is None else environ
    if env.get('CHROME_BINARY', '').strip():
        return env['CHROME_BINARY']
    for path in browser_candidates(env):
        if exists(path):
            env['CHROME_BINARY'] = path
            return path
    return ''


def wrap_browser_for_proxy(port, environ=None):
    """Point CHROME_BINARY at <state>/browser-direct.cmd (real browser +
    --proxy-server). Returns the wrapper path, or '' with the browser left plain."""
    env = os.environ if environ is None else environ
    browser = env.get('CHROME_BINARY', '')
    if not browser or browser.lower().endswith(('.cmd', '.bat')):
        return ''
    wrapper = dc.write_browser_wrapper(browser, port)
    if wrapper:
        env['MP_REAL_BROWSER'] = browser
        env['CHROME_BINARY'] = wrapper
    return wrapper


WARMUP_SITES = ('https://www.ozon.ru/', 'https://www.avito.ru/', 'https://www.wildberries.ru/')


def scraping_profile(environ=None):
    env = os.environ if environ is None else environ
    custom = env.get('CHROME_SCRAPING_PROFILE', '').strip() or env.get('WB_CDP_PROFILE', '').strip()
    return custom or os.path.join(env.get('LOCALAPPDATA', ''), 'Chrome-Scraping')


def warmup_args(browser, profile, port, proxy_port=0):
    """The MCP's own launch flags (CDP port + profile), but visible and on the
    marketplaces, so the person passes the anti-bot check once and the MCP
    later reuses this very profile/instance. With a VPN it carries the same
    --proxy-server as the MCP autostart, so the check is passed from the
    address the searches will come from."""
    extra = dc.proxy_flags(proxy_port) if proxy_port else []
    return [browser, '--remote-debugging-port=%s' % port, '--remote-debugging-address=127.0.0.1',
            '--user-data-dir=%s' % profile, '--no-first-run', '--no-default-browser-check'] + extra + list(WARMUP_SITES)


def scraping_browsers(profile):
    """Command lines of running main browser processes of OUR scraping profile."""
    import subprocess
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
          "Where-Object { ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') "
          "-and $_.CommandLine -like ('*--user-data-dir=' + $env:MP_PROFILE + '*') "
          "-and $_.CommandLine -notlike '* --type=*' } | ForEach-Object { $_.CommandLine }")
    env = dict(os.environ, MP_PROFILE=profile)
    out = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], env=env,
                         stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    return [line.strip() for line in out.decode('utf-8', errors='replace').splitlines() if line.strip()]


def unproxied(cmdlines, port):
    """Pure: the command lines that lack our --proxy-server flag."""
    flag = '--proxy-server=http://127.0.0.1:%d' % int(port)
    return [c for c in cmdlines if flag not in c]


def warn_if_unproxied_browser(port):
    """MCP path: a scraping browser already running without our proxy flag is
    reused over CDP and still goes through the VPN. Only log it; never kill a
    browser mid-session (the next --warmup restarts it with the flag)."""
    try:
        stale = unproxied(scraping_browsers(scraping_profile()), port)
        if stale:
            log('scraping browser already running WITHOUT --proxy-server=http://127.0.0.1:%d: it goes through '
                'the VPN until restarted (--warmup restarts it)' % port)
    except Exception as exc:  # noqa: BLE001
        log('running browser check failed (%s)' % exc)


def stop_scraping_browser(profile):
    """Close only browser processes of OUR scraping profile (a hidden,
    off-screen instance would swallow the visible window). Never the person's
    own browser."""
    import subprocess
    ps = ("Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') "
          "-and $_.CommandLine -like ('*--user-data-dir=' + $env:MP_PROFILE + '*') } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    env = dict(os.environ, MP_PROFILE=profile)
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                   env=env, capture_output=True, timeout=30, check=False)


def warmup():
    """`--warmup`: open Ozon, Avito and Wildberries in the MCP's browser profile, visibly."""
    import subprocess
    import time
    browser = choose_browser()
    if not browser:
        print('Не нашёл Chrome или Edge. Установите Google Chrome и повторите.')
        return 1
    profile = scraping_profile()
    port = os.environ.get('CHROME_CDP_PORT', '9222')
    proxy_port = 0
    mode, ip, reason = network_plan()
    if mode == 'proxy':
        proxy_port, how = ensure_persistent_proxy(ip)
        log('warmup: VPN detected (%s); browser proxy: %s' % (
            reason, '127.0.0.1:%d (%s)' % (proxy_port, how) if proxy_port else 'unavailable, ' + how))
    stop_scraping_browser(profile)
    time.sleep(1.5)
    subprocess.Popen(warmup_args(browser, profile, port, proxy_port), creationflags=0x00000008 | 0x00000200,
                     close_fds=True)
    log('warmup: opened Ozon, Avito and Wildberries in %s' % os.path.basename(browser))
    print('Открыл окно браузера с Ozon, Авито и Wildberries. Если сайт просит проверку «я не робот» — '
          'пройдите её, окно можно не закрывать. После этого повторите поиск.')
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        dc.stderr(__doc__)
        return 2
    if args[0] == '--decide':
        prepare()
        return 0
    if args[0] == '--warmup':
        return warmup()
    status = prepare()
    try:
        browser = choose_browser()
        log('browser for Ozon/Avito/WB: %s' % (os.path.basename(browser) if browser else 'not found'))
        if browser and status == 'persistent':
            port = _SOURCE['port']
            wrapper = wrap_browser_for_proxy(port)
            log('browser autostart via %s' % (wrapper or 'the plain browser (wrapper not written)'))
            if wrapper:
                import threading
                threading.Thread(target=warn_if_unproxied_browser, args=(port,), daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        log('browser lookup failed (%s)' % exc)
    if args[0] == '-e' and len(args) >= 2:
        import importlib
        mod_name, _, func_name = args[1].partition(':')
        sys.argv = [args[1]] + args[2:]
        result = getattr(importlib.import_module(mod_name), func_name or 'main')()
        return result if isinstance(result, int) else 0
    if args[0] == '-s' and len(args) >= 2:
        import runpy
        script = os.path.abspath(args[1])
        sys.path.insert(0, os.path.dirname(script))
        sys.argv = [script] + args[2:]
        runpy.run_path(script, run_name='__main__')
        return 0
    if args[0] == '-m' and len(args) >= 2:
        import runpy
        sys.argv = args[1:]
        runpy.run_module(args[1], run_name='__main__', alter_sys=True)
        return 0
    dc.stderr('unknown args: %s' % args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
