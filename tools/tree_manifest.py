"""Build the file manifest of a pinned GitHub commit (Hermes by default).

Used for backend/upstream/tree-manifest.sha256 (Hermes, checked by install.ps1) and
assets/marketplaces/tree-manifest.sha256 (ru-marketplace-mcp, checked by extras.py).
Both installers may fetch the source as a ZIP through third-party GitHub proxies when
github.com is blocked. A proxy can serve anything, so the extracted tree is accepted only
if every file matches this manifest, which is generated here from an authentic source:

  1. `git fetch --depth=1 <github> <commit>`: git content-addresses every object, so the
     tree we hash IS the pinned commit, whoever carried the bytes.
  2. `git archive` of that commit: the exact bytes GitHub's archive ZIP contains
     (working-tree form: .gitattributes `eol=crlf` applies to *.ps1, nothing else is
     converted; autocrlf/eol are forced so the builder's own git config cannot leak in).
  3. Every blob in `git ls-tree -r` must appear in the archive and vice versa; symlinks,
     submodules, export-ignore/export-subst/filter attributes and case-only path
     collisions abort the build instead of being skipped silently.
  4. Optionally (--cross-check-zip) the real GitHub archive ZIP is downloaded and must
     match file for file.

Output is deterministic: sorted paths, LF, UTF-8 without BOM, no timestamps.

  python tools/tree_manifest.py generate --commit <sha> --out backend/upstream/tree-manifest.sha256 [--cross-check-zip]
  python tools/tree_manifest.py check    --commit <sha> --manifest backend/upstream/tree-manifest.sha256
  (another repo: add --repo-url https://github.com/<owner>/<name>.git --title "<...>" [--zip-url <codeload URL>])
Exit codes: 0 ok, 1 mismatch/invalid, 3 github.com unreachable (not verified).
"""
import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_URL = 'https://github.com/NousResearch/hermes-agent.git'
DEFAULT_TITLE = 'Hermes tree manifest'
# Not 2: Python itself exits 2 when the script file is missing ("can't open file").
NETWORK_EXIT = 3
REGULAR_MODES = {'100644', '100755'}
FORBIDDEN_ATTRS = ('export-ignore', 'export-subst', 'filter', 'ident', 'working-tree-encoding')
GIT = ['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', '-c', 'core.safecrlf=false']


class ManifestError(Exception):
    pass


class FetchError(ManifestError):
    """github.com unreachable: the manifest could not be checked, not proven wrong."""


def _git(repo, *args, stdin=None):
    result = subprocess.run(GIT + ['-C', str(repo)] + list(args), input=stdin, capture_output=True)
    if result.returncode != 0:
        raise ManifestError('git %s failed: %s' % (args[0], result.stderr.decode('utf-8', 'replace').strip()))
    return result.stdout


def fetch_commit(repo, commit, url=REPO_URL):
    _git(repo, 'init', '-q')
    try:
        _git(repo, 'fetch', '-q', '--depth=1', url, commit)
    except ManifestError as exc:
        raise FetchError(str(exc))
    head = _git(repo, 'rev-parse', 'FETCH_HEAD^{commit}').decode().strip()
    if head != commit:
        raise ManifestError('fetched %s, expected %s' % (head, commit))


