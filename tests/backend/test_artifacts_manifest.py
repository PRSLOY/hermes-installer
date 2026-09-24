"""Executable guards for the release-pinned artifact manifest (issue #15).

The manifest is embedded in HermesSetup.exe and handed to install.ps1 via
HERMES_ARTIFACTS_MANIFEST_B64. A downloaded artifact is accepted only when its
hash matches the pinned entry -- no size-based fallback, no missing-entry skip.

These tests both check the manifest's shape/consistency and RUN the real hash
helpers from install.ps1 against synthetic artifacts, so a broken verifier fails
here instead of on a user's machine.
"""
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / 'backend' / 'upstream'
INSTALL = UPSTREAM / 'install.ps1'
MANIFEST = UPSTREAM / 'artifacts.sha256.json'
COMMIT_TXT = UPSTREAM / 'commit.txt'
PS = str(Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')


def _helper_block(src):
    """The contiguous Subscriber-patch helper block that verifies artifacts."""
    start = src.index('function Get-ArtifactManifest')
    end = src.index("# Suppress Invoke-WebRequest's per-chunk progress bar.")
    return src[start:end]


def _fn(block, name):
    i = block.index('function ' + name)
    nxt = block.find('\nfunction ', i + 1)
    return block[i:nxt] if nxt != -1 else block[i:]


def _run_ps(script_text):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'run.ps1'
        p.write_text(script_text, encoding='utf-8-sig')
        return subprocess.run(
            [PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(p)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)


class ManifestShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(MANIFEST.read_text(encoding='utf-8-sig'))
        cls.by_id = {}
        for a in cls.doc['artifacts']:
            cls.by_id.setdefault(a['id'], []).append(a)

    def test_install_ps1_hash_is_pinned_to_the_vendored_file(self):
        actual = hashlib.sha256(INSTALL.read_bytes()).hexdigest()
        entry = self.by_id['install-ps1'][0]
        self.assertEqual(
            entry['sha256'], actual,
            'artifacts.sha256.json pins the wrong install.ps1 hash; '
            're-run tools/gen-artifacts-manifest.ps1 after editing install.ps1')

    def test_hermes_source_commit_matches_commit_txt(self):
        pinned = COMMIT_TXT.read_text(encoding='utf-8-sig').strip()
        entry = self.by_id['hermes-source'][0]
        self.assertEqual(entry['commit'], pinned,
                         'the manifest source commit and commit.txt disagree')

    def test_every_artifact_has_a_hash_and_sources(self):
        for a in self.doc['artifacts']:
            with self.subTest(artifact=a.get('id'), arch=a.get('arch')):
                # install-ps1 is the vendored file itself, not a download: no sources.
                if a['id'] != 'install-ps1':
                    self.assertTrue(a.get('sources'), 'no sources listed')
                key = 'content_sha256' if a['id'] == 'hermes-source' else 'sha256'
                self.assertRegex(a.get(key, ''), r'^[0-9a-f]{64}$',
                                 f'{key} must be a bare lowercase sha256')

    def test_archs_cover_the_installer(self):
        pairs = {(a['id'], a.get('arch')) for a in self.doc['artifacts']}
        for want in (('portablegit', 'x64'), ('portablegit', 'arm64'), ('portablegit', 'x86'),
                     ('node', 'x64'), ('node', 'arm64')):
            self.assertIn(want, pairs, f'manifest is missing {want}')


class VerifierExecutesTests(unittest.TestCase):
    """Run the real helpers from install.ps1 against synthetic artifacts."""
    @classmethod
    def setUpClass(cls):
        cls.src = INSTALL.read_text(encoding='utf-8-sig')
        cls.block = _helper_block(cls.src)

    def _script(self, body):
        parts = [_fn(self.block, n) for n in
                 ('Get-ArtifactManifest', 'Get-ArtifactEntry', 'Assert-ArtifactHash')]
        return '\n'.join(parts) + '\n' + body

    def test_content_hash_matches_the_documented_algorithm(self):
        fn = _fn(self.block, 'Get-ArchiveContentSha256')
        with tempfile.TemporaryDirectory() as tmp:
            zp = Path(tmp) / 'src.zip'
            with zipfile.ZipFile(zp, 'w') as z:
                z.writestr('repo-abc/file1.txt', 'hello')
                z.writestr('repo-abc/sub/file2.txt', 'world')
            # Python reference implementation of the same algorithm.
            with zipfile.ZipFile(zp) as z:
                items = [(n.split('/', 1)[1], hashlib.sha256(z.read(n)).hexdigest())
                         for n in z.namelist() if not n.endswith('/')]
            items.sort(key=lambda t: t[0])
            blob = ''.join(f'{rel}\x00{h}\n' for rel, h in items).encode()
            expected = hashlib.sha256(blob).hexdigest()

            r = _run_ps(fn + f'\nGet-ArchiveContentSha256 "{zp}"')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip().lower(), expected,
                             'install.ps1 hashes archives differently from the documented algorithm')

    def _manifest_b64(self, entries):
        doc = {'schema': 1, 'artifacts': entries}
        return base64.b64encode(json.dumps(doc).encode()).decode()

    def test_correct_hash_passes_and_wrong_hash_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'artifact.bin'
            f.write_bytes(b'payload')
            good = hashlib.sha256(b'payload').hexdigest()
            bad = '0' * 64
            template = self._script(
                '$env:HERMES_ARTIFACTS_MANIFEST_B64 = "@@B64@@"\n'
                f'try {{ Assert-ArtifactHash -Id "demo" -Arch "" -Path "{f}" -Label "demo" | Out-Null;'
                ' Write-Output "OK" } catch { Write-Output ("FAIL " + $_.Exception.Message) }')

            ok = _run_ps(template.replace('@@B64@@', self._manifest_b64([{'id': 'demo', 'sha256': good}])))
            self.assertIn('OK', ok.stdout, ok.stderr)

            wrong = _run_ps(template.replace('@@B64@@', self._manifest_b64([{'id': 'demo', 'sha256': bad}])))
            self.assertIn('FAIL', wrong.stdout, wrong.stderr)
            self.assertIn('does not match', wrong.stdout)

    def test_missing_entry_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'artifact.bin'
            f.write_bytes(b'payload')
            script = self._script(
                '$env:HERMES_ARTIFACTS_MANIFEST_B64 = "@@B64@@"\n'
                f'try {{ Assert-ArtifactHash -Id "demo" -Arch "x64" -Path "{f}" -Label "demo" | Out-Null;'
                ' Write-Output "OK" } catch { Write-Output ("FAIL " + $_.Exception.Message) }')
            # manifest has demo:x64 only for another arch -> x64 lookup must be fatal
            r = _run_ps(script.replace('@@B64@@', self._manifest_b64([{'id': 'demo', 'arch': 'arm64', 'sha256': 'a' * 64}])))
            self.assertIn('FAIL', r.stdout, r.stderr)
            self.assertIn('No pinned hash', r.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
