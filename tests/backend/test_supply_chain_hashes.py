"""Mirrors and proxies may serve anything: every byte from them is checked against an
authentic hash before it runs.

  * PortableGit: Invoke-DownloadFromSources -Sha256 with the official git-for-windows digests.
  * Repository ZIP (GitHub or ghproxy/gh-proxy): the extracted tree must match
    backend/upstream/tree-manifest.sha256, built from the pinned commit by
    tools/tree_manifest.py (git content-addressing + git archive).
  * Electron: covered by electron's own checksums.json; the two holes are closed in install.ps1.

The PowerShell functions are executed for real: they are cut out of install.ps1 by the
PowerShell parser (AST) and dot-sourced next to stubs, never grepped.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / 'backend' / 'upstream' / 'install.ps1'
MANIFEST = ROOT / 'backend' / 'upstream' / 'tree-manifest.sha256'
PS = str(Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
sys.path.insert(0, str(ROOT / 'tools'))
import tree_manifest as tm  # noqa: E402

COMMIT = 'c712f06dcdd24053a4118f38d2090ac53137ecfc'

# Loads named functions from install.ps1 through the parser, not by text slicing.
LOAD = r'''
$ErrorActionPreference = 'Stop'
function Write-Info($m) { [Console]::WriteLine('INFO ' + $m) }
function Write-Warn($m) { [Console]::WriteLine('WARN ' + $m) }
function Write-Success($m) { [Console]::WriteLine('OK ' + $m) }
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('INSTALL_PATH', [ref]$tokens, [ref]$errors)
foreach ($name in @(FUNCS)) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if (-not $fn) { throw "function $name not found" }
    . ([ScriptBlock]::Create($fn.Extent.Text))
}
'''


def run_ps(body, funcs, tmp):
    script = LOAD.replace('INSTALL_PATH', str(INSTALL)).replace(
        'FUNCS', ','.join("'%s'" % f for f in funcs)) + body
    path = Path(tmp) / 'case.ps1'
    path.write_text(script, encoding='utf-8-sig')
    r = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                       capture_output=True, text=True, encoding='utf-8', timeout=120)
    return r.returncode, r.stdout + r.stderr


def sha(data):
    return hashlib.sha256(data).hexdigest()


class DownloadHashTests(unittest.TestCase):
    """Invoke-DownloadFromSources -Sha256: a wrong file is discarded and the next source tried."""

    STUB = r'''
$served = @{ 'https://github/x' = 'EVIL'; 'https://mirror1/x' = 'EVIL2'; 'https://mirror2/x' = 'GOOD' }
$script:tried = @()
function Invoke-DownloadWithRetry { param($Uri, $OutFile, $Label)
    $script:tried += $Uri
    [IO.File]::WriteAllText($OutFile, $served[$Uri]); return $true }
$out = Join-Path 'TMP' 'asset.bin'
'''

    def case(self, sources, expected_sha, extra=''):
        with tempfile.TemporaryDirectory() as tmp:
            body = self.STUB.replace('TMP', tmp) + r'''
$ok = Invoke-DownloadFromSources -Sources @(SOURCES) -OutFile $out -Label 'git' SHAARG
[Console]::WriteLine('RESULT ' + $ok)
[Console]::WriteLine('TRIED ' + ($script:tried -join ','))
[Console]::WriteLine('MISMATCH ' + $script:DownloadHashMismatch)
if (Test-Path $out) { [Console]::WriteLine('FILE ' + [IO.File]::ReadAllText($out)) } else { [Console]::WriteLine('FILE <none>') }
'''.replace('SOURCES', ','.join("'%s'" % s for s in sources)).replace(
                'SHAARG', ("-Sha256 '%s'" % expected_sha) if expected_sha else '') + extra
            code, out = run_ps(body, ['Invoke-DownloadFromSources'], tmp)
            self.assertEqual(code, 0, out)
            return out

    def test_mismatching_sources_are_skipped_until_the_matching_one(self):
        out = self.case(['https://github/x', 'https://mirror1/x', 'https://mirror2/x'], sha(b'GOOD'))
        self.assertIn('RESULT True', out)
        self.assertIn('TRIED https://github/x,https://mirror1/x,https://mirror2/x', out)
        self.assertIn('FILE GOOD', out)
        self.assertIn('failed the SHA-256 check', out)

    def test_correct_hash_on_first_source_succeeds_without_trying_more(self):
        out = self.case(['https://mirror2/x', 'https://github/x'], sha(b'GOOD').upper())
        self.assertIn('RESULT True', out)
        self.assertIn('TRIED https://mirror2/x\n', out.replace('\r', ''))
        self.assertIn('MISMATCH False', out)

    def test_all_sources_wrong_fails_and_leaves_no_file(self):
        out = self.case(['https://github/x', 'https://mirror1/x'], sha(b'GOOD'))
        self.assertIn('RESULT False', out)
        self.assertIn('MISMATCH True', out)
        self.assertIn('FILE <none>', out, 'an unverified self-extractor must not stay on disk')

    def test_without_sha256_the_first_complete_file_is_accepted(self):
        out = self.case(['https://github/x', 'https://mirror2/x'], None)
        self.assertIn('RESULT True', out)
        self.assertIn('FILE EVIL', out)


class PortableGitPinTests(unittest.TestCase):
    """The digests are the official ones (release notes of v2.54.0.windows.1) and are used."""

    OFFICIAL = {
        'PortableGit-2.54.0-64-bit.7z.exe': 'bea006a6cc69673f27b1647e84ab3a68e912fbc175ab6320c5987e012897f311',
        'PortableGit-2.54.0-arm64.7z.exe': 'f8e92cd3359fcbb96998cfd606a536ccc6dbfb23c04e12b29042f9ba45b6b0c7',
        'MinGit-2.54.0-32-bit.zip': '52fc36c9b22611f0a6a7fabdc68c763b914400e3af0e35ad822468dc64cb7981',
    }

    def test_table_holds_official_digests_and_the_download_uses_it(self):
        src = INSTALL.read_text(encoding='utf-8-sig')
        self.assertIn('$gitVer    = "2.54.0"', src, 'git version moved: refresh GitForWindowsSha256')
        for name, digest in self.OFFICIAL.items():
            self.assertIn("'%s'" % name, src)
            self.assertIn(digest, src)
        self.assertIn('-MinBytes 3000000 -Sha256 $gitSha256', src)


def make_tree(root, files):
    for rel, data in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


FILES = {
    'README.md': b'# hermes\n',
    'scripts/install.ps1': b'Write-Host hi\r\nexit 0\r\n',
    'hermes_cli/main.py': b'print("hi")\n',
}


class TreeManifestTests(unittest.TestCase):
    """Read-HermesTreeManifest + Test-HermesTreeManifest on a manifest rendered by the generator."""

    def verify(self, mutate=None, manifest_commit=COMMIT, manifest_bytes=None):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / 'hermes-agent-x'
            make_tree(tree, FILES)
            manifest = Path(tmp) / 'tree-manifest.sha256'
            manifest.write_bytes(manifest_bytes or tm.render(manifest_commit, {k: sha(v) for k, v in FILES.items()}))
            if mutate:
                mutate(tree)
            body = r'''
try {
    $exp = Read-HermesTreeManifest -Path 'MANIFEST' -Commit 'COMMIT'
    $problem = Test-HermesTreeManifest -Root 'TREE' -Expected $exp
    if ($problem) { [Console]::WriteLine('PROBLEM ' + $problem) } else { [Console]::WriteLine('MATCH ' + $exp.Count) }
} catch { [Console]::WriteLine('THROW ' + $_.Exception.Message) }
'''.replace('MANIFEST', str(manifest)).replace('COMMIT', COMMIT).replace('TREE', str(tree))
            code, out = run_ps(body, ['Read-HermesTreeManifest', 'Test-HermesTreeManifest'], tmp)
            self.assertEqual(code, 0, out)
            return out

    def test_identical_tree_matches(self):
        self.assertIn('MATCH 3', self.verify())

    def test_tampered_file_is_rejected(self):
        out = self.verify(lambda t: (t / 'hermes_cli/main.py').write_bytes(b'import os; os.system("x")\n'))
        self.assertIn('PROBLEM content mismatch hermes_cli/main.py', out)

    def test_line_ending_change_is_a_mismatch(self):
        out = self.verify(lambda t: (t / 'scripts/install.ps1').write_bytes(b'Write-Host hi\nexit 0\n'))
        self.assertIn('PROBLEM content mismatch scripts/install.ps1', out)

    def test_missing_file_is_rejected(self):
        out = self.verify(lambda t: (t / 'README.md').unlink())
        self.assertIn('PROBLEM 1 file(s) missing', out)

    def test_extra_file_is_rejected(self):
        """A dropped-in .pth/sitecustomize runs with the venv: extras are never ignored."""
        out = self.verify(lambda t: (t / 'hermes_cli/evil.pth').write_bytes(b'import evil\n'))
        self.assertIn('PROBLEM unexpected file hermes_cli/evil.pth', out)

    def test_manifest_for_another_commit_is_unusable(self):
        out = self.verify(manifest_commit='0' * 40)
        self.assertIn('THROW Hermes tree manifest unusable', out)

    def test_manifest_count_mismatch_is_unusable(self):
        good = tm.render(COMMIT, {k: sha(v) for k, v in FILES.items()})
        out = self.verify(manifest_bytes=good.replace(b'# files 3', b'# files 4'))
        self.assertIn('THROW Hermes tree manifest unusable', out)


class ZipRouteTests(unittest.TestCase):
    """The real ZIP block of Install-Repository: a tampered proxy is skipped, never used."""

    def run_zip_block(self, served):
        src = INSTALL.read_text(encoding='utf-8-sig')
        start = src.index('                $zipSources = @($zipUrl)')
        end = src.index('                if ($extractedDir) {', start)
        block = src[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / 'tree-manifest.sha256'
            manifest.write_bytes(tm.render(COMMIT, {k: sha(v) for k, v in FILES.items()}))
            zips = {}
            for i, files in enumerate(served):
                z = Path(tmp) / ('src%d.zip' % i)
                with zipfile.ZipFile(z, 'w') as archive:
                    for rel, data in files.items():
                        archive.writestr('hermes-agent-%s/%s' % (COMMIT, rel), data)
                zips['src%d' % i] = str(z)
            body = r'''
$Commit = 'COMMIT'
$zipUrl = 'https://github.com/x.zip'
$zipLabel = $Commit
$zipPath = Join-Path 'TMP' 'dl.zip'
$extractPath = Join-Path 'TMP' 'extract'
$script:HermesTreeManifestPath = 'MANIFEST'
$script:GitHubZipProxies = @(PROXIES)
$zips = @{ ZIPMAP }
function Invoke-DownloadFromSources { param($Sources, $OutFile, $Label, $MinBytes)
    $key = if ($Sources[0] -eq $zipUrl) { 'src0' } else { $Sources[0].Substring(0, 4) }
    [Console]::WriteLine('FETCH ' + $key)
    Copy-Item -LiteralPath $zips[$key] -Destination $OutFile -Force; return $true }
try {
BLOCK
    [Console]::WriteLine('USED ' + $extractedDir.Name)
    [Console]::WriteLine('README ' + (Test-Path (Join-Path $extractedDir.FullName 'README.md')))
} catch { [Console]::WriteLine('THROW ' + $_.Exception.Message) }
'''
            proxies = ','.join("'src%d'" % i for i in range(1, len(served)))
            zipmap = '; '.join("'%s' = '%s'" % (k, v) for k, v in zips.items())
            body = (body.replace('BLOCK', block).replace('COMMIT', COMMIT).replace('TMP', tmp)
                    .replace('MANIFEST', str(manifest)).replace('PROXIES', proxies).replace('ZIPMAP', zipmap))
            code, out = run_ps(body, ['Read-HermesTreeManifest', 'Test-HermesTreeManifest'], tmp)
            self.assertEqual(code, 0, out)
            return out

    def test_tampered_github_zip_then_good_proxy(self):
        evil = dict(FILES, **{'hermes_cli/main.py': b'evil\n'})
        out = self.run_zip_block([evil, FILES])
        self.assertIn('FETCH src0', out)
        self.assertIn('FETCH src1', out)
        self.assertIn('USED hermes-agent-%s' % COMMIT, out)
        self.assertIn('does not match the verified file list', out)

    def test_every_source_tampered_fails_closed(self):
        evil = dict(FILES, **{'extra.py': b'x\n'})
        out = self.run_zip_block([evil, evil, evil])
        self.assertIn('THROW Hermes tree manifest mismatch', out)
        self.assertNotIn('USED', out)


def git(repo, *args, stdin=None):
    return subprocess.run(['git', '-C', str(repo)] + list(args), input=stdin, capture_output=True, check=True).stdout


def local_commit(tmp, files, attributes=b'*.ps1 text eol=crlf\n', extra_entries=()):
    """A commit built with plumbing (no working tree, no autocrlf involvement)."""
    repo = Path(tmp) / 'origin'
    git(tmp, 'init', '-q', str(repo))
    git(repo, 'config', 'core.ignorecase', 'false')
    entries = []
    tree_files = [('100644', rel, data) for rel, data in sorted(dict(files, **{'.gitattributes': attributes}).items())]
    for mode, rel, data in tree_files + list(extra_entries):
        blob = git(repo, 'hash-object', '-w', '--stdin', stdin=data).decode().strip()
        entries.append('%s blob %s\t%s' % (mode, blob, rel))
    # Nested paths need nested trees: let update-index/write-tree build them.
    index = Path(tmp) / 'index'
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    for line in entries:
        mode_kind_sha, path = line.split('\t')
        mode, _, oid = mode_kind_sha.split(' ')
        subprocess.run(['git', '-C', str(repo), 'update-index', '--add', '--cacheinfo', '%s,%s,%s' % (mode, oid, path)],
                       env=env, check=True, capture_output=True)
    tree = subprocess.run(['git', '-C', str(repo), 'write-tree'], env=env, check=True, capture_output=True).stdout.decode().strip()
    env.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
               GIT_AUTHOR_DATE='2026-01-01T00:00:00Z', GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')
    commit = subprocess.run(['git', '-C', str(repo), 'commit-tree', tree, '-m', 'fixture'], env=env, check=True,
                            capture_output=True).stdout.decode().strip()
    git(repo, 'update-ref', 'refs/heads/main', commit)
    git(repo, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    return repo.as_uri(), commit


class GeneratorTests(unittest.TestCase):
    """tools/tree_manifest.py against a local repository (no network)."""

    LF_FILES = {'README.md': b'# x\n', 'scripts/install.ps1': b'Write-Host hi\nexit 0\n', 'a/b/c.py': b'pass\n'}

    def test_deterministic_and_in_archive_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            url, commit = local_commit(tmp, self.LF_FILES)
            first = tm.render(commit, tm.build(commit, url))
            second = tm.render(commit, tm.build(commit, url))
        self.assertEqual(first, second)
        text = first.decode('utf-8')
        self.assertIn('# commit %s\n# files 4\n' % commit, text)
        self.assertNotIn('\r', text)
        # eol=crlf applies to *.ps1 exactly as in GitHub's ZIP; other files stay byte-identical.
        self.assertIn('%s  scripts/install.ps1' % sha(b'Write-Host hi\r\nexit 0\r\n'), text)
        self.assertIn('%s  README.md' % sha(b'# x\n'), text)
        paths = [line.split('  ', 1)[1] for line in text.splitlines() if not line.startswith('#')]
        self.assertEqual(paths, sorted(paths, key=lambda p: p.encode('utf-8')))

    def test_export_ignore_aborts_instead_of_silently_excluding(self):
        with tempfile.TemporaryDirectory() as tmp:
            url, commit = local_commit(tmp, self.LF_FILES, attributes=b'README.md export-ignore\n')
            with self.assertRaisesRegex(tm.ManifestError, 'export-ignore'):
                tm.build(commit, url)

    def test_symlink_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            url, commit = local_commit(tmp, self.LF_FILES, extra_entries=[('120000', 'link', b'README.md')])
            with self.assertRaisesRegex(tm.ManifestError, 'unsupported tree entry 120000'):
                tm.build(commit, url)

    def test_case_collision_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            url, commit = local_commit(tmp, dict(self.LF_FILES, **{'Readme.md': b'other\n'}))
            with self.assertRaisesRegex(tm.ManifestError, 'case-insensitive'):
                tm.build(commit, url)


class ShippedManifestTests(unittest.TestCase):
    def test_manifest_describes_the_pin_and_is_well_formed(self):
        pin = (ROOT / 'backend/upstream/commit.txt').read_text().strip()
        data = MANIFEST.read_bytes()
        self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\r', data)
        lines = data.decode('utf-8').splitlines()
        self.assertIn('# commit %s' % pin, lines)
        body = [line for line in lines if not line.startswith('#')]
        self.assertIn('# files %d' % len(body), lines)
        self.assertGreater(len(body), 10000)
        paths = [line[66:] for line in body]
        self.assertEqual(len(paths), len(set(paths)))
        for line in body:
            self.assertRegex(line, r'^[0-9a-f]{64}  \S')

    def test_package_ships_and_checks_the_manifest(self):
        pkg = (ROOT / 'package-dist.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("'tree-manifest.sha256'", pkg)
        self.assertIn('tree_manifest.py', pkg)
        worker = (ROOT / 'backend/worker.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('Скачанный код Hermes не совпал с проверенной версией. Установка остановлена.', worker)


class ElectronChecksumTests(unittest.TestCase):
    """Electron zips from mirrors are validated against checksums.json of the lockfile-pinned
    electron package; these guard the two ways around that."""

    @classmethod
    def setUpClass(cls):
        cls.src = INSTALL.read_text(encoding='utf-8-sig')

    def test_remote_checksum_switch_is_cleared(self):
        self.assertIn('Remove-Item Env:electron_use_remote_checksums, Env:npm_config_electron_use_remote_checksums', self.src)

    def test_mirror_pack_requires_a_local_verified_dist(self):
        i = self.src.index('Re-downloading Electron via a public mirror')
        loop = self.src[i:i + 1500]
        guard = loop.index('if (-not (Test-ElectronDist -InstallDir $InstallDir)) { continue }')
        self.assertLess(guard, loop.index('& $npmExe run pack'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
