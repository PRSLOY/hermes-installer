"""RED/GREEN: the Electron fallback mirrors must be hosts that actually answer from the
subscriber's network, and they must be tried BEFORE falling back to a slow/blocked GitHub.

Live measurements inside Windows Sandbox (2026-09-18, electron 40.10.2, real byte transfer):
    https://cdn.npmmirror.com/binaries/electron/  CustomDir="{{ version }}" -> HTTP 206, 2866 KB/s
    https://mirrors.huaweicloud.com/electron/     CustomDir="{{ version }}" -> HTTP 206, 1667 KB/s
    https://npmmirror.com/mirrors/electron/       -> connect timeout (this was the configured default!)
    https://github.com/electron/electron/releases -> works, but only ~520 KB/s

Two traps this file exists to prevent:
  1. A mirror that does not answer is worse than useless: the payload is ~150 MB and the whole
     desktop stage already runs ~15 minutes, so the retry it powers times out too.
  2. @electron/get builds the URL as <Mirror><CustomDir>/<filename>, and the DEFAULT CustomDir is
     "v" + version. These mirrors serve the bare version, so each entry must carry
     CustomDir = "{{ version }}" -- otherwise the fallback 404s/times out even on a good mirror.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / 'backend' / 'upstream' / 'install.ps1').read_text(
    encoding='utf-8-sig')

# Hosts measured to answer from inside the sandbox, with their measured KB/s.
VERIFIED = {
    'cdn.npmmirror.com': 2866,
    'mirrors.huaweicloud.com': 1667,
}
# Measured NOT to answer (connect timeout, 0 bytes). Must never be the configured source.
DEAD = {'npmmirror.com'}


class ElectronMirrorTests(unittest.TestCase):
    def _entries(self):
        """[(mirror, customdir), ...] from the fallback list."""
        block = re.search(
            r'\$script:DesktopElectronFallbackMirrors\s*=\s*@\((.*?)\n\)', SRC, re.S)
        self.assertIsNotNone(
            block, 'DesktopElectronFallbackMirrors list is not defined')
        return re.findall(
            r'Mirror\s*=\s*"([^"]+)"\s*;\s*CustomDir\s*=\s*"([^"]*)"', block.group(1))

    def test_the_fallback_list_is_not_empty(self):
        self.assertTrue(self._entries(), 'fallback mirror list must have at least one entry')

    def test_each_mirror_is_a_verified_reachable_host(self):
        for mirror, _ in self._entries():
            host = re.sub(r'^https?://', '', mirror).split('/')[0].lower()
            self.assertIn(
                host, VERIFIED,
                f'Electron mirror {mirror!r} (host {host!r}) has no measured throughput from the '
                f'sandbox. Verified-reachable hosts: {sorted(VERIFIED)}. Re-measure before '
                f'changing this -- a mirror that times out is what broke the cold install.')

    def test_no_mirror_host_is_the_known_dead_one(self):
        for mirror, _ in self._entries():
            host = re.sub(r'^https?://', '', mirror).split('/')[0].lower()
            self.assertNotIn(
                host, DEAD,
                f'{host} timed out from inside the sandbox (0 bytes). This was the old default '
                f'and it made the Electron retry fail as well.')

    def test_each_mirror_ends_with_a_slash(self):
        for mirror, _ in self._entries():
            self.assertTrue(
                mirror.endswith('/'),
                f'mirror {mirror!r} is used by string concatenation; a missing trailing slash '
                f'produces ".../40.10.2electron-v40...zip" and a silent 404')

    def test_each_mirror_uses_the_bare_version_path(self):
        """Default CustomDir is "v"+version, which 404s on these mirrors."""
        for mirror, customdir in self._entries():
            self.assertEqual(
                customdir.strip(), '{{ version }}',
                f'mirror {mirror!r} must set CustomDir="{{{{ version }}}}" -- the URL is built as '
                f'<Mirror><CustomDir>/<filename> and the default "v"-prefixed dir does not exist '
                f'there.')

    def test_retry_loops_over_every_mirror(self):
        """One mirror is not enough: the first candidate may be blocked for a given subscriber."""
        loops = SRC.count('foreach ($cand in $script:DesktopElectronFallbackMirrors)')
        self.assertGreaterEqual(
            loops, 2,
            'both the dependency-install retry and the build retry must iterate the mirror list')

    def test_mirror_env_is_restored_after_use(self):
        """Leaking ELECTRON_MIRROR/ELECTRON_CUSTOM_DIR into later stages changes behaviour."""
        self.assertGreaterEqual(SRC.count('Set-ElectronMirrorEnv'), 3)
        self.assertGreaterEqual(SRC.count('Restore-ElectronMirrorEnv'), 3)

    def test_github_is_still_the_implicit_last_resort(self):
        """A mirror outage must not become a hard failure: with no EV set, @electron/get
        defaults to GitHub, which measured ~520 KB/s (works, just slow). So the retry must
        clear the env rather than pin it to a mirror."""
        self.assertIn('Restore-ElectronMirrorEnv -Prev $prevMirror', SRC,
                      'the env must be restored (not left pinned) so GitHub stays reachable')


    def test_huaweicloud_is_first_because_it_was_verified_in_sandbox(self):
        """npmmirror stalled inside the Sandbox while huaweicloud served the payload in 33s.

        The first candidate is what the pre-seed and npm ci use, so a swap silently restores
        the stall. Re-measure inside a real Sandbox before changing this order.
        """
        import re
        m = re.search(r'DesktopElectronFallbackMirrors\s*=\s*@\((.*?)\n\)', SRC, re.S)
        self.assertIsNotNone(m, 'mirror list not found')
        body = m.group(1)
        first = body.index('huaweicloud')
        second = body.index('npmmirror')
        self.assertLess(first, second,
                        'huaweicloud must stay first: it is the only mirror verified to serve '
                        'the payload from inside Windows Sandbox (33s); npmmirror stalled there')


if __name__ == '__main__':
    unittest.main(verbosity=2)
