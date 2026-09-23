"""Every user-facing message must be Russian, not a raw English sentence.

The installer talks to non-technical subscribers. A message that reaches the UI
comes from exactly three places: Fail(...) in the PowerShell backend, Send-Event
progress/error payloads, and Failure(...) in the Python helper. This test walks
those call sites and rejects any sentence-like literal that has no Cyrillic --
which is how the English recovery messages ("No usable live install checkpoint")
slipped through before. Short technical tokens (API, HTTPS, Hermes, python.exe)
are allowed inside Russian text; what is forbidden is a whole sentence in Latin.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PS_FILES = ['backend/worker.ps1', 'backend/checkpoint.ps1', 'backend/streaming.ps1', 'backend/protect.ps1']
PY_FILES = ['backend/configure.py', 'backend/provider.py']
CYRILLIC = re.compile(r'[\u0400-\u04FF]')
LATIN_WORD = re.compile(r'[A-Za-z]{3,}')
QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


class UserMessageLocalizationTests(unittest.TestCase):
    def _literals(self, line):
        for single, double in QUOTED.findall(line):
            text = single or double
            if len(text) >= 15 and ' ' in text:
                yield text

    def _assert_russian(self, path, line_no, text):
        self.assertTrue(CYRILLIC.search(text),
                        f'{path}:{line_no}: user-facing message is not in Russian: {text!r}')

    def test_backend_messages_are_russian(self):
        checked = 0
        for rel in PS_FILES:
            path = ROOT / rel
            for line_no, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
                if not any(k in line for k in ('Fail ', 'Send-Event', 'message')):
                    continue
                for text in self._literals(line):
                    if LATIN_WORD.search(text):
                        self._assert_russian(rel, line_no, text)
                        checked += 1
        self.assertGreater(checked, 10, 'localization scan found too few messages to be meaningful')

    def test_python_messages_are_russian(self):
        checked = 0
        for rel in PY_FILES:
            path = ROOT / rel
            for line_no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if 'Failure(' not in line:
                    continue
                for text in self._literals(line):
                    if LATIN_WORD.search(text):
                        self._assert_russian(rel, line_no, text)
                        checked += 1
        self.assertGreater(checked, 5, 'localization scan found too few messages to be meaningful')


if __name__ == '__main__':
    unittest.main(verbosity=2)
