"""Offline protocol tests. Never send an actionable install request to host."""
import json, os, subprocess, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PS = os.path.join(os.environ['SYSTEMROOT'], 'System32/WindowsPowerShell/v1.0/powershell.exe')

class WorkerTests(unittest.TestCase):
    def test_rejects_invalid_input_without_secret_output(self):
        path = ROOT/'backend/worker.ps1'
        self.assertTrue(path.exists(), 'NDJSON worker missing')
        for payload in ['not-json-secret-test-key', json.dumps({'protocol':2, 'api_key':'secret-test-key'}), json.dumps({'protocol':1,'action':'install','endpoint':'http://unsafe.test','api_key':'secret-test-key'})]:
            run = subprocess.run([PS,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path)], input=payload, text=True, encoding='utf-8',capture_output=True,timeout=15)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn('secret-test-key', run.stdout + run.stderr)
            events = [json.loads(line) for line in run.stdout.splitlines()]
            finals = [e for e in events if e['type'] in ('error','success')]
            self.assertEqual(len(finals),1)
            self.assertEqual(finals[0]['code'],'INPUT')

    def test_helper_install_and_desktop_failures(self):
        run = subprocess.run([PS,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ROOT/'tests/backend/check_helpers.ps1'),'-Backend',str(ROOT/'backend')],capture_output=True,text=True,timeout=20)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('offline_helpers_passed=5',run.stdout)

    def test_duplicate_run_is_busy_before_host_inspection(self):
        holder = subprocess.Popen([PS,'-NoProfile','-NonInteractive','-Command', "$s=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $m=New-Object Threading.Mutex($false,('Local\\HermesSubscriberSetup-'+$s)); $null=$m.WaitOne(); [Console]::WriteLine('ready'); $null=[Console]::ReadLine(); $m.ReleaseMutex(); $m.Dispose()"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(),'ready')
            payload = {'protocol':1,'action':'install','endpoint':'https://example.test/v1','api_key':'secret-test-key'}
            run = subprocess.run([PS,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ROOT/'backend/worker.ps1')],input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',timeout=20)
            events = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(events[-1]['code'],'BUSY')
            self.assertNotIn('secret-test-key',run.stdout+run.stderr)
        finally:
            holder.communicate('\n',timeout=10)

if __name__ == '__main__': unittest.main(verbosity=2)
