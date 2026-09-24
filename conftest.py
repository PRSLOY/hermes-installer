"""Repo-root conftest: make test imports independent of the invocation directory.

Some backend tests import the package as ``from backend import provider`` and some
import sibling test modules (``from test_retry import ...``), so both the repo root
and ``tests/backend`` must be importable. Without this, only
``python -m pytest`` started from the repo root happened to work, because ``-m``
puts the current directory on ``sys.path`` (issue #17).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _path in (ROOT, ROOT / "tests" / "backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
