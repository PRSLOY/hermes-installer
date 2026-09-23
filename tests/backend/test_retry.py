"""Offline real worker Main; child installs/API are doubles, never host installs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[2]
PS = str(Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
HARNESS = r'''
param($Backend, $Root, $Outcome)
. (Join-Path $Backend 'worker.ps1')
$env:LOCALAPPDATA=$Root
$env:HERMES_HOME=''
function Run-Child($Exe, $Arguments, $InputText='', $Seconds=1200, [switch]$StreamStages) {
    if ($StreamStages) {
        [IO.File]::AppendAllText((Join-Path $Root 'calls.txt'), "install`n")
        $h=Join-Path $Root 'hermes'; $r=Join-Path $h 'hermes-agent'
        foreach ($p in @('bin\hermes.exe','hermes-agent\venv\Scripts\python.exe','hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe','hermes-agent\apps\desktop\release\win-unpacked\resources\app.asar','hermes-agent\apps\desktop\release\win-unpacked\icudtl.dat')) {
            $f=Join-Path $h $p; [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($f)); [IO.File]::WriteAllText($f,'MZ-double')
        }
        [void][IO.Directory]::CreateDirectory((Join-Path $r '.git'))
        [IO.File]::WriteAllText((Join-Path $r '.git\HEAD'), (Get-Content (Join-Path $Backend 'upstream\commit.txt') -Raw).Trim())
        Copy-Item (Join-Path $Backend 'upstream\cli-config.yaml.example') (Join-Path $r 'cli-config.yaml.example')
        Copy-Item (Join-Path $r 'cli-config.yaml.example') (Join-Path $h 'config.yaml')
        if ($Outcome -eq 'PARTIAL') { return @{Code=1;Text=''} }
        $lines=@('{"protocol_version":1,"ok":true}')
        foreach ($s in @('node','desktop','dependencies','repository','venv')) { $lines += (@{stage=$s;ok=$true;skipped=$false}|ConvertTo-Json -Compress) }
        return @{Code=0;Text=($lines -join "`n")}
    }
    # The optional post-configure set (extras.py, incl. --marketplaces) is not
    # under test here: answer it offline instead of feeding it to the API double.
    if ([IO.Path]::GetFileName([string]$Arguments[0]) -eq 'extras.py') { return @{Code=0;Text='{"ok":true,"marketplaces":"exists"}'} }
    $d=$InputText|ConvertFrom-Json
    $label=if ($d.api_key -eq 'corrected-fake-key') {'corrected'} else {'initial'}
    [IO.File]::AppendAllText((Join-Path $Root 'calls.txt'), "configure:$label`n")
    if ($Outcome -eq 'TIMEOUT') { Fail 'NETWORK' 'offline timeout double' }
    $text = $InputText | & $env:RETRY_TEST_PYTHON (Join-Path $Root 'configure_double.py') $Backend (Join-Path $Root 'hermes') $Outcome
    return @{Code=$LASTEXITCODE;Text=($text -join "`n")}
}
exit (Main)
'''

class RetryTests(unittest.TestCase):
    def run_worker(self, root, outcome, corrected=False):
        harness = root/'harness.ps1'
        harness.write_text(HARNESS, encoding='utf-8-sig')
        (root/'configure_double.py').write_text("""import sys,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import configure
from provider import Failure
data=json.load(sys.stdin)
def probe(*args):
    raise Failure(sys.argv[3], 'offline API double')
try:
    with patch.object(configure, 'select_model', probe):
        configure.main(Path(sys.argv[2]),Path(sys.argv[2])/'hermes-agent',data)
except Failure as e:
    if e.code == 'OK':
        print(json.dumps(dict(ok=True)))
        sys.exit(0)
    print(json.dumps(dict(ok=False,code=e.code,message=str(e))))
    sys.exit(1)
