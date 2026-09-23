"""Minimal local HTTP/HTTPS forward proxy whose outbound sockets are bound to the
physical adapter, so proxied traffic leaves directly instead of via the VPN.

Adapted from the owner's mcp-marketplaces/direct_proxy.py. Two ways to run:

  * persistent (preferred with a VPN): `python direct_proxy.py --daemon PORT IP`
    spawns a detached `--serve` process and exits at once, so the proxy is not
    a child of the MCP (a tree kill of the MCP leaves it alone) and outlives
    it, like the browser that points its --proxy-server at the fixed PORT.
  * in-process fallback (start_proxy): a daemon thread inside the launcher
    that lives and dies with the MCP server.

Listens on 127.0.0.1 only; loopback is never captured by a TUN, so the
client -> proxy leg is safe.

Log: <state dir>/proxy.log (see direct_common.state_dir), never the package.
Windows-only, stdlib-only. Nothing here writes to stdout.
"""

import os
import socket
import sys
import threading
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from direct_common import log as _log  # noqa: E402

LOG_NAME = 'proxy.log'


def log(message):
    _log(LOG_NAME, message)


def direct_connect(host, port, source_ip, timeout=30):
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    last = None
    for family, stype, proto, _canon, addr in infos:
        s = socket.socket(family, stype, proto)
        s.settimeout(timeout)
        if source_ip:
            try:
                s.bind((source_ip, 0))
            except OSError as exc:
                log('bind %s failed: %s' % (source_ip, exc))
        try:
            s.connect(addr)
            return s
        except OSError as exc:
            last = exc
            s.close()
    raise last or OSError('cannot connect to %s:%s' % (host, port))


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(conn, peer, source_ip):
    up = None
    if callable(source_ip):
        source_ip = source_ip()
    try:
        conn.settimeout(30)
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 262144:
                return
        head, _, rest = buf.partition(b'\r\n\r\n')
        lines = head.split(b'\r\n')
        try:
            method, target, version = lines[0].split(b' ', 2)
        except ValueError:
            conn.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
            return
        method = method.decode('latin1')
        target = target.decode('latin1')

        if method.upper() == 'CONNECT':
            host, _, port = target.rpartition(':')
            if not host:
                host, port = target, '443'
            up = direct_connect(host.strip('[]'), int(port or '443'), source_ip)
            log('CONNECT %s:%s via %s' % (host, port, source_ip or 'default'))
            conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if rest:
                up.sendall(rest)
        else:
            parts = urlsplit(target)
            host = parts.hostname or ''
            port = parts.port or 80
            path = parts.path or '/'
            if parts.query:
                path += '?' + parts.query
            up = direct_connect(host, port, source_ip)
            log('%s %s://%s:%s via %s' % (method, parts.scheme or 'http', host, port, source_ip or 'default'))
            rebuilt = b' '.join([method.encode('latin1'), path.encode('latin1'), version])
            up.sendall(rebuilt + b'\r\n' + b'\r\n'.join(lines[1:]) + b'\r\n\r\n' + rest)

        conn.settimeout(None)
        up.settimeout(None)
        threading.Thread(target=pump, args=(conn, up), daemon=True).start()
        pump(up, conn)
    except (OSError, ValueError) as exc:
        log('error for %s: %s' % (peer, exc))
        try:
            conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
        except OSError:
            pass
    finally:
        for sock in (conn, up):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def bind_listener(port):
    """Bind 127.0.0.1:port exclusively (port 0 = OS picks). Raises OSError."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        srv.bind(('127.0.0.1', port))
        srv.listen(64)
    except OSError:
        srv.close()
        raise
    return srv


def serve(srv, source_ip):
    while True:
        try:
            conn, peer = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn, peer, source_ip), daemon=True).start()


def start_proxy(srv, source_ip):
    """Serve on an already bound listener in a daemon thread; returns the port."""
    port = srv.getsockname()[1]
    log('listening on 127.0.0.1:%s, source=%s' % (port, source_ip or 'default'))
    threading.Thread(target=serve, args=(srv, source_ip), daemon=True, name='mp-direct-proxy').start()
    return port


DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def spawn_detached(args):
    import subprocess
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACHED, close_fds=True, cwd=os.path.dirname(os.path.abspath(__file__)))


def dynamic_source(initial):
    """The launcher refreshes direct_ip.txt on every start; a long-lived proxy
    follows it, so a DHCP change does not strand it on a dead address."""
    from direct_common import read_cached_ip

    def current():
        return read_cached_ip() or initial
    return current


def main(argv):
    if len(argv) == 3 and argv[0] in ('--daemon', '--serve'):
        port, ip = int(argv[1]), argv[2]
        if argv[0] == '--daemon':
            spawn_detached([sys.executable, os.path.abspath(__file__), '--serve', str(port), ip])
            return 0
        try:
            srv = bind_listener(port)
        except OSError as exc:
            log('persistent proxy: port %s busy (%s)' % (port, exc))
            return 1
        log('persistent proxy pid %s listening on 127.0.0.1:%s, source=%s' % (os.getpid(), port, ip or 'default'))
        serve(srv, dynamic_source(ip))
        return 0
    sys.stderr.write('usage: direct_proxy.py --daemon|--serve PORT SOURCE_IP\n')
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
