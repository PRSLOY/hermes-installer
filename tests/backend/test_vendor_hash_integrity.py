"""Guards the vendor-file integrity check that gates the whole installer.

Why this exists: `backend/upstream/install.ps1` is a vendored file whose SHA-256 must match
`backend/upstream/install.sha256`, otherwise the worker refuses to run it -- by design, so a
tampered installer cannot execute.

That design has a sharp edge for US: every subscriber patch we make to install.ps1 invalidates
the recorded hash, and the symptom is not "hash mismatch" but 28 unrelated backend tests failing
at once (the worker simply will not start). Cost me a full debugging cycle on 2026-09-18.

This test fails with the exact fix command instead of letting that happen silently.
"""
import hashlib
import unittest
from pathlib import Path

UPSTREAM = Path(__file__).resolve().parents[2] / 'backend' / 'upstream'
INSTALL = UPSTREAM / 'install.ps1'
RECORDED = UPSTREAM / 'install.sha256'


class VendorHashIntegrityTests(unittest.TestCase):
    def test_install_ps1_hash_matches_recorded_value(self):
        self.assertTrue(INSTALL.is_file(), f'missing {INSTALL}')
        self.assertTrue(RECORDED.is_file(), f'missing {RECORDED}')
        actual = hashlib.sha256(INSTALL.read_bytes()).hexdigest()
        recorded = RECORDED.read_text(encoding='utf-8-sig').strip().lower()
        self.assertEqual(
            actual, recorded,
            'backend/upstream/install.ps1 was edited but backend/upstream/install.sha256 was not '
            'updated. The worker verifies this hash and will refuse to run the installer, which '
            'surfaces as ~28 unrelated backend test failures.\n'
            f'Fix: printf \'{actual}\\n\' > backend/upstream/install.sha256')

    def test_recorded_hash_is_a_bare_lowercase_sha256(self):
        recorded = RECORDED.read_text(encoding='utf-8-sig').strip()
        self.assertRegex(recorded, r'^[0-9a-f]{64}$',
                         'install.sha256 must contain exactly one lowercase hex digest')


if __name__ == '__main__':
    unittest.main(verbosity=2)
