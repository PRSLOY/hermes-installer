"""Regression: a fresh git clone must not be made dirty by core.autocrlf.

Root cause of the cold-install `repository` failure (Windows Sandbox, 2026-09-21):
the fresh path cloned with Git for Windows' default core.autocrlf=true (CRLF tree),
then set core.autocrlf=false AFTER the tree existed, which made every text file look
locally modified. The pinned `git checkout --detach $Commit` then aborted with
"Your local changes would be overwritten by checkout" (exit 1), killing the install
before any download. Verified end to end: with the pin set at clone time and the
checkout forced, all 16 stages complete.

Guards the two exact edits so the defect cannot silently return.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / 'backend' / 'upstream' / 'install.ps1'


class RepositoryAutocrlfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL.read_text(encoding='utf-8-sig', errors='replace')

    def test_both_clone_attempts_pin_autocrlf_off(self):
        # SSH and HTTPS clone branches must both carry the pin, or the CRLF tree
        # reappears and the pinned checkout aborts again.
        self.assertIn('core.autocrlf=false clone --depth 1 --branch $Branch $RepoUrlSsh', self.text)
        self.assertIn('core.autocrlf=false clone --depth 1 --branch $Branch $RepoUrlHttps', self.text)

    def test_pinned_commit_checkout_is_forced(self):
        # The post-clone pin must be forced (-f), like the ZIP path already does,
        # so a dirty working tree can never block reaching the exact commit.
        self.assertIn('checkout -f --detach $Commit', self.text)

    def test_autocrlf_is_not_only_set_after_the_clone(self):
        # Setting the config only after the tree exists is exactly the bug; the
        # clone-time pin must precede the post-clone config pin.
        clone_at = self.text.find('core.autocrlf=false clone')
        post_clone = self.text.find('Pinning to commit $Commit')
        self.assertNotEqual(clone_at, -1)
        self.assertNotEqual(post_clone, -1)
        self.assertLess(clone_at, post_clone, 'clone-time autocrlf pin must come before the pin checkout block')


if __name__ == '__main__':
    unittest.main(verbosity=2)
