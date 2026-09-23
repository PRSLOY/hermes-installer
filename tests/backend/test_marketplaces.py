"""Offline tests for the marketplaces MCP step and its launcher logic.

No network, no uv, no subprocess: the installer and the stdio probe are
injected; the launcher's decision functions are pure.
"""
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
ASSETS = ROOT / 'assets' / 'marketplaces'
sys.path.insert(0, str(BACKEND))
sys.dont_write_bytecode = True  # never leave __pycache__ inside shipped assets


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e = load('extras_mp', BACKEND / 'extras.py')
dc = load('direct_common_mp', ASSETS / 'direct_common.py')

# Real shape captured on the owner's host (Happ VPN on), PowerShell ConvertTo-Json.
HAPP_ADAPTERS = [
    {'Name': 'vEthernet (Default Switch)', 'InterfaceDescription': 'Hyper-V Virtual Ethernet Adapter', 'ifIndex': 22, 'Status': 'Up', 'HardwareInterface': False},
    {'Name': 'happ-xray', 'InterfaceDescription': 'Happ Tunnel', 'ifIndex': 45, 'Status': 'Up', 'HardwareInterface': False},
    {'Name': 'Ethernet', 'InterfaceDescription': 'Realtek Gaming 2.5GbE Family Controller', 'ifIndex': 6, 'Status': 'Up', 'HardwareInterface': True},
]
HAPP_ROUTES = [
    {'DestinationPrefix': '0.0.0.0/0', 'ifIndex': 45, 'InterfaceAlias': 'happ-xray', 'RouteMetric': 0},
    {'DestinationPrefix': '0.0.0.0/0', 'ifIndex': 6, 'InterfaceAlias': 'Ethernet', 'RouteMetric': 0},
]
HAPP_METRICS = [{'ifIndex': 22, 'InterfaceMetric': 5000}, {'ifIndex': 6, 'InterfaceMetric': 25}, {'ifIndex': 45, 'InterfaceMetric': None}]
HAPP_ADDRESSES = [
    {'ifIndex': 22, 'IPAddress': '172.23.64.1'},
    {'ifIndex': 6, 'IPAddress': '192.168.1.50'},
    {'ifIndex': 45, 'IPAddress': '172.19.0.1'},
    {'ifIndex': 1, 'IPAddress': '127.0.0.1'},
]

dl = load('direct_launcher_mp', ASSETS / 'direct_launcher.py')
wb = load('wb_browser_mcp_mp', ASSETS / 'wb_browser_mcp.py')


class BrowserTests(unittest.TestCase):
    ENV = {'ProgramFiles': r'C:\Program Files', 'ProgramFiles(x86)': r'C:\Program Files (x86)', 'LOCALAPPDATA': r'C:\Users\u\AppData\Local'}
    EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'

    def test_edge_only_machine_finds_edge_in_program_files_x86(self):
        env = dict(self.ENV)
        self.assertEqual(dl.choose_browser(env, exists=lambda p: p == self.EDGE), self.EDGE)
        self.assertEqual(env['CHROME_BINARY'], self.EDGE)

    def test_chrome_preferred_over_edge(self):
        env = dict(self.ENV)
        self.assertEqual(dl.choose_browser(env, exists=lambda p: p in (self.EDGE, self.CHROME)), self.CHROME)

    def test_user_choice_kept(self):
        env = dict(self.ENV, CHROME_BINARY=r'D:\my\chrome.exe')
        self.assertEqual(dl.choose_browser(env, exists=lambda p: True), r'D:\my\chrome.exe')

    def test_no_browser(self):
        env = dict(self.ENV)
        self.assertEqual(dl.choose_browser(env, exists=lambda p: False), '')
        self.assertNotIn('CHROME_BINARY', env)

    def test_warmup_uses_the_mcp_profile_and_port(self):
        self.assertEqual(dl.scraping_profile(self.ENV), r'C:\Users\u\AppData\Local\Chrome-Scraping')
        self.assertEqual(dl.scraping_profile(dict(self.ENV, CHROME_SCRAPING_PROFILE=r'D:\p')), r'D:\p')
        args = dl.warmup_args(self.CHROME, r'D:\p', '9222')
        self.assertIn('--remote-debugging-port=9222', args)
        self.assertIn('--user-data-dir=D:\\p', args)
        self.assertTrue(any('ozon.ru' in a for a in args) and any('avito.ru' in a for a in args))
        self.assertTrue(any('wildberries.ru' in a for a in args))
        self.assertFalse(any('headless' in a or '-32000' in a for a in args))
        self.assertFalse(any('proxy' in a for a in args), 'no VPN -> plain browser')

    def test_warmup_with_vpn_uses_the_same_proxy_flags(self):
        args = dl.warmup_args(self.CHROME, r'D:\p', '9222', 34568)
        self.assertIn('--proxy-server=http://127.0.0.1:34568', args)
        self.assertEqual(args[-3:], list(dl.WARMUP_SITES))

    def test_unproxied_browser_detection(self):
        lines = ['"chrome.exe" --remote-debugging-port=9222 --user-data-dir=D:\\p about:blank',
                 '"chrome.exe" --proxy-server=http://127.0.0.1:34567 --user-data-dir=D:\\p']
        self.assertEqual(dl.unproxied(lines, 34567), lines[:1])
        self.assertEqual(dl.unproxied(lines, 34568), lines)


