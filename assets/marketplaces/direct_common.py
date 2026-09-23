"""Shared helpers for the marketplaces launcher: state dir, logging, VPN detection.

Windows-only, stdlib-only. Adapted from the owner's mcp-marketplaces/direct_common.py.

The decision logic (decide_route, choose_port) is pure and unit-tested; the only
side-effecting collector is collect_network (one PowerShell call, bounded).
Nothing here may write to stdout: the MCP server owns the stdio JSON-RPC stream.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time

# Adapter/route names that mean "traffic goes through a VPN or TUN".
VPN_PATTERN = re.compile(r'tun|wintun|happ|karing|sing-?box|wireguard|openvpn|vpn', re.IGNORECASE)
DEFAULT_PORT = 34567
LOG_LIMIT = 512 * 1024
# Catch-all prefixes a TUN may install instead of (or in front of) 0.0.0.0/0.
DEFAULT_PREFIXES = ('0.0.0.0/0', '0.0.0.0/1', '128.0.0.0/1')


def state_dir():
    """Writable directory for the cache and logs; never the launcher's own folder."""
    candidates = [os.environ.get('MP_STATE_DIR', '')]
    home = os.environ.get('HERMES_HOME', '')
    if home:
        candidates.append(os.path.join(home, 'mcp', 'marketplaces', 'state'))
    local = os.environ.get('LOCALAPPDATA', '')
    if local:
        candidates.append(os.path.join(local, 'hermes', 'mcp', 'marketplaces', 'state'))
    candidates.append(os.path.join(os.environ.get('TEMP', '.'), 'hermes-marketplaces'))
    for path in candidates:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return ''


def log(name, message):
    """Append one line to <state>/<name>; rotate once at LOG_LIMIT. Never raises."""
    base = state_dir()
    if not base:
        return
    path = os.path.join(base, name)
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, path + '.1')
    except OSError:
        pass
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), message))
    except OSError:
        pass


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _metric(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        # A TUN often reports no interface metric at all: it wins routing.
        return 0


def _usable_ip(ip):
    return (isinstance(ip, str) and ip.count('.') == 3
            and not ip.startswith('169.254.') and not ip.startswith('127.'))


def decide_route(adapters, routes, metrics, addresses):
    """Pure decision over Get-NetAdapter / Get-NetRoute / Get-NetIPInterface /
    Get-NetIPAddress rows (dicts as PowerShell ConvertTo-Json emits them).

    Returns {'vpn': bool, 'reason': str, 'direct_ip': str}. direct_ip is the
    IPv4 of the best physical adapter (only meaningful when vpn is True; may be
    '' when no physical address exists, and the caller then falls back).
    """
    adapters = [a for a in _as_list(adapters) if isinstance(a, dict)]
    by_index = {a.get('ifIndex'): a for a in adapters}
    if_metric = {m.get('ifIndex'): _metric(m.get('InterfaceMetric'))
                 for m in _as_list(metrics) if isinstance(m, dict)}

    def label(index, alias=''):
        a = by_index.get(index) or {}
        return ' '.join(str(x) for x in (alias, a.get('Name', ''), a.get('InterfaceDescription', '')) if x)

    def physical(index):
        a = by_index.get(index)
        return bool(a) and a.get('HardwareInterface') is True and not VPN_PATTERN.search(label(index))

    def up(index):
        a = by_index.get(index)
        return bool(a) and str(a.get('Status', '')).lower() == 'up'

    ranked = []
    for r in _as_list(routes):
        if not isinstance(r, dict) or r.get('DestinationPrefix') not in DEFAULT_PREFIXES:
            continue
        index = r.get('ifIndex')
        # Split /1 routes beat any /0 by prefix length: rank them first.
        split = 0 if r.get('DestinationPrefix') != '0.0.0.0/0' else 1
        ranked.append((split, _metric(r.get('RouteMetric')) + if_metric.get(index, 0), index, r.get('InterfaceAlias', '')))
    ranked.sort(key=lambda item: (item[0], item[1]))

    vpn, reason = False, 'no vpn'
    if ranked:
        _, _, index, alias = ranked[0]
        if VPN_PATTERN.search(label(index, alias)):
            vpn, reason = True, 'default route via ' + (alias or str(index))
        elif not physical(index):
            vpn, reason = True, 'default route via non-physical ' + (alias or str(index))
    if not vpn:
        for a in adapters:
            if up(a.get('ifIndex')) and VPN_PATTERN.search(label(a.get('ifIndex'))):
                vpn, reason = True, 'active adapter ' + str(a.get('Name', a.get('ifIndex')))
                break

    # Physical source address: prefer an adapter that has its own default route.
    routed = {index: metric for split, metric, index, _ in ranked if split == 1}
    best = []
    for row in _as_list(addresses):
        if not isinstance(row, dict):
            continue
        index = row.get('ifIndex')
        ip = row.get('IPAddress')
        if _usable_ip(ip) and physical(index) and up(index):
            best.append((0 if index in routed else 1, routed.get(index, if_metric.get(index, 9999)), ip))
    best.sort()
    return {'vpn': vpn, 'reason': reason, 'direct_ip': best[0][2] if best else ''}


_PS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ErrorActionPreference='SilentlyContinue'; "
    "$a=@(Get-NetAdapter | Select-Object Name,InterfaceDescription,ifIndex,Status,HardwareInterface); "
    "$r=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0','0.0.0.0/1','128.0.0.0/1' | "
    "Select-Object DestinationPrefix,ifIndex,InterfaceAlias,RouteMetric); "
    "$m=@(Get-NetIPInterface -AddressFamily IPv4 | Select-Object ifIndex,InterfaceMetric); "
    "$i=@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object ifIndex,IPAddress); "
    "ConvertTo-Json -Compress -Depth 3 @{adapters=$a;routes=$r;metrics=$m;addresses=$i}"
)


