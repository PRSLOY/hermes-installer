"""RED/GREEN: npm >= 12 refuses to run dependency install scripts unless the project
opts in, and still exits 0 -- so electron's postinstall (which downloads the ~138MB
payload) never runs and the tree ends up without electron.exe.

Live evidence ON THE HOST (npm 12.0.2, fast link, so NOT a network artifact), 2026-09-18:
    $ npm install electron@40.10.2
    npm warn install-scripts 1 package had install scripts blocked because they are
      not covered by allowScripts: electron@40.10.2 (postinstall: node install.js)
    -> exit 0, "added 70 packages in 1s", but NO node_modules/electron/dist/electron.exe

Forms measured for the opt-in:
    .npmrc  allow-scripts[]=electron   -> postinstall ran, electron.exe 213,947,904 B, 5s  <= used
    package.json allowScripts          -> also works
    npm_config_allow_scripts env var    -> "not allowed in project-scoped installs"
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'backend' / 'upstream' / 'install.ps1').read_text(encoding='utf-8-sig')


class InstallScriptPolicyTests(unittest.TestCase):
    def test_approver_uses_the_npmrc_opt_in(self):
        i = SRC.index('function Approve-ElectronInstallScript')
        body = SRC[i:i + 2200]
        self.assertIn('allow-scripts[]=electron', body,
                      'must write the verified .npmrc opt-in')
        self.assertIn(".npmrc", body)
        self.assertIn("Join-Path $InstallDir '.npmrc'", body,
                      'npm reads .npmrc from the PROJECT ROOT (where npm ci runs), not the workspace subdir')

    def test_existing_npmrc_is_not_clobbered(self):
        i = SRC.index('function Approve-ElectronInstallScript')
        body = SRC[i:i + 2200]
        self.assertIn('allow-scripts', body)
        self.assertIn('Test-Path -LiteralPath $npmrc', body,
                      'must read the existing .npmrc before writing')

    def test_approver_runs_before_the_workspace_install(self):
        approve = SRC.index('Approve-ElectronInstallScript -NpmExe')
        ci = SRC.index('$npmExe ci --include=optional')
        self.assertLess(approve, ci,
                        'approval must happen BEFORE npm ci, or the postinstall is skipped')

    def test_seed_project_opts_in_too(self):
        """The pre-seed runs in a temp project, which needs its own opt-in on npm>=12."""
        i = SRC.index('function Initialize-ElectronPayloadCache')
        body = SRC[i:i + 3000]
        self.assertIn('allow-scripts[]=electron', body,
                      'the seed project must opt in, otherwise it seeds nothing on npm 12')

    def test_workspace_installs_use_foreground_scripts(self):
        for line in SRC.splitlines():
            if 'npmExe ci --include=optional' in line or 'npmExe install --include=optional' in line:
                self.assertIn('--foreground-scripts', line,
                              'postinstall output must be visible: ' + line.strip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
