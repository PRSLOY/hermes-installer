"""Offline failure diagnostics: no vendor install and no host configuration."""
import json, os, subprocess, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PS = str(Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe')

class FailureDetailsTests(unittest.TestCase):
    def check(self, frames, exit_code):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/'result.json'
            data.write_text(json.dumps({'Code':exit_code,'Text':'\n'.join(json.dumps(f) for f in frames)}), encoding='utf-8')
            harness = Path(tmp)/'check.ps1'
            harness.write_text(". '"+str(ROOT/'backend/worker.ps1')+"'\n$r=Get-Content -Raw -LiteralPath '"+str(data)+"' | ConvertFrom-Json\ntry { Check-InstallResult $r; throw 'unexpected success' } catch { [Console]::WriteLine(($_.Exception.Message)) }", encoding='utf-8-sig')
            result = subprocess.run([PS,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(harness)],capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout

    def test_failed_stage_and_exit_code_without_raw_reason(self):
        output = self.check([{'stage':'uv','ok':False,'skipped':False,'reason':'private-test-token https://sensitive.example/secret'}], 7)
        self.assertIn('uv', output)
        self.assertIn('7', output)
        self.assertNotIn('private-test-token', output)
        self.assertNotIn('sensitive.example', output)

    def test_unknown_stage_is_not_echoed(self):
        output = self.check([{'stage':'private-test-token','ok':False,'skipped':False,'reason':'private-test-token'}], 9)
        self.assertIn('9', output)
        self.assertNotIn('private-test-token', output)

    def test_network_reason_becomes_actionable_without_echoing_it(self):
        output = self.check([{'stage':'repository','ok':False,'skipped':False,
                              'reason':'Failed to download repository (tried git clone SSH, HTTPS, and ZIP)'}], 1)
        self.assertIn('repository', output)
        self.assertIn('интернет', output)
        self.assertNotIn('Failed to download repository', output)

    def test_fetch_reason_maps_to_network_hint(self):
        output = self.check([{'stage':'repository','ok':False,'skipped':False,
                              'reason':'git fetch c712f06dcdd2 failed (exit 128)'}], 1)
        self.assertIn('сети', output)
        self.assertNotIn('git fetch', output)

    def test_repository_without_reason_still_gets_network_hint(self):
        output = self.check([{'stage':'repository','ok':False,'skipped':False}], 1)
        self.assertIn('интернет', output)

    def test_unknown_reason_stays_opaque(self):
        output = self.check([{'stage':'dependencies','ok':False,'skipped':False,'reason':'weird-internal-token'}], 1)
        self.assertNotIn('weird-internal-token', output)

    def test_download_stage_failure_gets_a_network_hint(self):
        for stage in ('uv','git','node','system-packages','repository','dependencies','node-deps','desktop'):
            with self.subTest(stage=stage):
                output = self.check([{'stage':stage,'ok':False,'skipped':False}], 1)
                self.assertIn('интернет', output)
                self.assertNotIn('свободное место', output)

    def test_desktop_stage_wins_over_generic_npm_reason(self):
        output = self.check([{'stage':'desktop','ok':False,'skipped':False,
                              'reason':'desktop workspace npm install failed (exit 1) -- see lines above for cause'}], 1)
        self.assertIn('Desktop', output)

if __name__ == '__main__': unittest.main(verbosity=2)