def collect_network(timeout=20):
    """One bounded PowerShell call. Returns the parsed dict or None; never raises."""
    ps = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    try:
        out = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command', _PS],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
        data = json.loads(out.decode('utf-8-sig', errors='replace').strip() or 'null')
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def cache_path():
    base = state_dir()
    return os.path.join(base, 'direct_ip.txt') if base else ''


def read_cached_ip():
    path = cache_path()
    try:
        ip = open(path, encoding='utf-8').read().strip() if path else ''
    except OSError:
        return ''
    return ip if _usable_ip(ip) else ''


def write_cached_ip(ip):
    path = cache_path()
    if path and ip:
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(ip)
        except OSError:
            pass


def choose_port(preferred, can_bind):
    """Preferred port if it is free; otherwise 0 (let the OS pick a free one).

    can_bind(port) -> bool. The proxy lives inside this MCP process, so a port
    already held by anything else (even another Hermes window's proxy, which
    dies with its own MCP) is never reused.
    """
    try:
        preferred = int(preferred)
    except (TypeError, ValueError):
        preferred = DEFAULT_PORT
    if not 0 < preferred < 65536:
        preferred = DEFAULT_PORT
    return preferred if can_bind(preferred) else 0


def direct_path_works(ip, host='ozon.ru', port=443, timeout=5):
    """Bind to the physical address and open one TCP connection. Never raises."""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        return False, 'resolve %s failed: %s' % (host, exc)
    last = 'no address'
    for family, stype, proto, _canon, addr in infos[:3]:
        s = socket.socket(family, stype, proto)
        s.settimeout(timeout)
        try:
            s.bind((ip, 0))
            s.connect(addr)
            return True, 'ok'
        except OSError as exc:
            last = str(exc)
        finally:
            s.close()
    return False, last


# --- Persistent direct proxy (owner's original ensure_proxy pattern) ---------
# The browser outlives any one MCP process, so its proxy must too: a detached
# proxy on a FIXED port the browser's --proxy-server flag can keep pointing at.
PROXY_PORTS = (DEFAULT_PORT, 34568, 34569)
PORT_FILE = 'proxy_port.txt'
PROXY_CHECK_HOST = 'ozon.ru'


def _port(value):
    try:
        value = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return value if 0 < value < 65536 else 0


def proxy_port_candidates(preferred=None, recorded=None):
    """Recorded port first (reuse what the browser already points at), then the
    preferred one (MP_PROXY_PORT), then the fixed list. Deduplicated, valid only."""
    out = []
    for value in (recorded, preferred) + PROXY_PORTS:
        port = _port(value)
        if port and port not in out:
            out.append(port)
    return out


def pick_proxy_port(candidates, state_of, start):
    """Pure choice over candidate ports.

    state_of(port) -> 'ours' (a working direct proxy already listens: reuse it),
    'free' (nothing listens: try start(port) -> bool) or 'foreign' (something
    else listens: skip). Returns (port, 'reused'|'started') or (0, reason).
    """
    tried = []
    for port in candidates:
        try:
            state = state_of(port)
        except Exception:  # noqa: BLE001
            state = 'foreign'
        if state == 'ours':
            return port, 'reused'
        if state == 'free':
            try:
                if start(port):
                    return port, 'started'
            except Exception:  # noqa: BLE001
                pass
            tried.append('%d:start-failed' % port)
        else:
            tried.append('%d:busy' % port)
    return 0, 'no usable port (%s)' % ', '.join(tried)


