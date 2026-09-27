"""voice-summary skill: transcribe.py drives Hermes' own transcribe_audio().

A stub Hermes tree (tools/transcription_tools.py) stands in for the real one; the
real local faster-whisper path is exercised in the sandbox, not here.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'assets' / 'skills' / 'voice-summary' / 'scripts' / 'transcribe.py'

STUB = '''
def transcribe_audio(path, model=None, source=None):
    import os
    mode = os.environ.get('STUB_MODE', 'ok')
    if mode == 'fail':
        return {"success": False, "transcript": "", "error": "no STT provider"}
    if mode == 'empty':
        return {"success": True, "transcript": "   "}
    return {"success": True, "transcript": "Привет, это запись встречи. Договорились на пятницу.", "provider": "local"}
'''


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='голос ')
        root = Path(self.tmp.name)
        self.home = root / 'hermes'
        tools = self.home / 'hermes-agent' / 'tools'
        tools.mkdir(parents=True)
        (tools / '__init__.py').write_text('', encoding='utf-8')
        (tools / 'transcription_tools.py').write_text(STUB, encoding='utf-8')
        self.audio = root / 'встреча.ogg'
        self.audio.write_bytes(b'OggS fake')

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, mode='ok'):
        env = dict(os.environ, HERMES_HOME=str(self.home), STUB_MODE=mode, PYTHONIOENCODING='utf-8')
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              encoding='utf-8', env=env, timeout=60)

    def test_transcript_saved_next_to_the_audio(self):
        r = self.run_script(str(self.audio))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        saved = self.audio.with_name('встреча.transcript.txt')
        self.assertIn('Договорились на пятницу', saved.read_text(encoding='utf-8'))
        self.assertIn('Готово (local)', r.stdout)

    def test_custom_out_path(self):
        out = self.audio.with_name('итог.txt')
        r = self.run_script(str(self.audio), '--out', str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(out.is_file())

    def test_failure_and_silence_are_reported_not_invented(self):
        r = self.run_script(str(self.audio), mode='fail')
        self.assertEqual(r.returncode, 1)
        self.assertIn('Расшифровать не удалось: no STT provider', r.stdout)
        r = self.run_script(str(self.audio), mode='empty')
        self.assertEqual(r.returncode, 1)
        self.assertIn('пустой результат', r.stdout)

    def test_missing_file(self):
        r = self.run_script(str(self.audio.with_name('нет.ogg')))
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