def tree_entries(repo, commit):
    """Every path of the commit with its mode; non-regular entries abort the build."""
    entries = {}
    for record in _git(repo, 'ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        mode, kind, _ = meta.decode().split(' ')
        path = path.decode('utf-8')
        if kind != 'blob' or mode not in REGULAR_MODES:
            raise ManifestError('unsupported tree entry %s %s %s: install.ps1 cannot verify it' % (mode, kind, path))
        entries[path] = mode
    return entries


def check_attributes(repo, commit, paths):
    """Attributes that make the archive differ from the committed file are not allowed."""
    stdin = b''.join(p.encode('utf-8') + b'\0' for p in paths)
    out = _git(repo, 'check-attr', '-z', '--stdin', '--source', commit, *FORBIDDEN_ATTRS, stdin=stdin)
    fields = out.split(b'\0')
    bad = []
    for i in range(0, len(fields) - 2, 3):
        path, attr, value = (f.decode('utf-8') for f in fields[i:i + 3])
        if value != 'unspecified':
            bad.append('%s: %s=%s' % (path, attr, value))
    if bad:
        raise ManifestError('attributes change archive bytes: ' + '; '.join(bad[:10]))


def archive_hashes(repo, commit):
    data = _git(repo, 'archive', '--format=tar', commit)
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            if member.isdir() or member.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                continue
            if not member.isfile():
                raise ManifestError('archive holds a non-regular entry: %s' % member.name)
            hashes[member.name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    return hashes


def build(commit, url=REPO_URL):
    if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise ManifestError('commit must be a full lowercase SHA-1')
    with tempfile.TemporaryDirectory(prefix='hermes-manifest-') as tmp:
        fetch_commit(tmp, commit, url)
        entries = tree_entries(tmp, commit)
        check_attributes(tmp, commit, sorted(entries))
        hashes = archive_hashes(tmp, commit)
    if set(hashes) != set(entries):
        only_tree = sorted(set(entries) - set(hashes))[:10]
        only_archive = sorted(set(hashes) - set(entries))[:10]
        raise ManifestError('archive and tree disagree: tree-only %s, archive-only %s' % (only_tree, only_archive))
    folded = {}
    for path in hashes:
        other = folded.setdefault(path.lower(), path)
        if other != path:
            raise ManifestError('paths collide on a case-insensitive disk: %s / %s' % (other, path))
    return hashes


def render(commit, hashes, title=DEFAULT_TITLE):
    lines = [
        '# %s: SHA-256 of every file of the pinned commit, as `git archive`' % title,
        '# (= the GitHub archive ZIP) produces it. Generated by tools/tree_manifest.py; do not edit.',
        '# commit %s' % commit,
        '# files %d' % len(hashes),
    ]
    lines += ['%s  %s' % (hashes[p], p) for p in sorted(hashes, key=lambda p: p.encode('utf-8'))]
    return ('\n'.join(lines) + '\n').encode('utf-8')


def repo_name(repo_url):
    """'https://github.com/owner/name.git' -> 'name' (the top folder of GitHub ZIPs is name-<commit>)."""
    name = repo_url.rstrip('/').rsplit('/', 1)[-1]
    return name[:-4] if name.endswith('.git') else name


def default_zip_url(repo_url, commit):
    base = repo_url.rstrip('/')
    base = base[:-4] if base.endswith('.git') else base
    return '%s/archive/%s.zip' % (base, commit)


def cross_check_zip(commit, hashes, url=None, repo_url=REPO_URL):
    """The real GitHub archive must contain exactly the manifest's bytes."""
    url = url or default_zip_url(repo_url, commit)
    try:
        data = urllib.request.urlopen(url, timeout=300).read()
    except OSError as exc:
        raise FetchError('ZIP %s: %s' % (url, exc))
    prefix = '%s-%s/' % (repo_name(repo_url), commit)
    seen = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not info.filename.startswith(prefix):
                raise ManifestError('unexpected ZIP entry %s' % info.filename)
            seen[info.filename[len(prefix):]] = hashlib.sha256(archive.read(info)).hexdigest()
    if seen != hashes:
        diff = sorted(p for p in set(seen) | set(hashes) if seen.get(p) != hashes.get(p))
        raise ManifestError('GitHub ZIP differs from git archive in %d file(s): %s' % (len(diff), diff[:10]))
    return len(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('generate', 'check'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--commit', required=True)
        cmd.add_argument('--repo-url', default=REPO_URL, help='git URL (default: Hermes)')
        cmd.add_argument('--title', default=DEFAULT_TITLE, help='first header line')
        if name == 'generate':
            cmd.add_argument('--out', required=True)
            cmd.add_argument('--cross-check-zip', action='store_true')
            cmd.add_argument('--zip-url', help='archive to cross-check (default: <repo>/archive/<commit>.zip)')
        else:
            cmd.add_argument('--manifest', required=True)
    args = parser.parse_args(argv)
    try:
        hashes = build(args.commit, args.repo_url)
        content = render(args.commit, hashes, args.title)
        if args.cmd == 'generate':
            if args.cross_check_zip:
                size = cross_check_zip(args.commit, hashes, args.zip_url, args.repo_url)
                print('GitHub ZIP cross-check: OK (%d bytes)' % size)
            Path(args.out).write_bytes(content)
            print('tree manifest: %d files, sha256 %s' % (len(hashes), hashlib.sha256(content).hexdigest()))
        else:
            if Path(args.manifest).read_bytes() != content:
                print('tree manifest does NOT match commit %s; regenerate it' % args.commit, file=sys.stderr)
                return 1
            print('tree manifest: OK (%d files, %s)' % (len(hashes), args.commit))
    except FetchError as exc:
        print('tree manifest NOT verified (network): %s' % exc, file=sys.stderr)
        return NETWORK_EXIT
    except ManifestError as exc:
        print('tree manifest: %s' % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
