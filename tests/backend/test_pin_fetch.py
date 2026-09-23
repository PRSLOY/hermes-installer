"""Execute the actual vendored post-clone pin block with an offline git double."""
import os, subprocess, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
PS=str(Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
class PinFetchTests(unittest.TestCase):
 def run_block(self, failure):
  src=(ROOT/'backend/upstream/install.ps1').read_text(encoding='utf-8-sig')
  start=src.index('                Write-Info "Pinning to commit $Commit..."',src.index('# Post-clone pin:'))
  end=src.index('            } elseif ($Tag)',start)
  block=src[start:end]
  script='''$ErrorActionPreference='Stop'
$Commit='c712f06dcdd24053a4118f38d2090ac53137ecfc'
$script:checkout=$false
function Write-Info($x) {}
function Write-Warn($x) { [Console]::WriteLine('WARN '+$x) }
function git {
 $a=@($args)
 if($a -contains 'fetch') {
  if(-not ($a -contains '--depth=1')){throw 'UNBOUNDED_FETCH'}
  $global:LASTEXITCODE=FETCH_CODE
 } elseif($a -contains 'checkout') {$script:checkout=$true;$global:LASTEXITCODE=0}
}
try {
BLOCK
 [Console]::WriteLine('OK checkout='+$script:checkout)
} catch { [Console]::WriteLine('FAIL checkout='+$script:checkout+' '+$_.Exception.Message) }
'''.replace('FETCH_CODE','23' if failure else '0').replace('BLOCK',block)
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'test.ps1';path.write_text(script,encoding='utf-8-sig')
   r=subprocess.run([PS,'-NoProfile','-NonInteractive','-File',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=60)
   self.assertEqual(r.returncode,0,r.stderr);return r.stdout
 def test_bounded_fetch_then_checkout(self):
  self.assertIn('OK checkout=True',self.run_block(False))
 def test_fetch_failure_stops_before_checkout(self):
  """A persistent fetch failure is retried, then still stops before checkout."""
  result=self.run_block(True)
  self.assertIn('FAIL checkout=False',result)
  self.assertIn('23',result)
  self.assertIn('retrying (1/3)',result)
 def test_no_unbounded_pin_fetch_remains(self):
  """Every fetch of $Commit must be depth-limited: the managed clone is shallow."""
  source=(ROOT/'backend/upstream/install.ps1').read_text(encoding='utf-8-sig')
  offenders=[]
  for number,line in enumerate(source.splitlines(),1):
   if line.strip().startswith('git') and 'fetch' in line and '$Commit' in line and '--depth=1' not in line:
    offenders.append(f'{number}: {line.strip()}')
  self.assertEqual(offenders,[],'unbounded fetch of $Commit: '+str(offenders))
if __name__=='__main__':unittest.main(verbosity=2)
