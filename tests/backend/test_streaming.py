"""OFFLINE: real child-pipe timing; child is a simulation, never installer."""
import json, os, subprocess, tempfile, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PS = os.path.join(os.environ['SYSTEMROOT'], 'System32/WindowsPowerShell/v1.0/powershell.exe')

class StreamingTests(unittest.TestCase):
    def test_stage_arrives_before_child_exit_and_stderr_does_not_block(self):
        with tempfile.TemporaryDirectory(prefix='subscriber-stream-') as d:
            child = Path(d)/'child.ps1'
            child.write_text("[Console]::WriteLine('{\"stage\":\"repository\",\"ok\":true,\"skipped\":false,\"duration_ms\":42}')\n[Console]::Error.Write(('secret-test-key' * 100000))\nStart-Sleep -Seconds 7\n", encoding='utf-8-sig')
            harness = Path(d)/'harness.ps1'
            harness.write_text(". '"+str(ROOT/'backend/worker.ps1')+"'\n$r = Run-Child '"+PS+"' @('-NoProfile','-NonInteractive','-File','"+str(child)+"') '' 20 -StreamStages\n[Console]::WriteLine('{\"type\":\"test-end\"}')", encoding='utf-8-sig')
            start=time.monotonic()
            p=subprocess.Popen([PS,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(harness)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
            first=p.stdout.readline()
            delay=time.monotonic()-start
            rest,err=p.communicate(timeout=25)
            self.assertEqual(p.returncode,0,err)
            event=json.loads(first)
            self.assertEqual(event.get('type'),'progress','stage was not streamed')
            self.assertLess(delay,5,'stage buffered until child exit')
            self.assertEqual((event['stage'],event['index'],event['count']),('repository',5,16))
            events=[json.loads(x) for x in rest.splitlines()]
            self.assertTrue(any('Прошло' in e.get('message','') for e in events),'silent child needs honest elapsed heartbeat')
            self.assertNotIn('secret-test-key',first+rest+err)
            self.assertNotIn('%',first+rest)

if __name__=='__main__': unittest.main(verbosity=2)
