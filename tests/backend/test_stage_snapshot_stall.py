"""Regression: stage tree hashing must not stall the pipe reader or the heartbeat.

A full-tree digest of a real install tree is tens of thousands of files. Hashing it
synchronously inside Read-ChildPipes blocks stdout/stderr reads and suppresses the
elapsed heartbeat for minutes at a time, which can make the install watchdog misfire
as NETWORK. The hash now runs in a background job; this test proves the heartbeat
keeps firing while the digest is computed and that the completed digest is adopted.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PS = os.path.join(os.environ['SYSTEMROOT'], 'System32/WindowsPowerShell/v1.0/powershell.exe')
PLACEHOLDER = 'A' * 64


class StageSnapshotStallTests(unittest.TestCase):
    def _tree(self, root, n):
        home = root / 'hermes'
        d = home / 'deep' / 'nested' / 'tree'
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f'f{i}.txt').write_text('x' * 32)
        return home

    def _run(self, root, home, seconds):
        child = root / 'child.ps1'
        # One stage frame, then silence: the only thing that can reach the UI during
        # the hash is the 5s heartbeat.
        child.write_text(
            "[Console]::WriteLine('{\"stage\":\"venv\",\"ok\":true,\"skipped\":false}')\n"
            f"Start-Sleep -Seconds {seconds}\n", encoding='utf-8-sig')
        harness = root / 'harness.ps1'
        harness.write_text(
            ". '" + str(ROOT / 'backend' / 'worker.ps1') + "'\n"
            "$tree='" + str(home) + "'\n"
            "$pin='" + '0' * 40 + "'\n"
            "$state=[pscustomobject]@{schema=1;owner='HermesSubscriberSetup';sid='S-1-0-0';home=$tree;repo=$tree;revision=$pin;phase='installing';fingerprints=@('" + PLACEHOLDER + "','node','1','1')}\n"
            "$script:InstallState=$state\n"
            "$script:StageKnownHash='" + PLACEHOLDER + "'\n"
            "$script:InstallPid=1; $script:InstallStarted=1\n"
            "Write-Journal $state -New\n"
            "$r = Run-Child '" + PS + "' @('-NoProfile','-NonInteractive','-File','" + str(child) + "') '' 40 -StreamStages\n"
            "Start-Sleep -Seconds 2\n"
            "$s = Read-Journal $tree $tree $pin 'S-1-0-0'\n"
            "$expect = Install-TreeHash $tree\n"
            "[Console]::WriteLine('{\"type\":\"test-end\",\"hash\":\"' + $s.fingerprints[0] + '\",\"stage\":\"' + $s.fingerprints[1] + '\",\"expect\":\"' + $expect + '\"}')\n",
            encoding='utf-8-sig')
        p = subprocess.Popen([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        return p

    def test_heartbeat_fires_while_tree_hash_runs(self):
        with tempfile.TemporaryDirectory(prefix='subscriber-stall-') as tmp:
            root = Path(tmp)
            home = self._tree(root, 3000)  # ~12 s of hashing at ~4 ms/file
            p = self._run(root, home, 25)
            beats = []
            stage_at = None
            started = time.monotonic()
            last_beat = None
            try:
                for line in p.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev.get('stage') == 'venv' and stage_at is None:
                        stage_at = time.monotonic() - started
                    elif ev.get('type') == 'progress' and 'Прошло' in ev.get('message', ''):
                        beats.append(time.monotonic() - started)
                        last_beat = ev['message']
                    elif ev.get('type') == 'test-end':
                        final = ev
                        break
            finally:
                if p.poll() is None:
                    subprocess.run(['taskkill.exe', '/PID', str(p.pid), '/T', '/F'], capture_output=True)
                rest_out, err = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 0, err)

            self.assertLess(stage_at, 6, 'stage frame must not wait for the tree hash')
            # The digest takes ~12 s; at least two heartbeats must arrive while it runs.
            after = [b for b in beats if stage_at is not None and b > stage_at + 1]
            self.assertGreaterEqual(len(after), 2, f'heartbeat suppressed during hash: {beats}')
            # The adopted digest must describe the real tree, not a placeholder.
            self.assertEqual(final['hash'], final['expect'], 'journal digest != actual tree digest')
            self.assertNotEqual(final['hash'], PLACEHOLDER, 'background digest never adopted')
            self.assertEqual(final['stage'], 'venv')

    def test_small_tree_adopts_synchronously(self):
        with tempfile.TemporaryDirectory(prefix='subscriber-stall-') as tmp:
            root = Path(tmp)
            home = self._tree(root, 5)
            p = self._run(root, home, 4)
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            events = [json.loads(x) for x in out.splitlines() if x.strip().startswith('{')]
            final = next(e for e in events if e.get('type') == 'test-end')
            self.assertEqual(final['hash'], final['expect'], 'journal digest != actual tree digest')
            self.assertNotEqual(final['hash'], PLACEHOLDER, 'first-stage digest missing')
            self.assertEqual(final['stage'], 'venv')


if __name__ == '__main__':
    unittest.main(verbosity=2)
