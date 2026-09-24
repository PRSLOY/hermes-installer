"""RED/GREEN: the desktop workspace install must survive a blocked Electron download,
and the get-windows repair must not be skipped just because npm exited non-zero.

Live evidence (Windows Sandbox run #2, 2026-09-18, package candidate-dist4). npm debug logs
from the desktop stage:

    verbose argv "ci" "--include" "optional"
    silly unfinished npm timer build:run:postinstall:apps/desktop/node_modules/electron
    error RequestError: connect ETIMEDOUT 140.82.121.3:443      <-- github.com (Electron payload)
    verbose exit 1
  then the fallback:
    verbose argv "install" "--include" "optional"
    (same ETIMEDOUT, same exit 1)

Consequences observed in the same sandbox:
    electron dist=False           <-- payload never arrived
    get-windows=False             <-- repair never ran (it was gated on $code -eq 0)
    build logs=0, release=False   <-- stage died later in stage-native-deps

So two things must hold:
  1. A re-attempt of the workspace install must set ELECTRON_MIRROR, because the Electron
     payload is fetched by a postinstall inside `npm ci`/`npm install` -- the mirror fallback
     that exists around `npm run pack` never covers it (that is the gap this test pins).
  2. The get-windows repair must run even when the install exited non-zero but Electron was
     self-healed, since a failed *optional* dependency leaves npm reporting a broken tree.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL_PS1 = Path(__file__).resolve().parents[2] / 'backend' / 'upstream' / 'install.ps1'
PS = str(Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')


def _extract_function(src, name):
    i = src.index('function ' + name)
    nxt = src.find('\nfunction ', i + 1)
    return src[i:nxt] if nxt != -1 else src[i:]


def _run_ps(script_text):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'run.ps1'
        p.write_text(script_text, encoding='utf-8-sig')
        return subprocess.run(
            [PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(p)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)


class DesktopInstallResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INSTALL_PS1.read_text(encoding='utf-8-sig')

    def block_between(self, start_marker, end_marker):
        start = self.src.index(start_marker)
        end = self.src.index(end_marker, start)
        return self.src[start:end]

    def test_workspace_install_retries_with_electron_mirror(self):
        """The desktop npm install must retry with ELECTRON_MIRROR on failure.

        The Electron payload is downloaded by a postinstall inside npm ci/install, so a
        GitHub-blocked link kills the install long before `npm run pack` -- the only place a
        mirror is currently applied.
        """
        i = self.src.index('$npmExe ci --include=optional')
        block = self.src[i - 200:i + 4000]
        self.assertIn('Set-ElectronMirrorEnv -Candidate $cand', block,
                      'the desktop install never retries with a mirrored Electron download, so a '
                      'blocked github.com download (observed: ETIMEDOUT 140.82.121.3:443 during '
                      'build:run:postinstall:apps/desktop/node_modules/electron) is fatal')
        # ...and the helper must actually export it, or the retry is a no-op that looks fine.
        helper = self.block_between('function Set-ElectronMirrorEnv', 'function Restore-ElectronMirrorEnv')
        self.assertIn('ELECTRON_MIRROR', helper,
                      'Set-ElectronMirrorEnv must set ELECTRON_MIRROR -- @electron/get reads that '
                      'variable; without it the retry downloads from GitHub again')
        self.assertIn('ELECTRON_CUSTOM_DIR', helper,
                      'Set-ElectronMirrorEnv must also set ELECTRON_CUSTOM_DIR, otherwise the '
                      'default "v"-prefixed path is requested and the mirror 404s')

    def test_mirror_is_used_for_the_first_attempt_not_just_the_retry(self):
        """The Electron payload is pulled by a postinstall INSIDE npm ci.

        Live evidence (2026-09-18, run #5): three consecutive installs died with
        `AggregateError [ETIMEDOUT]` against github.com while the candidate mirror answered in
        ~2s at ~2.8 MB/s. A mirror that only wrapped the retry never helped the attempt that
        mattered, because the first `npm ci` already fetched (and failed) the payload.

        The search is scoped to the enclosing function: a plain rindex finds an unrelated
        `Set-ElectronMirrorEnv` inside the Electron repair helper, so deleting the real setup
        went unnoticed (issue #14).
        """
        first = self.src.index('& $npmExe ci --include=optional')
        func_start = self.src.rfind('\nfunction ', 0, first)
        self.assertGreater(func_start, -1)
        between = self.src[func_start:first]
        self.assertIn('Set-ElectronMirrorEnv -Candidate $script:DesktopElectronFallbackMirrors',
                      between,
                      'nothing sets the Electron mirror in the same function before the first '
                      '`npm ci`, so the payload download still goes straight to a possibly-blocked '
                      'github.com')

    def test_github_remains_the_last_resort(self):
        """A mirror outage must not be able to block installation outright."""
        loop = self.block_between('Mirrors exhausted', 'Test-ElectronPkgStagedMissingDist')
        self.assertIn('ci --include=optional', loop,
                      'after the mirrors fail, the installer must fall back to the configured '
                      'default (GitHub) instead of giving up')
        # The default is GitHub only if the mirror env was cleared first.
        self.assertLess(loop.index('Restore-ElectronMirrorEnv'), loop.index('ci --include=optional'),
                        'the GitHub fallback must run with the mirror env restored/cleared')

    def test_electron_mirror_retry_uses_the_configured_fallback(self):
        i = self.src.index('$npmExe ci --include=optional')
        block = self.src[i - 200:i + 4000]
        self.assertIn('foreach ($cand in $script:DesktopElectronFallbackMirrors)', block,
                      'the retry must iterate the configured mirror list, not a hardcoded URL -- '
                      'a single mirror may be blocked for a given subscriber')

    def test_electron_mirror_is_restored_after_the_retry(self):
        """Leaking ELECTRON_MIRROR into later stages would change unrelated behaviour."""
        i = self.src.index('$npmExe ci --include=optional')
        block = self.src[i - 200:i + 4000]
        self.assertIn('Set-ElectronMirrorEnv -Candidate $cand', block,
                      'the retry must set the mirror env for the candidate under test')
        self.assertIn('Restore-ElectronMirrorEnv -Prev $prevInstallMirror', block,
                      'the retry must restore the env captured BEFORE the attempt, so GitHub '
                      'stays reachable as the implicit last resort')

    def test_missing_electron_still_fails_with_an_actionable_error(self):
        """If every attempt fails we must fail NOW, not in a confusing later stage."""
        i = self.src.index('$npmExe ci --include=optional')
        block = self.src[i - 200:i + 5000]
        self.assertIn('throw', block,
                      'an unrecoverable desktop dependency install must throw here instead of '
                      'continuing into the build with no Electron/optional payloads')

    def test_get_windows_repair_not_gated_on_zero_exit_alone(self):
        """A self-healed Electron download leaves $code non-zero but the tree repairable.

        Live 2026-09-18: the repair was inside `if ($code -eq 0)`, so after the Electron
        self-heal ($code non-zero) it never ran, and the build died later in
        stage-native-deps with "get-windows is not installed".
        """
        i = self.src.index('$gwRoots = @(')
        block = self.src[i - 1400:i]
        self.assertNotIn('if ($code -eq 0) {\n            # Probe both hoisted', block,
                         'the get-windows probe is still gated on a zero exit code, so a '
                         'self-healed (non-zero) desktop install skips the repair')
        self.assertIn('$desktopTreeUsable', block,
                      'the repair gate must consider whether the desktop tree is usable, not '
                      'only whether npm exited zero')

    def test_repair_probe_and_copy_still_present(self):
        """Guard against accidentally deleting the verified repair implementation."""
        self.assertIn('hermes-getwin-', self.src)
        self.assertIn('apps\\desktop\\node_modules\\get-windows', self.src)


class ElectronMirrorEnvExecutesTests(unittest.TestCase):
    """Run the real Set-/Restore-ElectronMirrorEnv helpers from install.ps1.

    Grepping for 'ELECTRON_MIRROR' only proves the string is present; executing the helper
    proves it actually exports the variable @electron/get reads and restores it afterwards
    (issue #14). This is the executable style of test_pin_fetch.py.
    """
    @classmethod
    def setUpClass(cls):
        cls.src = INSTALL_PS1.read_text(encoding='utf-8-sig')
        cls.set_fn = _extract_function(cls.src, 'Set-ElectronMirrorEnv')
        cls.restore_fn = _extract_function(cls.src, 'Restore-ElectronMirrorEnv')

    def test_helper_exports_and_restores_the_mirror(self):
        script = (
            self.set_fn + '\n' + self.restore_fn + '\n'
            "Remove-Item Env:ELECTRON_MIRROR -ErrorAction SilentlyContinue\n"
            "Remove-Item Env:ELECTRON_CUSTOM_DIR -ErrorAction SilentlyContinue\n"
            "$prev = Set-ElectronMirrorEnv -Candidate @{ Mirror = 'https://m.example/e/'; CustomDir = '{{ version }}' }\n"
            'Write-Output ("SET=" + $env:ELECTRON_MIRROR + "|" + $env:ELECTRON_CUSTOM_DIR)\n'
            'Restore-ElectronMirrorEnv -Prev $prev\n'
            'Write-Output ("RESTORED=" + $env:ELECTRON_MIRROR + "|" + $env:ELECTRON_CUSTOM_DIR)\n')
        r = _run_ps(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('SET=https://m.example/e/|{{ version }}', r.stdout,
                      '@electron/get reads ELECTRON_MIRROR/ELECTRON_CUSTOM_DIR; the helper must set both')
        self.assertIn('RESTORED=|', r.stdout,
                      'the mirror must be restored so it does not leak into later stages')


if __name__ == '__main__':
    unittest.main(verbosity=2)
