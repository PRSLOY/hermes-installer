"""SIMULATION: real OS child interruption, never vendor execution or API traffic."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from test_retry import ROOT, PS

HARNESS = r'''
param($Backend,$Root,$Mode)
. (Join-Path $Backend 'worker.ps1')
$env:LOCALAPPDATA=$Root; $env:HERMES_HOME=''
$original=${function:Run-Child}
function Run-Child($Exe,$Arguments,$InputText='', $Seconds=1200,[switch]$StreamStages) {
 if ($StreamStages) {
  [IO.File]::AppendAllText((Join-Path $Root 'pins.txt'),$Arguments[$Arguments.IndexOf('-Commit')+1]+"`n")
  return (& $original $env:RESUME_PYTHON @((Join-Path $Root 'fixture.py'),$Root,$Backend,$Mode) '' 30 -StreamStages)
 }
 return @{Code=0;Text='{"ok":true}'} # SIMULATED API result; no config write claimed
}
exit (Main)
'''
FIXTURE = '''import json,os,sys,time
from pathlib import Path
root,backend,mode=map(Path,sys.argv[1:])
h=root/'hermes'; r=h/'hermes-agent'
(root/'child.pid').write_text(str(os.getpid()))
(r/'.git').mkdir(parents=True)
(r/'.git/HEAD').write_bytes((backend/'upstream/commit.txt').read_bytes())
(r/'partial.txt').write_text('SIMULATED partial vendor download')
print(json.dumps(dict(stage='repository',ok=True,skipped=False)),flush=True)
if str(mode)=='interrupt':
    time.sleep(60)
for name in ['bin/hermes.exe','hermes-agent/venv/Scripts/python.exe','hermes-agent/apps/desktop/release/win-unpacked/Hermes.exe','hermes-agent/apps/desktop/release/win-unpacked/resources/app.asar','hermes-agent/apps/desktop/release/win-unpacked/icudtl.dat']:
    p=h/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'MZ-SIMULATION')
(r/'cli-config.yaml.example').write_bytes((backend/'upstream/cli-config.yaml.example').read_bytes())
(h/'config.yaml').write_bytes((r/'cli-config.yaml.example').read_bytes())
for stage in ['node','desktop','dependencies','venv']:
    print(json.dumps(dict(stage=stage,ok=True,skipped=False)),flush=True)
print(json.dumps(dict(protocol_version=1,ok=True)),flush=True)
'''

class ResumeTests(unittest.TestCase):
    def start(self, root, mode):
        (root/'harness.ps1').write_text(HARNESS,encoding='utf-8-sig')
        (root/'fixture.py').write_text(FIXTURE,encoding='utf-8')
        p=subprocess.Popen([PS,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(root/'harness.ps1'),str(ROOT/'backend'),str(root),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',env={**os.environ,'RESUME_PYTHON':os.sys.executable})
        p.stdin.write(json.dumps(dict(protocol=1,action='install',endpoint='https://example.invalid/v1',api_key='resume-fake-secret'))); p.stdin.close(); p.stdin=None
        return p

    def interrupted(self, root):
        p=self.start(root,'interrupt')
        seen=[]
        try:
            while True:
                line=p.stdout.readline()
                self.assertTrue(line,'worker exited before stage')
                seen.append(line)
                if json.loads(line).get('stage')=='repository': break
            # Production streaming has consumed/persisted this frame; child is still running.
            cmd=f". '{ROOT/'backend/worker.ps1'}'; $h=Assert-SafePath '{root/'hermes'}'; $r=Assert-SafePath '{root/'hermes/hermes-agent'}'; $s=Read-Journal $h $r (Get-Content '{ROOT/'backend/upstream/commit.txt'}' -Raw).Trim() ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value); $s | ConvertTo-Json -Compress"
            evidence=subprocess.run([PS,'-NoProfile','-Command',cmd],capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(evidence.returncode,0,evidence.stderr)
            checkpoint=json.loads(evidence.stdout)
            self.assertEqual(checkpoint['phase'],'installing')
            self.assertEqual(checkpoint['fingerprints'][1],'repository')
            self.assertGreater(int(checkpoint['fingerprints'][2]),0) # Python venv redirector may own a second PID
            self.assertNotIn('resume-fake-secret',evidence.stdout)
            live=subprocess.run([PS,'-NoProfile','-Command',cmd.rsplit('; $s |',1)[0]+"; try { Preserve-IncompleteInstall $s; exit 9 } catch { if ($_.Exception.Data['code'] -eq 'CONFIG') { exit 0 }; exit 8 }"],capture_output=True,timeout=10)
            self.assertEqual(live.returncode,0,live.stderr)
            self.assertFalse(list(root.glob('hermes.subscriber-preserved-*')))
            child=int((root/'child.pid').read_text())
            killed=subprocess.run(['taskkill.exe','/PID',str(child),'/F'],capture_output=True)
            self.assertEqual(killed.returncode,0,killed.stderr)
            out,err=p.communicate(timeout=15)
            seen.append(out)
            self.assertEqual(p.returncode,1)
            self.assertFalse(err,err)
            self.assertNotIn('"type":"success"',''.join(seen))
            return ''.join(seen)
        finally:
            if p.poll() is None:
                subprocess.run(['taskkill.exe','/PID',str(p.pid),'/T','/F'],capture_output=True)
                p.communicate(timeout=15)

    def test_real_child_killed_then_worker_resumes_simulation(self):
        with tempfile.TemporaryDirectory(prefix='subscriber-resume-') as tmp:
            root=Path(tmp); (root/'unrelated.txt').write_text('KEEP')
            first=self.interrupted(root)
            partial=(root/'hermes/hermes-agent/partial.txt').read_bytes()
            p=self.start(root,'complete'); out,err=p.communicate(timeout=30)
            self.assertEqual(p.returncode,0,(out,err))
            self.assertEqual(json.loads(out.splitlines()[-1])['type'],'success')
            self.assertEqual((root/'pins.txt').read_text().splitlines(),[(ROOT/'backend/upstream/commit.txt').read_text().strip()]*2)
            parked=list(root.glob('hermes.subscriber-preserved-*'))
            self.assertEqual(len(parked),1)
            self.assertEqual((parked[0]/'hermes-agent/partial.txt').read_bytes(),partial)
            self.assertEqual((root/'unrelated.txt').read_text(),'KEEP')
            self.assertNotIn('resume-fake-secret',first+out+err)
            self.assertNotIn(b'resume-fake-secret',(root/'hermes.subscriber-checkpoint.json').read_bytes())

    def test_changed_partial_and_invalid_marker_refuse(self):
        # 'changed', 'added' and 'completed' used to refuse too. An interrupted tree is now parked
        # aside untouched and reinstalled (test_changed_interrupted_tree_is_parked_and_reinstalled);
        # foreign settings/keys, a forged or foreign journal and a wrong pin still refuse.
        for mode in ['corrupt','foreign','config','env','pin','bad-snapshot']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(prefix='subscriber-resume-negative-') as tmp:
                root=Path(tmp); self.interrupted(root)
                marker=root/'hermes.subscriber-checkpoint.json'
                for label,name in [('config','config.yaml'),('env','.env'),('completed','hermes-agent/.hermes-bootstrap-complete')]:
                    if mode==label: (root/'hermes'/name).write_text('EXTERNAL KEEP')
                if mode=='changed': (root/'hermes/hermes-agent/partial.txt').write_text('EXTERNAL KEEP')
                if mode=='added': (root/'hermes/user.txt').write_text('EXTERNAL KEEP')
                if mode=='corrupt': marker.write_bytes(b'INVALID')
                if mode=='foreign': marker.unlink()
                if mode in ('pin','bad-snapshot'):
                    change="$s.revision='"+'0'*40+"'" if mode=='pin' else "$s.fingerprints=@('BAD','repository','1','1')"
                    cmd=f". '{ROOT/'backend/worker.ps1'}'; $h=Assert-SafePath '{root/'hermes'}'; $r=Assert-SafePath '{root/'hermes/hermes-agent'}'; $s=Read-Journal $h $r (Get-Content '{ROOT/'backend/upstream/commit.txt'}' -Raw).Trim() ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value); {change}; Write-Journal $s"
                    result=subprocess.run([PS,'-NoProfile','-Command',cmd],capture_output=True,timeout=10)
                    self.assertEqual(result.returncode,0,result.stderr)
                before={str(f.relative_to(root/'hermes')):f.read_bytes() for f in (root/'hermes').rglob('*') if f.is_file()}
                p=self.start(root,'complete'); out,err=p.communicate(timeout=30)
                self.assertEqual(p.returncode,1,(out,err))
                self.assertEqual(json.loads(out.splitlines()[-1])['code'],'CONFIG')
                self.assertEqual(len((root/'pins.txt').read_text().splitlines()),1)
                self.assertEqual(before,{str(f.relative_to(root/'hermes')):f.read_bytes() for f in (root/'hermes').rglob('*') if f.is_file()})

    def test_changed_interrupted_tree_is_parked_and_reinstalled(self):
        """After Cancel the tree differs from the lagging checkpoint: park it, install clean."""
        for mode in ['changed','added','completed']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(prefix='subscriber-resume-park-') as tmp:
                root=Path(tmp); self.interrupted(root)
                target={'changed':'hermes-agent/partial.txt','added':'user.txt','completed':'hermes-agent/.hermes-bootstrap-complete'}[mode]
                (root/'hermes'/target).write_text('INTERRUPTED STATE')
                p=self.start(root,'complete'); out,err=p.communicate(timeout=30)
                self.assertEqual(p.returncode,0,(out,err))
                self.assertEqual(len((root/'pins.txt').read_text().splitlines()),2)
                parked=[d for d in root.iterdir() if d.name.startswith('hermes.subscriber-preserved-')]
                self.assertEqual(len(parked),1)
                self.assertEqual((parked[0]/target).read_text(),'INTERRUPTED STATE')

if __name__=='__main__': unittest.main(verbosity=2)
