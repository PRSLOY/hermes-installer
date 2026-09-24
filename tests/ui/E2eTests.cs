using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using HermesSetup;

// SIMULATION ONLY: protocol round-trip against a mock backend (PowerShell stub worker).
// This proves UI<->backend NDJSON wiring, NOT a real installation.
public static class E2eTests
{
    static void Check(bool condition, string name)
    {
        Console.WriteLine((condition ? "PASS: " : "FAIL: ") + name);
        if (!condition) Environment.ExitCode = 1;
    }
    static string WriteWorker(string directory, string body)
    {
        Directory.CreateDirectory(directory);
        string worker = Path.Combine(directory, "worker.ps1");
        File.WriteAllText(worker, body, new System.Text.UTF8Encoding(false));
        return worker;
    }
    public static int Run()
    {
        string temp = Path.Combine(Path.GetTempPath(), "hermes-setup-ui-tests", Guid.NewGuid().ToString("N"));
        var progress = new List<string>();
        try
        {
            // 1. Happy path: two progress records then a valid success record, exit 0.
            string okWorker = WriteWorker(Path.Combine(temp, "ok", "backend"), "$ErrorActionPreference='Stop'\r\n" +
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\r\n[Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8\r\n" +
                "[void]([Console]::In.ReadToEnd())\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"progress\",\"message\":\"step one\"}')\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"progress\",\"message\":\"step two\"}')\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"success\",\"message\":\"done\",\"launch_path\":\"C:\\\\mock\\\\hermes.exe\",\"launch_args\":[]}')\r\nexit 0\r\n");
            var outcome = WorkerClient.RunAsync(okWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" }, progress.Add,
                delegate(string p, string[] a) { return true; /* SIMULATION: permissive validator; production uses LaunchPolicy */ }).GetAwaiter().GetResult();
            Check(outcome.Success && outcome.Message=="done" && outcome.LaunchPath=="C:\\mock\\hermes.exe" &&
                  progress.Count==2 && progress[0]=="step one" && progress[1]=="step two" &&
                  outcome.LaunchArgs.Length==0, "mock success round-trip with ordered progress");

            // 2. Terminal error record with a misleading exit code 0. Must still fail.
            string errWorker = WriteWorker(Path.Combine(temp, "err", "backend"), "$ErrorActionPreference='Stop'\r\n" +
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\r\n[Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8\r\n" +
                "[void]([Console]::In.ReadToEnd())\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"error\",\"code\":\"AUTH\",\"message\":\"bad key\"}')\r\nexit 0\r\n");
            var errOutcome = WorkerClient.RunAsync(errWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" }, delegate { }).GetAwaiter().GetResult();
            Check(!errOutcome.Success && errOutcome.Message.Contains("AUTH"), "mock AUTH error rejects despite exit 0");

            // 3. Success record plus stderr diagnostics: session must be invalidated.
            string stderrWorker = WriteWorker(Path.Combine(temp, "stderr", "backend"), "$ErrorActionPreference='Stop'\r\n" +
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\r\n[Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8\r\n" +
                "[void]([Console]::In.ReadToEnd())\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"success\",\"message\":\"done\",\"launch_path\":\"C:\\\\mock\\\\hermes.exe\",\"launch_args\":[]}')\r\n" +
                "[Console]::Error.WriteLine('diag')\r\nexit 0\r\n");
            var stderrOutcome = WorkerClient.RunAsync(stderrWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" }, delegate { }).GetAwaiter().GetResult();
            Check(!stderrOutcome.Success, "stderr diagnostic invalidates a success record (mock)");

            // 4. Success record without trailing newline: violates NDJSON framing, must fail.
            string nonlWorker = WriteWorker(Path.Combine(temp, "nonl", "backend"), "$ErrorActionPreference='Stop'\r\n" +
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\r\n[Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8\r\n" +
                "[void]([Console]::In.ReadToEnd())\r\n" +
                "[Console]::Out.Write('{\"type\":\"success\",\"message\":\"done\",\"launch_path\":\"C:\\\\mock\\\\hermes.exe\",\"launch_args\":[]}')\r\nexit 0\r\n");
            var nonlOutcome = WorkerClient.RunAsync(nonlWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" }, delegate { }).GetAwaiter().GetResult();
            Check(!nonlOutcome.Success, "unterminated final record rejected (NDJSON framing enforced)");

            // 5. Stdin contract: protocol/action/endpoint/api_key/model arrive intact.
            // The mock header mirrors backend\worker.ps1 byte-for-byte and the parse
            // is guarded: a parse failure must surface as a failed field check
            // ("fields-bad"), never as a stderr record that invalidates the whole
            // session as VERIFY. The five-field comparison itself is unchanged.
            string echoWorker = WriteWorker(Path.Combine(temp, "echo", "backend"), "$ErrorActionPreference='Stop'\r\n" +
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\r\n[Console]::InputEncoding=$utf8\r\n[Console]::OutputEncoding=$utf8\r\n$OutputEncoding=$utf8\r\n" +
                "$raw = [Console]::In.ReadToEnd().TrimStart([char]0xFEFF)\r\n" +
                "$parsed = $null; try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null }\r\n" +
                "$ok = ($null -ne $parsed -and $parsed.protocol -eq 1 -and $parsed.action -eq 'install' -and $parsed.endpoint -eq 'https://mock.test/v1' -and $parsed.api_key -eq 'MOCKKEY12345' -and $parsed.model -eq 'mock-model')\r\n" +
                // Diagnostic only: name the mismatching field(s). The pass condition ($ok) is unchanged.
                "$bad = @(); if ($null -eq $parsed) { $bad += ('parse:first=' + [int][char]$raw[0] + ',len=' + $raw.Length) } else { " +
                "if (-not ($parsed.protocol -eq 1)) { $bad += ('protocol=' + $parsed.protocol) }; " +
                "if (-not ($parsed.action -eq 'install')) { $bad += ('action=' + $parsed.action) }; " +
                "if (-not ($parsed.endpoint -eq 'https://mock.test/v1')) { $bad += ('endpoint=' + $parsed.endpoint) }; " +
                "if (-not ($parsed.api_key -eq 'MOCKKEY12345')) { $bad += 'api_key' }; " +
                "if (-not ($parsed.model -eq 'mock-model')) { $bad += ('model=' + $parsed.model) } }\r\n" +
                "$msg = ('fields-bad:' + ($bad -join '+')) -replace '[^A-Za-z0-9:=+_./ -]','?'; if ($ok) { $msg = 'fields-ok' }\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"success\",\"message\":\"' + $msg + '\",\"launch_path\":\"C:\\\\mock\\\\hermes.exe\",\"launch_args\":[]}')\r\nexit 0\r\n");
            var echoOutcome = WorkerClient.RunAsync(echoWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345", model="mock-model" }, delegate { },
                delegate(string p, string[] a) { return true; /* SIMULATION: permissive validator */ }).GetAwaiter().GetResult();
            Check(echoOutcome.Success && echoOutcome.Message=="fields-ok", "request JSON fields arrive intact at backend (stdin protocol) got="+echoOutcome.Message);

            // 5b. Backup providers (issue #10) arrive as a JSON array of objects on stdin.
            string fbWorker = WriteWorker(Path.Combine(temp, "fallbacks", "backend"), "$ErrorActionPreference='Stop'\r\n" +
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\r\n[Console]::InputEncoding=$utf8\r\n[Console]::OutputEncoding=$utf8\r\n$OutputEncoding=$utf8\r\n" +
                "$raw = [Console]::In.ReadToEnd().TrimStart([char]0xFEFF)\r\n" +
                "$p = $null; try { $p = $raw | ConvertFrom-Json } catch { $p = $null }\r\n" +
                // Plain assignment: `$f = if (...) { @(...) }` unrolls a one-element array into a
                // bare PSCustomObject, which has no .Count in PowerShell 5.1.
                "$f = @(); if ($null -ne $p) { $f = @($p.fallbacks) }\r\n" +
                "$ok = ($f.Count -eq 1 -and $f[0].provider_id -eq 'dahl' -and $f[0].endpoint -eq 'https://dahl.mock.test/v1' -and $f[0].model -eq 'm-1' -and $f[0].api_key -eq 'MOCKBACKUP123')\r\n" +
                "$msg = if ($ok) { 'fallbacks-ok' } else { 'fallbacks-bad' }\r\n" +
                "[Console]::Out.WriteLine('{\"type\":\"success\",\"message\":\"' + $msg + '\",\"launch_path\":\"C:\\\\mock\\\\hermes.exe\",\"launch_args\":[]}')\r\nexit 0\r\n");
            var fbRequest = new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345",
                fallbacks = new List<FallbackEntry> { new FallbackEntry { provider_id="dahl", endpoint="https://dahl.mock.test/v1", model="m-1", api_key="MOCKBACKUP123" } } };
            var fbOutcome = WorkerClient.RunAsync(fbWorker, fbRequest, delegate { },
                delegate(string p, string[] a) { return true; /* SIMULATION: permissive validator */ }).GetAwaiter().GetResult();
            Check(fbOutcome.Success && fbOutcome.Message=="fallbacks-ok", "backup providers arrive intact at backend (stdin protocol) got="+fbOutcome.Message);

            // 6. Missing worker script: actionable INSTALL failure, not a crash.
            var missing = WorkerClient.RunAsync(Path.Combine(temp, "absent", "backend", "worker.ps1"), new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" }, delegate { }).GetAwaiter().GetResult();
            Check(!missing.Success && missing.Message.Contains("backend"), "missing worker script produces actionable error");

            // 7. «Отменить» on a worker that never exits: the whole tree dies, including a
            // grandchild whose parent already exited (the orphan `taskkill /T` cannot reach).
            string cancelPids = Path.Combine(temp, "cancel.pids"), cancelOrphan = Path.Combine(temp, "cancel.orphan");
            string cancelWorker = HangWorker(Path.Combine(temp, "cancel", "backend"), cancelPids, cancelOrphan);
            var cancel = new CancellationTokenSource();
            var stopped = WorkerClient.RunAsync(cancelWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" },
                delegate(string m) { if (m == "hanging") cancel.Cancel(); }, delegate(string p, string[] a) { return true; }, null,
                cancel.Token, TimeSpan.FromMinutes(90)).GetAwaiter().GetResult();
            Check(stopped.Cancelled && !stopped.Success && stopped.Message.Contains("остановлена"), "cancel stops a worker that never exits; the outcome says «остановлена» got="+stopped.Code);
            Check(AllGone(cancelPids, cancelOrphan), "cancel leaves no process behind: worker, child and orphaned grandchild are gone (by PID)");

            // 8. Hung worker: one record, then silence past the (injected) limit = tree killed, error shown.
            string hungPids = Path.Combine(temp, "hung.pids"), hungOrphan = Path.Combine(temp, "hung.orphan");
            string hungWorker = HangWorker(Path.Combine(temp, "hung", "backend"), hungPids, hungOrphan);
            var hung = WorkerClient.RunAsync(hungWorker, new Request { endpoint="https://mock.test/v1", api_key="MOCKKEY12345" }, delegate { },
                delegate(string p, string[] a) { return true; }, null, CancellationToken.None, TimeSpan.FromSeconds(15)).GetAwaiter().GetResult();
            Check(!hung.Success && hung.Code == Outcome.CodeTimeout && hung.Message == WorkerClient.HungMessage, "silence past the limit stops the install with the hung-worker error got="+hung.Code);
            Check(AllGone(hungPids, hungOrphan), "hung-worker stop leaves no process behind (by PID)");

            // 9. Telegram actions are bounded too.
            string tgPids = Path.Combine(temp, "tg.pids"), tgOrphan = Path.Combine(temp, "tg.orphan");
            string tgWorker = HangWorker(Path.Combine(temp, "tg", "backend"), tgPids, tgOrphan);
            var tg = WorkerClient.RunTelegramAsync(tgWorker, "telegram_pending", null, null, TimeSpan.FromSeconds(15)).GetAwaiter().GetResult();
            Check(!tg.Success && tg.Code == Outcome.CodeTimeout, "a hung Telegram action ends with a timeout instead of waiting forever got="+tg.Code);
            Check(AllGone(tgPids, tgOrphan), "hung Telegram action leaves no process behind (by PID)");
        }
        finally
        {
            // Never leave test pings running, even when a check above failed.
            foreach (string file in Directory.Exists(temp) ? Directory.GetFiles(temp) : new string[0])
                foreach (int pid in Pids(file)) try { using (var p = Process.GetProcessById(pid)) p.Kill(); } catch {}
            try { Directory.Delete(temp, true); } catch {}
        }
        return Environment.ExitCode;
    }

    // Mock worker that never exits: reads the request, starts ping as a direct child and, via
    // an intermediate PowerShell that exits at once, a second ping whose parent is dead. It
    // records all PIDs, then prints one progress record ("hanging") and sleeps forever.
    static string HangWorker(string directory, string pidFile, string orphanFile)
    {
        string grand = "$si = New-Object Diagnostics.ProcessStartInfo 'ping.exe', '-n 600 127.0.0.1'; $si.UseShellExecute = $false; $si.CreateNoWindow = $true; " +
            "$p = [Diagnostics.Process]::Start($si); [IO.File]::WriteAllText('" + orphanFile + "', [string]$p.Id)";
        string encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(grand));
        return WriteWorker(directory, "$ErrorActionPreference='Stop'\r\n" +
            "[void]([Console]::In.ReadToEnd())\r\n" +
            "$si = New-Object Diagnostics.ProcessStartInfo 'ping.exe', '-n 600 127.0.0.1'\r\n$si.UseShellExecute = $false; $si.CreateNoWindow = $true\r\n" +
            "$kid = [Diagnostics.Process]::Start($si)\r\n" +
            "$gi = New-Object Diagnostics.ProcessStartInfo (Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'), '-NoProfile -NonInteractive -EncodedCommand " + encoded + "'\r\n" +
            "$gi.UseShellExecute = $false; $gi.CreateNoWindow = $true\r\n$mid = [Diagnostics.Process]::Start($gi); $mid.WaitForExit()\r\n" +
            "[IO.File]::WriteAllText('" + pidFile + "', ([string]$PID + ' ' + $kid.Id))\r\n" +
            "[Console]::Out.WriteLine('{\"type\":\"progress\",\"message\":\"hanging\"}')\r\n" +
            "while ($true) { Start-Sleep -Milliseconds 200 }\r\n");
    }
    static List<int> Pids(string file)
    {
        var list = new List<int>();
        try { foreach (string part in File.ReadAllText(file).Split(' ')) { int pid; if (int.TryParse(part.Trim(), out pid)) list.Add(pid); } } catch {}
        return list;
    }
    static bool Alive(int pid)
    {
        try { using (var p = Process.GetProcessById(pid)) return !p.HasExited; }
        catch { return false; }
    }
    // Both PID files must exist (the tree really started) and every PID must be gone within 10 s.
    static bool AllGone(string pidFile, string orphanFile)
    {
        var pids = Pids(pidFile); pids.AddRange(Pids(orphanFile));
        if (pids.Count != 3) return false;
        for (int i = 0; i < 40; i++)
        {
            bool any = false;
            foreach (int pid in pids) if (Alive(pid)) any = true;
            if (!any) return true;
            Thread.Sleep(250);
        }
        return false;
    }
}