def read_recorded_port():
    base = state_dir()
    try:
        return _port(open(os.path.join(base, PORT_FILE), encoding='utf-8').read()) if base else 0
    except OSError:
        return 0


def write_recorded_port(port):
    base = state_dir()
    if base and _port(port):
        try:
            with open(os.path.join(base, PORT_FILE), 'w', encoding='utf-8') as fh:
                fh.write(str(int(port)))
        except OSError:
            pass


def port_listening(port, timeout=0.5):
    try:
        socket.create_connection(('127.0.0.1', int(port)), timeout=timeout).close()
        return True
    except OSError:
        return False


def proxy_works(port, host=PROXY_CHECK_HOST, timeout=6):
    """A CONNECT through 127.0.0.1:port to host:443 is answered with 200.
    That is what makes a listener 'ours' (the owner's opencode proxy qualifies)."""
    try:
        s = socket.create_connection(('127.0.0.1', int(port)), timeout=timeout)
    except OSError:
        return False
    try:
        s.settimeout(timeout)
        s.sendall(('CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n' % (host, host)).encode('ascii'))
        head = b''
        while b'\r\n' not in head and len(head) < 1024:
            chunk = s.recv(1024)
            if not chunk:
                break
            head += chunk
        parts = head.split(b'\r\n', 1)[0].split()
        return len(parts) >= 2 and parts[0].startswith(b'HTTP/') and parts[1] == b'200'
    except OSError:
        return False
    finally:
        s.close()


def proxy_port_state(port):
    if not port_listening(port):
        return 'free'
    return 'ours' if proxy_works(port) else 'foreign'


# --- Browser -----------------------------------------------------------------

def browser_candidates(environ=None):
    """Chrome first, then Edge, in the real Windows install locations.

    ru-marketplace-mcp (c17bd36) looks for Edge only under %ProgramFiles%, but
    Edge ships in %ProgramFiles(x86)%, so Edge-only machines (clean Windows,
    Windows Sandbox 2026-09-23) never got the browser tier Ozon/Avito need.
    """
    env = os.environ if environ is None else environ
    pf = env.get('ProgramFiles', r'C:\Program Files')
    pf86 = env.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    local = env.get('LOCALAPPDATA', '')
    found = [pf + r'\Google\Chrome\Application\chrome.exe',
             pf86 + r'\Google\Chrome\Application\chrome.exe']
    if local:
        found.append(local + r'\Google\Chrome\Application\chrome.exe')
    found += [pf86 + r'\Microsoft\Edge\Application\msedge.exe',
              pf + r'\Microsoft\Edge\Application\msedge.exe']
    return found


WRAPPER_NAME = 'browser-direct.cmd'
# Loopback stays direct (CDP, local pages); spelled without <...> so cmd.exe
# never sees a redirection character.
PROXY_BYPASS = 'localhost;127.0.0.1;[::1]'


def proxy_flags(port):
    return ['--proxy-server=http://127.0.0.1:%d' % int(port), '--proxy-bypass-list=%s' % PROXY_BYPASS]


def browser_wrapper_text(browser, port):
    """A .cmd that starts the real browser with our proxy flags plus whatever
    the MCP passes (%*). CRLF; '%' doubled so cmd keeps the path literal."""
    exe = browser.replace('%', '%%')
    return '@echo off\r\nstart "" "%s" %s %%*\r\n' % (exe, ' '.join(proxy_flags(port)))


def write_browser_wrapper(browser, port):
    """Write <state>/browser-direct.cmd; returns its path or '' (never raises).
    Encoded in the OEM code page cmd.exe parses batch files with."""
    base = state_dir()
    if not base or not browser:
        return ''
    path = os.path.join(base, WRAPPER_NAME)
    try:
        data = browser_wrapper_text(browser, port).encode('oem' if os.name == 'nt' else 'ascii')
    except (UnicodeEncodeError, LookupError):
        return ''
    try:
        if not (os.path.isfile(path) and open(path, 'rb').read() == data):
            tmp = path + '.tmp'
            with open(tmp, 'wb') as fh:
                fh.write(data)
            os.replace(tmp, path)
        return path
    except OSError:
        return ''


def stderr(message):
    try:
        sys.stderr.write('[marketplaces] %s\n' % message)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
