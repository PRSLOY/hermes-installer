"""Digests of every SOUL.md and skill folder this repository ever shipped.

extras.py updates a skill (or SOUL.md) on an existing install only when it is
untouched: its current content equals what the installer put there. Installs
made since the set marker (<home>.subscriber-set.json) record that digest
themselves. Older installs have no marker, so their untouched copies are
recognised by matching ANY version committed here before the marker existed:
assets/skills/shipped-history.json. A folder that matches no shipped version
is the user's (edited or their own) and is never replaced.

Digests use extras.content_hash / extras.digest_rows (CRLF folded to LF, so a
package built from an autocrlf checkout matches the committed blob).

The file only has to cover installs made before the marker, so it is generated
once and does not need regenerating when a skill changes later.

  python tools/shipped_history.py generate   # rewrite assets/skills/shipped-history.json
"""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import extras  # noqa: E402

OUT = ROOT / 'assets' / 'skills' / 'shipped-history.json'
GIT = ['git', '-c', 'core.autocrlf=false', '-C', str(ROOT)]


def git(*args):
    return subprocess.run(GIT + list(args), capture_output=True, check=True).stdout


def blobs(commit):
    """{repo path: bytes} of assets/SOUL.md and assets/skills/** at commit."""
    out = {}
    listing = git('ls-tree', '-r', '-z', commit, '--', 'assets/SOUL.md', 'assets/skills')
    for record in listing.split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        _, kind, sha = meta.decode().split(' ')
        if kind == 'blob':
            out[path.decode('utf-8')] = git('cat-file', 'blob', sha)
    return out


def build():
    commits = git('rev-list', 'HEAD', '--', 'assets/SOUL.md', 'assets/skills').decode().split()
    soul, skills = set(), {}
    for commit in commits:
        files = blobs(commit)
        if 'assets/SOUL.md' in files:
            soul.add(extras.content_hash(files['assets/SOUL.md']))
        rows = {}
        for path, data in files.items():
            parts = path.split('/')
            if len(parts) < 4 or parts[1] != 'skills' or extras.skipped_part(parts[3:]):
                continue
            rows.setdefault(parts[2], {})['/'.join(parts[3:])] = extras.content_hash(data)
        for name, files_of_skill in rows.items():
            if 'SKILL.md' in files_of_skill:
                skills.setdefault(name, set()).add(extras.digest_rows(files_of_skill))
    return {'schema': 1, 'soul': sorted(soul), 'skills': {n: sorted(d) for n, d in sorted(skills.items())}}


def render(data):
    return (json.dumps(data, indent=1, sort_keys=True) + '\n').encode('utf-8')


def main(argv):
    content = render(build())
    if argv[1:2] == ['generate']:
        OUT.write_bytes(content)
        print('shipped history: %s' % OUT)
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