""", encoding='utf-8')
        payload = dict(protocol=1, action='install', endpoint='https://example.invalid/v1',
                       api_key='corrected-fake-key' if corrected else 'initial-fake-key', fresh=True)
        p = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                            '-File', str(harness), str(ROOT/'backend'), str(root), outcome],
                           input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8', timeout=30,
                           env={**os.environ, 'RETRY_TEST_PYTHON':sys.executable})
        self.assertFalse(p.stderr, p.stderr)
        events=[json.loads(x) for x in p.stdout.splitlines()]
        return events[-1]

    def test_damaged_checkpoint_and_external_edits_fail_closed(self):
        for kind in ('missing', 'corrupt', 'foreign', 'config', 'env', 'head', 'exe', 'partial', 'junction', 'phase', 'revision', 'schema', 'extra', 'owner', 'fingerprints'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='subscriber-negative-') as tmp:
                root=Path(tmp)
                first='PARTIAL' if kind=='partial' else 'AUTH'
                self.assertEqual(self.run_worker(root, first)['code'], 'INSTALL' if kind=='partial' else 'AUTH')
                marker=root/'hermes.subscriber-checkpoint.json'
                if kind=='missing': marker.unlink()
                if kind=='corrupt': marker.write_bytes(b'broken checkpoint')
                if kind=='foreign':
                    with tempfile.TemporaryDirectory(prefix='subscriber-other-') as other:
                        other=Path(other)
                        self.assertEqual(self.run_worker(other, 'AUTH')['code'], 'AUTH')
                        marker.write_bytes((other/marker.name).read_bytes())
                for k,rel in [('config','config.yaml'),('env','.env'),('head','hermes-agent/.git/HEAD'),('exe','bin/hermes.exe')]:
                    if kind==k: (root/'hermes'/rel).write_text('EXTERNAL KEEP')
                if kind in ('phase', 'revision', 'schema', 'extra', 'owner', 'fingerprints'):
                    mutations={'phase':"$s.phase='unknown'", 'revision':"$s.revision='0000000000000000000000000000000000000000'",
                               'schema':"$s.schema='1'", 'extra':"$s|Add-Member extra 'unexpected'", 'owner':"$s.owner='foreign'", 'fingerprints':"$s.fingerprints=@('absent')"}
                    cmd=f". '{ROOT/'backend/worker.ps1'}'; $h=Assert-SafePath '{root/'hermes'}'; $r=Assert-SafePath '{root/'hermes/hermes-agent'}'; $pin=(Get-Content '{ROOT/'backend/upstream/commit.txt'}' -Raw).Trim(); $s=Read-Journal $h $r $pin ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value); {mutations[kind]}; Write-Journal $s"
                    modified=subprocess.run([PS,'-NoProfile','-Command',cmd],capture_output=True)
                    self.assertEqual(modified.returncode,0,modified.stderr)
                junction=None
                if kind=='junction':
                    junction=root/'hermes/bin'; parked=root/'parked'; junction.rename(parked)
                    cmd=f"New-Item -ItemType Junction -Path '{junction}' -Target '{parked}' | Out-Null"
                    made=subprocess.run([PS,'-NoProfile','-Command',cmd],capture_output=True)
                    self.assertEqual(made.returncode,0,made.stderr)
                before=(root/'calls.txt').read_bytes()
                try:
                    self.assertEqual(self.run_worker(root, 'OK')['code'], 'CONFIG')
                    self.assertEqual((root/'calls.txt').read_bytes(),before)
                    for k,rel in [('config','config.yaml'),('env','.env'),('head','hermes-agent/.git/HEAD'),('exe','bin/hermes.exe')]:
                        if kind==k: self.assertEqual((root/'hermes'/rel).read_text(),'EXTERNAL KEEP')
                finally:
                    if junction is not None: os.rmdir(junction)

    def test_configure_rejects_forged_fresh_before_network(self):
        import sys
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT/'backend'))
        import configure
        from provider import Failure
        with tempfile.TemporaryDirectory(prefix='subscriber-config-') as tmp:
            home=Path(tmp)/'hermes'; repo=home/'hermes-agent'; repo.mkdir(parents=True)
            template=(ROOT/'backend/upstream/cli-config.yaml.example').read_bytes()
            (repo/'cli-config.yaml.example').write_bytes(template)
            (home/'config.yaml').write_bytes(template)
            with patch.object(configure, 'select_model', side_effect=AssertionError('NETWORK REACHED')):
                with self.assertRaises(Failure) as caught:
                    configure.main(home, repo, dict(endpoint='https://example.invalid/v1', api_key='fake-key', fresh=True))
                self.assertEqual(caught.exception.code, 'CONFIG')

    def test_owned_api_retry_without_reinstall_and_foreign_refusal(self):
        for failure in ('AUTH', 'QUOTA', 'NETWORK', 'TIMEOUT'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='subscriber-retry-') as tmp:
                root=Path(tmp)
                self.assertEqual(self.run_worker(root, failure)['code'], 'NETWORK' if failure=='TIMEOUT' else failure)
                result=self.run_worker(root, 'OK', corrected=True)
                self.assertEqual(result['type'], 'success', result)
                self.assertEqual((root/'calls.txt').read_text().splitlines(),
                                 ['install', 'configure:initial', 'configure:corrected'])
                self.assertEqual(self.run_worker(root, 'OK')['code'], 'CONFIG')
        with tempfile.TemporaryDirectory(prefix='subscriber-foreign-') as tmp:
            root=Path(tmp); (root/'hermes').mkdir(); (root/'hermes/user.txt').write_text('KEEP')
            self.assertEqual(self.run_worker(root, 'OK')['code'], 'CONFIG')
            self.assertFalse((root/'calls.txt').exists())
            self.assertEqual((root/'hermes/user.txt').read_text(), 'KEEP')

if __name__ == '__main__': unittest.main(verbosity=2)
