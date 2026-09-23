"""Build from source in a disposable directory, without existing dist files.
Does not run installer or modify host Hermes configuration.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class CleanPackageTest(unittest.TestCase):
    def test_package_builds_without_existing_dist(self):
        with tempfile.TemporaryDirectory(prefix='hermes-clean-package-') as folder:
            work = Path(folder)
            for name in ('ui', 'backend'):
                shutil.copytree(ROOT / name, work / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for name in ('build-ui.ps1', 'package-dist.ps1', 'providers.json.template', 'user-readme.txt'):
                if (ROOT / name).exists():
                    shutil.copy2(ROOT / name, work / name)
            ps = str(Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(work / 'package-dist.ps1')], cwd=work, capture_output=True, timeout=60)
            output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, output)
            dist = work / 'dist'
            catalog = json.loads((dist / 'providers.json').read_text(encoding='utf-8-sig'))
            presets = catalog['providers']
            self.assertEqual([p['id'] for p in presets], ['openrouter', 'custom'])
            self.assertEqual(presets[0]['endpoint'], 'https://openrouter.ai/api/v1')
            self.assertTrue((dist / 'HermesSetup.exe').is_file())
            self.assertTrue((dist / 'backend/worker.ps1').is_file())
            # A novice must get a double-click entry point and instructions in every build.
            self.assertTrue((dist / 'Как установить.cmd').is_file())
            self.assertTrue((dist / 'Прочти меня.txt').is_file())

if __name__ == '__main__':
    unittest.main(verbosity=2)
