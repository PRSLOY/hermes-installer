"""Guard the non-GitHub install routes for networks that block github.com.

Russian consumer links routinely block/throttle github.com, which broke the `git`
(PortableGit) and `repository` stages. The installer now tries direct GitHub first
and then falls back to mirrors/proxies verified reachable from Russia (2026-09-21):
  * PortableGit  -> registry.npmmirror.com and mirrors.huaweicloud.com
  * repo archive -> ghproxy.net and gh-proxy.com
and records the pinned commit in a marker file for the worker's revision check when
the git fetch could not run. These are structural guards; live behaviour is proven
by the blocked-GitHub Sandbox run.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / 'backend' / 'upstream' / 'install.ps1'
WORKER = ROOT / 'backend' / 'worker.ps1'


class NonGitHubRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL.read_text(encoding='utf-8-sig', errors='replace')
        cls.worker = WORKER.read_text(encoding='utf-8-sig', errors='replace')

    def test_git_mirrors_present(self):
        self.assertIn('registry.npmmirror.com/-/binary/git-for-windows/', self.install)
        self.assertIn('mirrors.huaweicloud.com/git-for-windows/', self.install)
        self.assertIn('Invoke-DownloadFromSources', self.install)

    def test_repo_archive_proxies_present(self):
        self.assertIn('ghproxy.net', self.install)
        self.assertIn('gh-proxy.com', self.install)

    def test_zip_path_records_pin_and_seeds_github_sha(self):
        self.assertIn('.hermes-subscriber-pin', self.install)
        self.assertIn('$env:GITHUB_SHA = $Commit', self.install)

    def test_worker_accepts_pin_marker(self):
        self.assertIn('.hermes-subscriber-pin', self.worker)
        self.assertIn('$markerPin -cne $pin', self.worker)


if __name__ == '__main__':
    unittest.main(verbosity=2)