class WrapperTests(unittest.TestCase):
    EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'

    def test_wrapper_text_is_crlf_start_with_proxy_and_passthrough(self):
        text = dc.browser_wrapper_text(self.EDGE, 34567)
        self.assertEqual(text, '@echo off\r\nstart "" "%s" --proxy-server=http://127.0.0.1:34567 '
                               '--proxy-bypass-list=localhost;127.0.0.1;[::1] %%*\r\n' % self.EDGE)
        self.assertNotIn('\n', text.replace('\r\n', ''))
        # No cmd.exe redirection/pipe characters outside the quoted exe.
        for ch in '<>|&':
            self.assertNotIn(ch, text)

    def test_percent_in_path_is_escaped(self):
        self.assertIn('"C:\\a%%b\\chrome.exe"', dc.browser_wrapper_text('C:\\a%b\\chrome.exe', 1))

    def test_wrapper_written_to_state_dir_and_chrome_binary_swapped(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get('MP_STATE_DIR')
            os.environ['MP_STATE_DIR'] = tmp
            try:
                env = {'CHROME_BINARY': self.EDGE}
                path = dl.wrap_browser_for_proxy(34569, env)
                self.assertEqual(path, os.path.join(tmp, 'browser-direct.cmd'))
                self.assertEqual(env['CHROME_BINARY'], path)
                self.assertEqual(env['MP_REAL_BROWSER'], self.EDGE)
                data = Path(path).read_bytes()
                self.assertTrue(data.endswith(b'%*\r\n'))
                self.assertIn(b'127.0.0.1:34569', data)
                # Second call: same bytes, never wraps the wrapper.
                self.assertEqual(dl.wrap_browser_for_proxy(34569, env), '')
                self.assertEqual(dl.wrap_browser_for_proxy(34569, {'CHROME_BINARY': self.EDGE}), path)
                self.assertEqual(Path(path).read_bytes(), data)
            finally:
                if old is None:
                    os.environ.pop('MP_STATE_DIR', None)
                else:
                    os.environ['MP_STATE_DIR'] = old


class PersistentPortTests(unittest.TestCase):
    def test_candidates_order(self):
        self.assertEqual(dc.proxy_port_candidates(), [34567, 34568, 34569])
        self.assertEqual(dc.proxy_port_candidates('34580', 34569), [34569, 34580, 34567, 34568])
        self.assertEqual(dc.proxy_port_candidates('junk', 0), [34567, 34568, 34569])

    def test_compatible_proxy_on_34567_is_reused_not_restarted(self):
        started = []
        port, how = dc.pick_proxy_port([34567, 34568], lambda p: 'ours', lambda p: started.append(p) or True)
        self.assertEqual((port, how, started), (34567, 'reused', []))

    def test_foreign_listener_skipped_next_fixed_port_started(self):
        states = {34567: 'foreign', 34568: 'free'}
        started = []
        port, how = dc.pick_proxy_port([34567, 34568, 34569], states.get, lambda p: started.append(p) or True)
        self.assertEqual((port, how, started), (34568, 'started', [34568]))

    def test_all_fail_gives_zero_for_the_in_process_fallback(self):
        def boom(p):
            raise OSError('x')
        port, how = dc.pick_proxy_port([34567, 34568], lambda p: 'free', boom)
        self.assertEqual(port, 0)
        self.assertIn('34567:start-failed', how)

    def test_recorded_port_round_trip(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get('MP_STATE_DIR')
            os.environ['MP_STATE_DIR'] = tmp
            try:
                self.assertEqual(dc.read_recorded_port(), 0)
                dc.write_recorded_port(34568)
                self.assertEqual(dc.read_recorded_port(), 34568)
            finally:
                if old is None:
                    os.environ.pop('MP_STATE_DIR', None)
                else:
                    os.environ['MP_STATE_DIR'] = old

    def test_real_proxy_detected_as_ours_and_plain_listener_as_foreign(self):
        import socket
        import threading
        direct_proxy = load('direct_proxy_mp2', ASSETS / 'direct_proxy.py')
        plain = direct_proxy.bind_listener(0)
        try:
            port = plain.getsockname()[1]

            def answer():
                try:
                    conn, _ = plain.accept()
                    conn.recv(1024)
                    conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                    conn.close()
                except OSError:
                    pass
            threading.Thread(target=answer, daemon=True).start()
            self.assertEqual(dc.proxy_port_state(port), 'foreign')
        finally:
            plain.close()
        free = socket.socket()
        free.bind(('127.0.0.1', 0))
        free_port = free.getsockname()[1]
        free.close()
        self.assertEqual(dc.proxy_port_state(free_port), 'free')


class WildberriesPathTests(unittest.TestCase):
    ENV = {'ProgramFiles': r'C:\Program Files', 'ProgramFiles(x86)': r'C:\Program Files (x86)',
           'LOCALAPPDATA': r'C:\Users\u\AppData\Local'}
    EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'

    def test_no_hardcoded_user_path(self):
        text = (ASSETS / 'wb_browser_mcp.py').read_text(encoding='utf-8').lower()
        self.assertNotIn(r'c:\users', text)

    def test_port_env_order(self):
        self.assertEqual(wb.resolve_port({}), 9222)
        self.assertEqual(wb.resolve_port({'CHROME_CDP_PORT': '9333'}), 9333)
        self.assertEqual(wb.resolve_port({'WB_CDP_PORT': '9444', 'CHROME_CDP_PORT': '9333'}), 9444)
        self.assertEqual(wb.resolve_port({'WB_CDP_PORT': 'x'}), 9222)

    def test_chrome_env_order_and_edge_x86_lookup(self):
        env = dict(self.ENV, CHROME_BINARY=r'D:\s\browser-direct.cmd')
        self.assertEqual(wb.resolve_chrome(env, exists=lambda p: False), r'D:\s\browser-direct.cmd')
        self.assertEqual(wb.resolve_chrome(dict(env, WB_CDP_CHROME=r'D:\c.exe')), r'D:\c.exe')
        self.assertEqual(wb.resolve_chrome(dict(self.ENV), exists=lambda p: p == self.EDGE), self.EDGE)

    def test_profile_env_order(self):
        self.assertEqual(wb.resolve_profile(self.ENV), r'C:\Users\u\AppData\Local\Chrome-Scraping')
        self.assertEqual(wb.resolve_profile(dict(self.ENV, CHROME_SCRAPING_PROFILE=r'D:\p')), r'D:\p')
        self.assertEqual(wb.resolve_profile(dict(self.ENV, CHROME_SCRAPING_PROFILE=r'D:\p', WB_CDP_PROFILE=r'D:\w')), r'D:\w')


class DecideRouteTests(unittest.TestCase):
    def test_happ_default_route_is_vpn_with_physical_source(self):
        d = dc.decide_route(HAPP_ADAPTERS, HAPP_ROUTES, HAPP_METRICS, HAPP_ADDRESSES)
        self.assertTrue(d['vpn'])
        self.assertIn('happ-xray', d['reason'])
        self.assertEqual(d['direct_ip'], '192.168.1.50')

    def test_plain_home_network_is_direct(self):
        adapters = [{'Name': 'Wi-Fi', 'InterfaceDescription': 'Intel(R) Wi-Fi 6 AX201', 'ifIndex': 12, 'Status': 'Up', 'HardwareInterface': True},
                    HAPP_ADAPTERS[0]]
        routes = [{'DestinationPrefix': '0.0.0.0/0', 'ifIndex': 12, 'InterfaceAlias': 'Wi-Fi', 'RouteMetric': 0}]
        d = dc.decide_route(adapters, routes, [{'ifIndex': 12, 'InterfaceMetric': 35}],
                            [{'ifIndex': 12, 'IPAddress': '10.0.0.5'}])
        self.assertFalse(d['vpn'])
        self.assertEqual(d['direct_ip'], '10.0.0.5')

    def test_split_tun_routes_win_over_physical_default(self):
        # sing-box style: 0.0.0.0/1 + 128.0.0.0/1 on the TUN, /0 left on Ethernet.
        adapters = HAPP_ADAPTERS[2:] + [{'Name': 'sing-box', 'InterfaceDescription': 'sing-box TUN', 'ifIndex': 50, 'Status': 'Up', 'HardwareInterface': False}]
        routes = [HAPP_ROUTES[1], {'DestinationPrefix': '0.0.0.0/1', 'ifIndex': 50, 'InterfaceAlias': 'sing-box', 'RouteMetric': 0}]
        d = dc.decide_route(adapters, routes, [{'ifIndex': 6, 'InterfaceMetric': 25}], HAPP_ADDRESSES)
        self.assertTrue(d['vpn'])
        self.assertEqual(d['direct_ip'], '192.168.1.50')

    def test_unnamed_non_physical_lowest_metric_is_vpn(self):
        adapters = HAPP_ADAPTERS[2:] + [{'Name': 'Local Area Connection 3', 'InterfaceDescription': 'Some Virtual Adapter', 'ifIndex': 70, 'Status': 'Up', 'HardwareInterface': False}]
        routes = [HAPP_ROUTES[1], {'DestinationPrefix': '0.0.0.0/0', 'ifIndex': 70, 'InterfaceAlias': 'Local Area Connection 3', 'RouteMetric': 0}]
        d = dc.decide_route(adapters, routes, [{'ifIndex': 6, 'InterfaceMetric': 25}, {'ifIndex': 70, 'InterfaceMetric': 1}], HAPP_ADDRESSES)
        self.assertTrue(d['vpn'])
        self.assertIn('non-physical', d['reason'])

    def test_active_vpn_adapter_without_default_route_still_counts(self):
        adapters = HAPP_ADAPTERS[2:] + [{'Name': 'WireGuard Tunnel', 'InterfaceDescription': 'WireGuard', 'ifIndex': 80, 'Status': 'Up', 'HardwareInterface': False}]
        d = dc.decide_route(adapters, [HAPP_ROUTES[1]], [{'ifIndex': 6, 'InterfaceMetric': 25}], HAPP_ADDRESSES)
        self.assertTrue(d['vpn'])

    def test_disconnected_vpn_adapter_is_ignored(self):
        adapters = HAPP_ADAPTERS[2:] + [{'Name': 'OpenVPN TAP', 'InterfaceDescription': 'TAP-Windows Adapter', 'ifIndex': 81, 'Status': 'Disconnected', 'HardwareInterface': False}]
        d = dc.decide_route(adapters, [HAPP_ROUTES[1]], [{'ifIndex': 6, 'InterfaceMetric': 25}], HAPP_ADDRESSES)
        self.assertFalse(d['vpn'])

    def test_vpn_without_physical_address_gives_empty_source(self):
        d = dc.decide_route(HAPP_ADAPTERS[:2], HAPP_ROUTES[:1], [], HAPP_ADDRESSES)
        self.assertTrue(d['vpn'])
        self.assertEqual(d['direct_ip'], '')

    def test_single_objects_and_garbage_are_tolerated(self):
        # PowerShell may unwrap one-element arrays; junk rows must not crash.
        d = dc.decide_route(HAPP_ADAPTERS[2], HAPP_ROUTES[1], None, ['junk', {'ifIndex': 6, 'IPAddress': '169.254.1.1'}])
        self.assertFalse(d['vpn'])
        self.assertEqual(d['direct_ip'], '')


class ChoosePortTests(unittest.TestCase):
    def test_free_preferred_port_is_used(self):
        self.assertEqual(dc.choose_port('34567', lambda p: True), 34567)

    def test_busy_port_falls_back_to_os_choice(self):
        seen = []
        self.assertEqual(dc.choose_port(34567, lambda p: seen.append(p) or False), 0)
        self.assertEqual(seen, [34567])

    def test_invalid_port_uses_default(self):
        seen = []
        for bad in ('abc', None, 0, 70000):
            dc.choose_port(bad, lambda p: seen.append(p) or True)
        self.assertEqual(seen, [dc.DEFAULT_PORT] * 4)

    def test_real_busy_socket(self):
        direct_proxy = load('direct_proxy_mp', ASSETS / 'direct_proxy.py')
        held = direct_proxy.bind_listener(0)
        try:
            port = held.getsockname()[1]
            def can_bind(p):
                try:
                    direct_proxy.bind_listener(p).close()
                    return True
                except OSError:
                    return False
            self.assertEqual(dc.choose_port(port, can_bind), 0)
        finally:
            held.close()


class MarketplacesStepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mp-test-')
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.assets = ROOT / 'assets'
        self.calls = {'install': 0, 'probe': 0}

    def tearDown(self):
        self.tmp.cleanup()

    def installer(self, home, assets):
        self.calls['install'] += 1
        p = e.marketplaces_paths(home)
        return p['repo'] / '.venv' / 'Scripts' / 'python.exe', p['launcher'], p['state']

    def probe_ok(self, command, args, env):
        self.calls['probe'] += 1
        if '-s' in args:
            return ['wb_search', 'wb_browser_status']
        return ['ozon_search', 'yandex_search', 'compare_prices']

    def write_config(self, text):
        (self.home / 'config.yaml').write_text(text, encoding='utf-8')

    def config_bytes(self):
        path = self.home / 'config.yaml'
        return path.read_bytes() if path.exists() else None

    def run_step(self, probe=None, installer=None):
        return e.main_marketplaces(self.home, self.assets, installer=installer or self.installer, probe=probe or self.probe_ok)

    def test_stdio_entry_merged_next_to_user_servers(self):
        import yaml
        self.write_config('model:\n  default: m\n# keep me\nmcp_servers:\n  mine:\n    url: https://mine.test/mcp\n')
        result = self.run_step()
        self.assertEqual(result, {'ok': True, 'marketplaces': 'added', 'wildberries': 'added'})
        text = self.config_bytes().decode('utf-8')
        self.assertIn('# keep me', text)
        parsed = yaml.safe_load(text)
        self.assertEqual(parsed['model'], {'default': 'm'})
        self.assertEqual(parsed['mcp_servers']['mine'], {'url': 'https://mine.test/mcp'})
        entry = parsed['mcp_servers']['marketplaces']
        paths = e.marketplaces_paths(self.home)
        self.assertEqual(entry['command'], str(paths['repo'] / '.venv' / 'Scripts' / 'python.exe'))
        self.assertEqual(entry['args'], [str(paths['launcher'] / 'direct_launcher.py'), '-e', 'marketplace_connector.__main__:main'])
        self.assertEqual(entry['env'], {'MARKETPLACE_SOURCES': 'ozon,avito,yandex_market,detsky_mir,compare',
                                        'MP_STATE_DIR': str(paths['state'])})
        self.assertIs(entry['enabled'], True)
        self.assertNotIn('url', entry)
        wbe = parsed['mcp_servers']['wildberries']
        self.assertEqual(wbe['command'], entry['command'], 'same venv python')
        self.assertEqual(wbe['args'], [str(paths['launcher'] / 'direct_launcher.py'), '-s',
                                       str(paths['launcher'] / 'wb_browser_mcp.py')])
        self.assertEqual(wbe['env'], {'MP_STATE_DIR': str(paths['state'])})
        self.assertIs(wbe['enabled'], True)

    def test_non_ascii_windows_path_survives_yaml(self):
        import yaml
        entry = e.marketplaces_entry(r'C:\Users\Павел\x y\python.exe', r'C:\Users\Павел\l', r'C:\s')
        block = 'mcp_servers:\n' + e.server_block({'marketplaces': entry})
        self.assertEqual(yaml.safe_load(block)['mcp_servers']['marketplaces'], entry)

    def test_config_without_mcp_section_gets_one(self):
        import yaml
        self.write_config('model:\n  default: m\n')
        self.assertEqual(self.run_step()['marketplaces'], 'added')
        self.assertIn('marketplaces', yaml.safe_load(self.config_bytes().decode('utf-8'))['mcp_servers'])

    def test_second_run_is_idempotent(self):
        self.write_config('model:\n  default: m\n')
        self.assertEqual(self.run_step()['marketplaces'], 'added')
        after = self.config_bytes()
        self.assertEqual(self.run_step(), {'ok': True, 'marketplaces': 'exists', 'wildberries': 'exists'})
        self.assertEqual(self.config_bytes(), after)
        self.assertEqual(self.calls, {'install': 1, 'probe': 2})

    def test_probe_failure_is_failed_and_config_untouched(self):
        self.write_config('model:\n  default: m\n')
        before = self.config_bytes()
        self.assertEqual(self.run_step(probe=lambda c, a, env: []),
                         {'ok': False, 'marketplaces': 'failed', 'wildberries': 'failed'})
        self.assertEqual(self.config_bytes(), before)

    def test_probe_without_search_tool_is_failed(self):
        self.write_config('model:\n  default: m\n')
        self.assertEqual(self.run_step(probe=lambda c, a, env: ['marketplace_sources'])['marketplaces'], 'failed')

    def test_installer_exception_is_failed(self):
        self.write_config('model:\n  default: m\n')
        before = self.config_bytes()
        def boom(home, assets):
            raise OSError('network down')
        self.assertEqual(self.run_step(installer=boom), {'ok': False, 'marketplaces': 'failed', 'wildberries': 'failed'})
        self.assertEqual(self.config_bytes(), before)

    def test_broken_config_is_failed_not_raised(self):
        self.write_config('- just\n- a list\n')
        self.assertEqual(self.run_step()['marketplaces'], 'failed')
        self.write_config('mcp_servers: [1, 2]\n')
        self.assertEqual(self.run_step()['marketplaces'], 'failed')
        self.assertEqual(self.calls['install'], 0)

    def test_concurrent_edit_during_probe_is_not_overwritten(self):
        self.write_config('model:\n  default: m\n')
        edited = b'model:\n  default: m\nedited: true\n'
        def probe(c, a, env):
            (self.home / 'config.yaml').write_bytes(edited)
            return ['ozon_search']
        self.assertEqual(self.run_step(probe=probe)['marketplaces'], 'failed')
        self.assertEqual(self.config_bytes(), edited)

    def test_existing_entries_are_kept_byte_for_byte(self):
        self.write_config('mcp_servers:\n  marketplaces:\n    command: my-python\n  wildberries:\n    command: mine\n')
        before = self.config_bytes()
        self.assertEqual(self.run_step(), {'ok': True, 'marketplaces': 'exists', 'wildberries': 'exists'})
        self.assertEqual(self.config_bytes(), before)
        self.assertEqual(self.calls['install'], 0)

    def test_upgrade_adds_only_wildberries_next_to_existing_marketplaces(self):
        import yaml
        self.write_config('mcp_servers:\n  marketplaces:\n    command: my-python\n')
        self.assertEqual(self.run_step(), {'ok': True, 'marketplaces': 'exists', 'wildberries': 'added'})
        text = self.config_bytes().decode('utf-8')
        self.assertIn('    command: my-python', text)
        parsed = yaml.safe_load(text)['mcp_servers']
        self.assertEqual(parsed['marketplaces'], {'command': 'my-python'})
        self.assertIn('-s', parsed['wildberries']['args'])
        self.assertEqual(self.calls, {'install': 1, 'probe': 1})

    def test_wildberries_probe_failure_does_not_block_marketplaces(self):
        import yaml
        self.write_config('model:\n  default: m\n')
        probe = lambda c, a, env: [] if '-s' in a else ['ozon_search']
        self.assertEqual(self.run_step(probe=probe), {'ok': True, 'marketplaces': 'added', 'wildberries': 'failed'})
        servers = yaml.safe_load(self.config_bytes().decode('utf-8'))['mcp_servers']
        self.assertEqual(sorted(servers), ['marketplaces'])
        # A rerun retries only the missing Wildberries entry.
        self.assertEqual(self.run_step()['wildberries'], 'added')

    def test_main_extras_does_not_run_marketplaces(self):
        result = e.main(self.home, Path(self.tmp.name) / 'no-repo', self.assets, probe=lambda url: False)
        self.assertNotIn('marketplaces', result)
        self.assertFalse((self.home / 'mcp').exists())


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mp-install-')
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.runs = []

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, url, dest):
        self.assertIn(e.MARKETPLACES_COMMIT, url)
        top = 'ru-marketplace-mcp-' + e.MARKETPLACES_COMMIT + '/'
        Path(dest).write_bytes(make_zip({top + 'uv.lock': 'lock', top + 'pyproject.toml': 'p',
                                         top + 'packages/marketplace-connector/pyproject.toml': 'p'}))

    def runner(self, cmd, cwd, timeout, env):
        self.runs.append(cmd)
        py = Path(cwd) / '.venv' / 'Scripts' / 'python.exe'
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_bytes(b'')
        return 0

    def install(self, runner=None):
        return e.install_marketplaces(self.home, ROOT / 'assets', fetch=self.fetch, runner=runner or self.runner,
                                      uv=Path('uv.exe'))

    def test_install_then_reuse(self):
        python, launcher, state = self.install()
        self.assertTrue(python.is_file())
        self.assertEqual(sorted(p.name for p in launcher.glob('*.py')), sorted(e.MARKETPLACES_LAUNCHER))
        self.assertTrue(state.is_dir())
        self.assertIn('--frozen', self.runs[0])
        self.assertIn('--no-dev', self.runs[0])
        self.assertEqual((python.parents[2] / e.MARKER).read_text(encoding='utf-8'), e.MARKETPLACES_COMMIT)
        self.install()
        self.assertEqual(len(self.runs), 1, 'a complete install must not sync again')

    def test_failed_sync_raises_and_retry_cleans_up(self):
        with self.assertRaises(Exception):
            self.install(runner=lambda cmd, cwd, timeout, env: 1)
        python, _, _ = self.install()
        self.assertTrue(python.is_file())

    def test_foreign_folder_is_never_removed(self):
        repo = e.marketplaces_paths(self.home)['repo']
        repo.mkdir(parents=True)
        (repo / 'mine.txt').write_text('user data', encoding='utf-8')
        with self.assertRaises(Exception):
            self.install()
        self.assertEqual((repo / 'mine.txt').read_text(encoding='utf-8'), 'user data')

    def test_zip_slip_rejected(self):
        archive = Path(self.tmp.name) / 'evil.zip'
        archive.write_bytes(make_zip({'top/ok.txt': 'x', 'top/../../evil.txt': 'x'}))
        with self.assertRaises(Exception):
            e.safe_extract(archive, Path(self.tmp.name) / 'out')
        self.assertFalse((Path(self.tmp.name) / 'evil.txt').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
