using System;
using System.IO;
using HermesSetup;

// Unit tests for the NDJSON protocol state machine and launch policy (offline, no backend).
// Compiled WITHOUT ui/InstallerForm.cs so the harness has its own console entry point and
// never starts the GUI message loop.
public static class UnitTests
{
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "e2e") return E2eTests.Run();
        return Run();
    }
    static void Check(bool condition, string name)
    {
        Console.WriteLine((condition ? "PASS: " : "FAIL: ") + name);
        if (!condition) Environment.ExitCode = 1;
    }
    static Protocol New(Action<string> sink) { return new Protocol("TESTSECRET123", s => sink(s), delegate { return true; }); }
    public static string SuccessLine(string path)
    {
        return "{\"type\":\"success\",\"message\":\"Готово\",\"launch_path\":\"" + path + "\",\"launch_args\":[]}";
    }
    public static int Run()
    {
        string stageText = null;
        var staged = New(s => stageText=s);
        staged.Feed("{\"type\":\"progress\",\"stage\":\"repository\",\"index\":5,\"count\":16,\"message\":\"Репозиторий завершён\"}");
        staged.Feed(SuccessLine("C:\\x\\hermes.exe"));
        Check(stageText == "Этап 5/16: Репозиторий завершён", "v2 stage index/count renders Russian numbered progress");
        var normalized = new Request { endpoint="  gateway.example.test/custom/api///  ", api_key="TESTKEY123" };
        Check(normalized.Validate()==null && normalized.endpoint=="https://gateway.example.test/custom/api", "normalization adds https trims whitespace/slashes and preserves explicit API path");
        Check(new Request { endpoint="example.test", api_key="TESTKEY123" }.Validate()!=null, "website requires explicit API address without guessed v1");
        string catalogPath=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"providers.json");
        File.WriteAllText(catalogPath,"{\"providers\":["+
            "{\"id\":\"gwarden\",\"label\":\"GWarden\",\"endpoint\":\"https://gwarden.su/v1\",\"model\":\"glm-5.3\",\"note\":\"Ключ — GWAR-XXXX\",\"key_url\":\"https://t.me/GwardenAIBot\"},"+
            "{\"id\":\"openrouter\",\"label\":\"OpenRouter\",\"endpoint\":\"https://openrouter.ai/api/v1\",\"note\":\"Ключ — openrouter.ai/keys\",\"key_url\":\"https://openrouter.ai/keys\"},"+
            "{\"id\":\"custom\",\"label\":\"Другой (точный адрес API)\"}]}");
        try { using(var form=new InstallerForm()) {
            Check(form.ProviderCount==3,"provider cards read external owner-maintained JSON");
            Check(form.SelectedProvider==0 && !form.IsCustomSelected,"first (recommended) provider is preselected");
            Check(!form.ManualEndpointShown,"preset hides the manual API address field");
            bool inManual=false;
            for(System.Windows.Forms.Control c=form.Endpoint;c!=null;c=c.Parent) if(c.Name=="CustomPanel") inManual=true;
            Check(inManual,"manual address, preview and confirmation live in the Другой panel");
            Check(form.BuildRequest().endpoint=="https://gwarden.su/v1","preset endpoint is used without being displayed");
            Check(form.BuildRequest().model=="glm-5.3","preset model is sent to the backend request");
            form.ToggleModelField();
            form.Model.Text="owner-model";
            Check(form.BuildRequest().model=="owner-model","revealing the model field overrides the preset model");
            form.ToggleModelField();
            Check(form.PresetAt(0).KeyUrl=="https://t.me/GwardenAIBot","https key_url is parsed for the get-key button");
            Check(KeyLinkPolicy.Clean("http://x.example")=="" && KeyLinkPolicy.Clean("https://ok.example/keys")=="https://ok.example/keys","key link policy opens only plain https URLs");
            form.SelectProvider(2);
            Check(form.IsCustomSelected && form.ManualEndpointShown,"choosing Другой reveals the manual API address field");
            Check(form.ConfirmEndpoint.Enabled==false,"custom endpoint starts unconfirmed and unvalidated");
            form.Endpoint.Text="  other.test/custom/api///  ";
            Check(!form.ConfirmEndpoint.Checked && !form.CanChoose,"unconfirmed custom URL cannot proceed");
            Check(form.EndpointPreview.Text.Contains("https://other.test/custom/api"),"final normalized URL preview is visible before network");
            form.ConfirmEndpoint.Checked=true;
            Check(form.CanChoose,"confirming displayed URL permits proceeding");
            form.Endpoint.Text="other.test/different/api";
            Check(!form.ConfirmEndpoint.Checked && !form.CanChoose,"URL changes reset prior confirmation");
            form.SelectProvider(1);
            Check(!form.ManualEndpointShown && form.BuildRequest().endpoint=="https://openrouter.ai/api/v1","returning to a preset hides the address and cannot retain a custom host");
        }} finally { File.Delete(catalogPath); }
        File.WriteAllText(catalogPath,"{\"_comment\":\"owner notes\",\"providers\":[{\"id\":\"openrouter\",\"label\":\"OpenRouter\",\"endpoint\":\"https://openrouter.ai/api/v1\"},{\"id\":\"custom\",\"label\":\"Другой\"}]}");
        try {
            var wrapped=ProviderCatalog.Load(catalogPath);
            Check(wrapped.Length==2 && wrapped[0].endpoint=="https://openrouter.ai/api/v1","shipped providers.json wrapper format with owner comment loads");
        } finally { File.Delete(catalogPath); }
        File.WriteAllText(catalogPath,"this is not json at all");
        try { ProviderCatalog.Load(catalogPath); Check(false,"garbage providers.json rejected"); }
        catch { Check(true,"garbage providers.json rejected"); }
        finally { File.Delete(catalogPath); }
        Check(WorkerClient.StartInfo("C:/package/backend/worker.ps1").EnvironmentVariables.ContainsKey("OS"), "worker receives OS for platform preflight without inherited secrets");
        // The elevated VC++ redistributable fails with 0x80070003 without these.
        Environment.SetEnvironmentVariable("HERMES_TEST_SECRET_KEY", "must-not-leak");
        try {
            var env = WorkerClient.StartInfo("C:/package/backend/worker.ps1").EnvironmentVariables;
            Check(env.ContainsKey("SystemDrive") && env.ContainsKey("CommonProgramFiles"), "worker receives SystemDrive and CommonProgramFiles for the VC++ installer");
            Check(!env.ContainsKey("HERMES_TEST_SECRET_KEY"), "unrelated variables are still not forwarded to the worker");
        } finally { Environment.SetEnvironmentVariable("HERMES_TEST_SECRET_KEY", null); }
        var none = New(null);
        Check(!none.Finish(0).Success, "exit 0 without terminal success is rejected");
        Check(!none.Finish(1).Success, "nonzero exit without terminal success is rejected");

        var bad = New(null); bad.Feed("{oops");
        Check(!bad.Finish(0).Success, "malformed NDJSON line rejects overall success");

        var corruptAfter = New(null); corruptAfter.Feed(SuccessLine("C:\\x\\hermes.exe")); corruptAfter.Feed("{\"type\":");
        Check(!corruptAfter.Finish(0).Success, "corruption after a success record still rejects");

        var trailing = New(null); trailing.Feed(SuccessLine("C:\\x\\hermes.exe")); trailing.Feed("  ");
        Check(!trailing.Finish(0).Success, "blank/garbage trailing line rejects");

        var err = New(null); err.Feed("{\"type\":\"error\",\"code\":\"INSTALL\",\"message\":\"сбой\"}");
        var errOutcome = err.Finish(0);
        Check(!errOutcome.Success && errOutcome.Message.Contains("INSTALL"), "terminal error wins even with exit 0");

        var quota = New(null); quota.Feed("{\"type\":\"error\",\"code\":\"QUOTA\",\"message\":\"лимит\"}");
        Check(quota.Finish(1).Message.Contains("QUOTA"), "QUOTA error surfaces with advice");

        var ok = New(null); ok.Feed("{\"type\":\"progress\",\"message\":\"шаг\"}");
        Check(!ok.Finish(0).Success, "progress-only stream without final record is rejected");

        var noterm = New(null); noterm.Feed(SuccessLine("C:\\x\\hermes.exe"));
        Check(!noterm.Finish(1).Success, "success record with nonzero exit is rejected");

        var extra = New(null); extra.Feed("{\"type\":\"success\",\"message\":\"Готово\",\"launch_path\":\"C:\\\\x\\\\hermes.exe\",\"launch_args\":[],\"extra\":1}");
        Check(!extra.Finish(0).Success, "success record with unexpected extra field is rejected");

        var unknown = New(null); unknown.Feed("{\"type\":\"error\",\"code\":\"HACK\",\"message\":\"x\"}");
        Check(!unknown.Finish(0).Success, "unknown error code is rejected as protocol violation");

        var dup = New(null); dup.Feed("{\"type\":\"progress\",\"message\":\"a\",\"message\":\"b\"}");
        Check(!dup.Finish(0).Success, "duplicate JSON keys are rejected");

        var truncated = New(null); truncated.Feed("{\"type\":\"success\",\"message\":\"Го");
        Check(!truncated.Finish(0).Success, "truncated JSON is rejected");

        string leaked = null;
        var redact = new Protocol("TESTSECRET123", delegate(string s) { leaked = s; }, delegate { return true; });
        redact.Feed("{\"type\":\"progress\",\"message\":\"ключ TESTSECRET123 получен\"}");
        Check(leaked != null && !leaked.Contains("TESTSECRET123"), "secret is redacted from progress text");
        var redact2 = new Protocol("TESTSECRET123", delegate { }, delegate { return true; });
        redact2.Feed("{\"type\":\"success\",\"message\":\"ключ TESTSECRET123 сохранён\",\"launch_path\":\"C:\\\\x\\\\hermes.exe\",\"launch_args\":[]}");
        Check(!redact2.Finish(0).Message.Contains("TESTSECRET123"), "secret is redacted from success message");

        var stderr = New(null); stderr.Invalidate();
        Check(!stderr.Finish(0).Success, "stderr output invalidates the whole session");

        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string packed = Path.Combine(local, "hermes", "hermes-agent", "apps", "desktop", "release",
            String.Equals(Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE"), "ARM64", StringComparison.OrdinalIgnoreCase) ? "win-arm64-unpacked" : "win-unpacked",
            "Hermes.exe");
        Check(!LaunchPolicy.Validate(null, new string[0]), "launch policy rejects null path");
        Check(!LaunchPolicy.Validate("C:\\Windows\\System32\\cmd.exe", new string[0]), "launch policy rejects foreign path");
        Check(!LaunchPolicy.Validate(packed, new[] { "desktop" }), "launch policy rejects arguments to the packed app");
        Check(!LaunchPolicy.Validate(Path.Combine(local, "hermes", "bin", "hermes.exe"), new string[0]), "launch policy rejects the rebuild-prone CLI launcher");

        Check(new Request { endpoint = "http://x", api_key = "TESTKEY123" }.Validate() != null, "request validation rejects http endpoint");
        Check(new Request { endpoint = "https://x/v1", api_key = "ключ с пробелом" }.Validate() != null, "request validation rejects space in key");
        Check(new Request { endpoint = "https://x/v1", api_key = "TESTKEY123" }.Validate() == null, "request validation accepts well-formed request");
        FallbackTests(catalogPath);
        FailureActionTests(catalogPath);
        MaintenanceProtocolTests();
        MaintenanceScreenTests(catalogPath);
        AfterConfiguredTests(catalogPath);
        return Environment.ExitCode;
    }

    // --- stop/timeout after the key was verified, launch errors, elevation, autostart ------
    static void AfterConfiguredTests(string catalogPath)
    {
        const string configured = "{\"type\":\"configured\",\"message\":\"Ключ проверен\",\"launch_path\":\"C:\\\\x\\\\Hermes.exe\"}";
        var p = New(delegate { }); p.Feed(configured);
        Check(p.ConfiguredLaunchPath == "C:\\x\\Hermes.exe", "configured record remembers the verified Desktop path");
        var stopped = WorkerClient.AfterConfigured(p, Outcome.Failure(Outcome.CodeCancelled, WorkerClient.StoppedMessage));
        Check(stopped.Success && stopped.Partial && stopped.LaunchPath == "C:\\x\\Hermes.exe" && stopped.Message.Contains("Необязательные шаги пропущены") && stopped.Message.Contains("остановлена"),
              "Cancel after the key was saved ends on Done (optional steps skipped), not on «Повторить»");
        var hung = WorkerClient.AfterConfigured(p, Outcome.Failure(Outcome.CodeTimeout, WorkerClient.HungMessage));
        Check(hung.Success && hung.Partial, "the hung-worker stop after the key was saved ends on Done too");
        var early = WorkerClient.AfterConfigured(New(null), Outcome.Failure(Outcome.CodeCancelled, WorkerClient.StoppedMessage));
        Check(!early.Success && early.Cancelled, "a stop before the key was saved stays a stop with «Повторить»");
        var bad = new Protocol(null, delegate { }, delegate { return false; }); bad.Feed(configured);
        Check(bad.ConfiguredLaunchPath == null && !bad.Finish(0).Success, "configured with a path LaunchPolicy rejects is a protocol violation");
        var twice = New(delegate { }); twice.Feed(configured); twice.Feed(configured);
        Check(!twice.Finish(0).Success, "a second configured record is rejected");
        var inMaint = MaintProtocol(); inMaint.Feed(configured);
        Check(!inMaint.Finish(0).Success, "configured is an install-only record");
        var ok = New(delegate { }); ok.Feed(configured); ok.Feed(SuccessLine("C:\\\\x\\\\Hermes.exe"));
        Check(ok.Finish(0).Success && !WorkerClient.AfterConfigured(ok, ok.Finish(0)).Partial, "a normal success is untouched");

        Check(ElevationGuard.Warn(true, "S-1-5-21-1-500", "S-1-5-21-1-1001"), "elevated as another account = warn");
        Check(!ElevationGuard.Warn(true, "S-1-5-21-1-1001", "S-1-5-21-1-1001"), "same-user elevation (and Windows Sandbox) = no warning");
        Check(!ElevationGuard.Warn(false, "S-1-5-21-1-500", "S-1-5-21-1-1001"), "not elevated = no warning");
        Check(!ElevationGuard.Warn(true, "S-1-5-21-1-500", null), "unknown shell owner = no warning (never blocks a normal run)");
        Check(ElevationGuard.Message.Contains("двойным щелчком"), "the warning tells how to start it");

        var env = WorkerClient.StartInfo("C:/package/backend/worker.ps1").EnvironmentVariables;
        Environment.SetEnvironmentVariable("HTTPS_PROXY", "http://127.0.0.1:3067");
        Environment.SetEnvironmentVariable("NODE_EXTRA_CA_CERTS", "C:\\ca.pem");
        try {
            env = WorkerClient.StartInfo("C:/package/backend/worker.ps1").EnvironmentVariables;
            Check(env["HTTPS_PROXY"] == "http://127.0.0.1:3067" && env["NODE_EXTRA_CA_CERTS"] == "C:\\ca.pem", "proxy and CA variables reach the worker");
        } finally { Environment.SetEnvironmentVariable("HTTPS_PROXY", null); Environment.SetEnvironmentVariable("NODE_EXTRA_CA_CERTS", null); }

        File.WriteAllText(catalogPath, "{\"providers\":[{\"id\":\"gwarden\",\"label\":\"GWarden\",\"endpoint\":\"https://gwarden.su/v1\",\"model\":\"glm-5.3\"},{\"id\":\"custom\",\"label\":\"Другой\"}]}");
        try { using (var form = new InstallerForm()) {
            typeof(InstallerForm).GetField("result", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(form, new Outcome { Success = true, LaunchPath = "C:\\nowhere\\Hermes.exe", LaunchArgs = new string[0] });
            form.GoTo(InstallerForm.PageDone);
            form.TryLaunch();
            Check(form.LaunchNote.Contains("карантин") && form.LaunchNote.Contains("C:\\nowhere\\Hermes.exe"), "a failed launch is explained on Done: antivirus quarantine and the exe path");
            Check(form.Launch.Enabled, "«Открыть Hermes» stays enabled after a failed launch");
            form.ShowTelegramStep("my_bot", true);
            var text = (System.Windows.Forms.Label)form.Controls.Find("TelegramStepText", true)[0];
            Check(text.Text.Contains("автозапуск бота после перезагрузки не настроен"), "a missing bot autostart is said on the Done screen");
            form.ShowTelegramStep("my_bot", false);
            Check(!text.Text.Contains("автозапуск"), "no autostart line when autostart is set");
            var interrupted = Status("ours_completed", true, true, "0.1.3"); interrupted.ChangeInterrupted = true;
            form.ApplyStatus(interrupted);
            Check(form.MaintenanceNote.Contains("смена провайдера была прервана") && form.PrimaryMaintenanceAction == "Сменить провайдера или ключ", "an interrupted provider change is offered again, with a note");
        }} finally { File.Delete(catalogPath); }
    }

    // --- maintenance: the "status" record, the "done" record, the requests -----------------
    public static string StatusLine(string state, string extra)
    {
        return "{\"type\":\"status\",\"message\":\"m\",\"state\":\"" + state + "\",\"launch_path\":\"\",\"set_supported\":true," +
            "\"set_version\":\"0.1.2\",\"package_set_version\":\"0.1.3\",\"model_ours\":true,\"base_url\":\"https://gwarden.su/v1\"," +
            "\"model\":\"glm-5.3\",\"telegram\":false,\"backups\":1,\"change_interrupted\":false" + extra + "}";
    }
    static Protocol StatusProtocol() { return new Protocol(null, delegate { }, delegate { return false; }) { StatusMode = true }; }
    static Protocol MaintProtocol() { return new Protocol(null, delegate { }, delegate { return false; }) { MaintenanceMode = true }; }
    static void MaintenanceProtocolTests()
    {
        var st = StatusProtocol(); st.Feed(StatusLine("ours_completed", ""));
        var s = st.Finish(0);
        Check(s.Success && s.Status != null && s.Status.State == "ours_completed" && s.Status.BaseUrl == "https://gwarden.su/v1" && s.Status.Model == "glm-5.3" &&
              s.Status.Backups == 1 && !s.Status.Telegram && s.Status.SetSupported && s.Status.ModelOurs && s.Status.ProviderHost == "gwarden.su", "status record parses every field");
        Check(s.Status.UpdateAvailable, "0.1.2 installed, 0.1.3 shipped = update available");
        foreach (string state in new[] { "none", "ours_incomplete", "foreign" })
        { var p = StatusProtocol(); p.Feed(StatusLine(state, "")); Check(p.Finish(0).Status.State == state, "status state " + state + " accepted"); }
        var install = New(null); install.Feed(StatusLine("ours_completed", ""));
        Check(!install.Finish(0).Success, "a status record is a protocol violation in an install session");
        var extra = StatusProtocol(); extra.Feed(StatusLine("ours_completed", ",\"api_key\":\"x\""));
        Check(!extra.Finish(0).Success, "a status record with an extra field (e.g. a key) is rejected");
        var bad = StatusProtocol(); bad.Feed(StatusLine("hacked", ""));
        Check(!bad.Finish(0).Success, "unknown state rejected");
        var http = StatusProtocol(); http.Feed(StatusLine("ours_completed", "").Replace("https://gwarden.su/v1", "http://gwarden.su/v1"));
        Check(!http.Finish(0).Success, "non-https base_url rejected");
        var cred = StatusProtocol(); cred.Feed(StatusLine("ours_completed", "").Replace("https://gwarden.su/v1", "https://u:p@gwarden.su/v1"));
        Check(!cred.Finish(0).Success, "base_url with credentials rejected");
        var ver = StatusProtocol(); ver.Feed(StatusLine("ours_completed", "").Replace("\"0.1.2\"", "\"1.2-beta\""));
        Check(!ver.Finish(0).Success, "malformed set version rejected");
        var launch = StatusProtocol(); launch.Feed(StatusLine("ours_completed", "").Replace("\"launch_path\":\"\"", "\"launch_path\":\"C:\\\\Windows\\\\System32\\\\cmd.exe\""));
        var launched = launch.Finish(0);
        Check(launched.Success && launched.Status.LaunchPath == "", "a launch path LaunchPolicy rejects becomes «no launch», never a launch");
        var twice = StatusProtocol(); twice.Feed(StatusLine("ours_completed", "")); twice.Feed(StatusLine("none", ""));
        Check(!twice.Finish(0).Success, "a second terminal record rejects");
        Check(new InstallStatus { SetVersion = "", PackageSetVersion = "0.1.3" }.UpdateAvailable, "no recorded set = update available");
        Check(!new InstallStatus { SetVersion = "0.1.3", PackageSetVersion = "0.1.3" }.UpdateAvailable, "same set = no update");
        Check(!new InstallStatus { SetVersion = "0.2", PackageSetVersion = "0.1.3" }.UpdateAvailable, "a newer installed set is not «updated» backwards");
        Check(!new InstallStatus { SetVersion = "", PackageSetVersion = "" }.UpdateAvailable, "unknown package set = no update offer");

        var done = MaintProtocol(); done.Feed("{\"type\":\"progress\",\"message\":\"шаг\"}"); done.Feed("{\"type\":\"done\",\"status\":\"ok\",\"message\":\"Набор обновлён.\"}");
        var d = done.Finish(0);
        Check(d.Success && d.MaintenanceStatus == "ok" && d.Message == "Набор обновлён." && d.TelegramBot == null, "done record ends a maintenance session");
        var partial = MaintProtocol(); partial.Feed("{\"type\":\"done\",\"status\":\"partial\",\"message\":\"не полностью\"}");
        Check(partial.Finish(0).MaintenanceStatus == "partial", "done partial parsed");
        var bot = MaintProtocol(); bot.Feed("{\"type\":\"done\",\"status\":\"ok\",\"message\":\"x\",\"telegram_bot\":\"my_bot\"}");
        Check(bot.Finish(0).TelegramBot == "my_bot", "done carries a connected bot name");
        var badBot = MaintProtocol(); badBot.Feed("{\"type\":\"done\",\"status\":\"ok\",\"message\":\"x\",\"telegram_bot\":\"a b\"}");
        Check(!badBot.Finish(0).Success, "malformed bot name rejected");
        var badStatus = MaintProtocol(); badStatus.Feed("{\"type\":\"done\",\"status\":\"great\",\"message\":\"x\"}");
        Check(!badStatus.Finish(0).Success, "unknown done status rejected");
        var inInstall = New(null); inInstall.Feed("{\"type\":\"done\",\"status\":\"ok\",\"message\":\"x\"}");
        Check(!inInstall.Finish(0).Success, "done is a protocol violation in an install session");
        var succ = MaintProtocol(); succ.Feed(SuccessLine("C:\\x\\hermes.exe"));
        Check(!succ.Finish(0).Success, "an install success is a protocol violation in a maintenance session");
        var err = MaintProtocol(); err.Feed("{\"type\":\"error\",\"code\":\"AUTH\",\"message\":\"ключ отклонён\"}");
        Check(err.Finish(1).Code == "AUTH", "maintenance errors keep their protocol code");

        var ser = new System.Web.Script.Serialization.JavaScriptSerializer();
        foreach (string a in new[] { MaintenanceRequest.UpdateSet, MaintenanceRequest.ForeignAddSet })
        {
            var r = new MaintenanceRequest { Action = a };
            Check(r.Validate() == null && ser.Serialize(r.Payload()) == "{\"protocol\":1,\"action\":\"" + a + "\"}", a + ": payload is protocol+action only");
        }
        var change = new MaintenanceRequest { Action = MaintenanceRequest.ChangeProvider, Provider = new Request { endpoint = " gwarden.su/v1 ", api_key = "NEW KEY 12345", model = "glm-5.3", telegram_bot_token = "1:x", fallbacks = new System.Collections.Generic.List<FallbackEntry> { new FallbackEntry() } } };
        Check(change.Validate() == null, "change_provider validates like an install (token/backups dropped)");
        var cp = change.Payload();
        Check(cp.Count == 5 && (string)cp["endpoint"] == "https://gwarden.su/v1" && (string)cp["api_key"] == "NEWKEY12345" && (string)cp["model"] == "glm-5.3" && (string)cp["action"] == "change_provider" && !cp.ContainsKey("telegram_bot_token") && !cp.ContainsKey("fallbacks"),
              "change_provider payload: normalized endpoint, cleaned key, model; nothing else");
        Check(Array.IndexOf(change.Secrets(), "NEWKEY12345") >= 0, "the new key is redacted from worker text");
        change.ClearSecrets();
        Check(change.Provider.api_key == null, "ClearSecrets drops the new key");
        Check(new MaintenanceRequest { Action = MaintenanceRequest.ChangeProvider, Provider = new Request { endpoint = "http://x/v1", api_key = "KEY12345678" } }.Validate() != null, "change_provider rejects http");
        var tg = new MaintenanceRequest { Action = MaintenanceRequest.TelegramConnect, TelegramToken = " 123456789:" + new string('A', 35) + " " };
        Check(tg.Validate() == null && ((string)tg.Payload()["telegram_bot_token"]).StartsWith("123456789:") && tg.Payload().Count == 3, "telegram_connect: cleaned token, protocol+action+token only");
        Check(new MaintenanceRequest { Action = MaintenanceRequest.TelegramConnect, TelegramToken = "" }.Validate() != null, "telegram_connect requires a token");
        Check(new MaintenanceRequest { Action = MaintenanceRequest.TelegramConnect, TelegramToken = "not-a-token" }.Validate() != null, "telegram_connect rejects a malformed token");
        var backups = new MaintenanceRequest { Action = MaintenanceRequest.AddBackups, PrimaryEndpoint = "https://gwarden.su/v1",
            Fallbacks = new System.Collections.Generic.List<FallbackEntry> { new FallbackEntry { provider_id = "dahl", endpoint = "https://dahl.test/v1", model = "m", api_key = "BACKUPKEY1" } } };
        Check(backups.Validate() == null && backups.Payload().Count == 3 && backups.Payload().ContainsKey("fallbacks"), "add_backups: protocol+action+fallbacks only");
        Check(new MaintenanceRequest { Action = MaintenanceRequest.AddBackups, PrimaryEndpoint = "https://dahl.test/v1", Fallbacks = backups.Fallbacks }.Validate() != null, "add_backups never adds the current primary as a backup");
        Check(new MaintenanceRequest { Action = MaintenanceRequest.AddBackups, Fallbacks = new System.Collections.Generic.List<FallbackEntry>() }.Validate() != null, "add_backups with no entries rejected");
        Check(new MaintenanceRequest { Action = "install" }.Validate() != null, "unknown maintenance action rejected");
    }

    static InstallStatus Status(string state, bool supported, bool modelOurs, string setVersion)
    {
        return new InstallStatus { State = state, Message = "Найден Hermes.", SetSupported = supported, ModelOurs = modelOurs, SetVersion = setVersion, PackageSetVersion = "0.1.3",
            BaseUrl = modelOurs ? "https://gwarden.su/v1" : "", Model = modelOurs ? "glm-5.3" : "", Telegram = false, Backups = 0, LaunchPath = "" };
    }
    static void MaintenanceScreenTests(string catalogPath)
    {
        File.WriteAllText(catalogPath, "{\"providers\":[" +
            "{\"id\":\"gwarden\",\"label\":\"GWarden\",\"endpoint\":\"https://gwarden.su/v1\",\"model\":\"glm-5.3\"}," +
            "{\"id\":\"dahl\",\"label\":\"Dahl — бесплатно\",\"endpoint\":\"https://inference.dahl.global/v1\",\"model\":\"MiniMaxAI/MiniMax-M2.7\"}," +
            "{\"id\":\"custom\",\"label\":\"Другой (точный адрес API)\"}]}");
        try { using (var form = new InstallerForm()) {
            form.ApplyStatus(Status("none", false, false, ""));
            Check(form.PageIndex == InstallerForm.PageChoose && form.Installed == null, "fresh computer: the wizard is unchanged");
            form.ApplyStatus(Status("ours_incomplete", false, false, ""));
            Check(form.PageIndex == InstallerForm.PageChoose && form.Installed == null, "an unfinished install of ours keeps the resume wizard");

            form.SelectProvider(1);
            form.ApplyStatus(Status("ours_completed", true, true, "0.1.2")); form.GoTo(InstallerForm.PageMaintain);
            Check(form.MaintenanceHeading == "Hermes уже установлен", "ours: «Hermes уже установлен»");
            Check(String.Join("|", form.MaintenanceActions) == "Обновить набор|Подключить Телеграм|Запасные ключи|Сменить провайдера или ключ", "ours: update, Telegram, backups, change provider");
            Check(form.PrimaryMaintenanceAction == "Обновить набор", "a newer set makes «Обновить набор» the recommended action");
            Check(form.MaintenanceSummary.Contains("gwarden.su") && form.MaintenanceSummary.Contains("glm-5.3") && form.MaintenanceSummary.Contains("обновление до 0.1.3"), "summary names the provider, model and the available update");
            Check(form.SelectedProvider == 0, "the provider screen starts on the installed provider");
            var names = new System.Collections.Generic.List<string>();
            foreach (var p in form.MaintenanceBackupCandidates()) names.Add(p.id);
            Check(String.Join(",", names.ToArray()) == "dahl", "backup candidates exclude the installed primary and «Другой»");
            using (var d = form.CreateMaintenanceBackupDialog())
            {
                d.Keys[0].Text = "BACKUP KEY 111";
                Check(d.TryAccept() == null, "the backup dialog accepts a key on an installed Hermes");
                var br = form.BuildBackupRequest(d);
                Check(br != null && br.Action == "add_backups" && br.Validate() == null && br.Fallbacks.Count == 1 && br.Fallbacks[0].api_key == "BACKUPKEY111" && br.PrimaryEndpoint == "https://gwarden.su/v1", "the dialog result becomes an add_backups request");
            }

            form.ShowTelegramEntry(true);
            Check(form.TelegramEntryShown, "«Подключить Телеграм» opens the token entry");
            form.ShowTelegramEntry(false);
            Check(!form.TelegramEntryShown, "«Отмена» closes it");

            form.BeginChangeProvider();
            Check(form.IsChangeMode && form.PageIndex == InstallerForm.PageChoose && form.Next.Text == "Далее", "«Сменить провайдера или ключ» opens the provider screen");
            form.GoTo(InstallerForm.PageKey);
            Check(form.Next.Text == "Сохранить" && !form.TelegramShown, "the key screen saves instead of installing; no Telegram field in a provider change");
            form.GoTo(InstallerForm.PageChoose);
            form.LeaveChangeMode();
            Check(!form.IsChangeMode && form.PageIndex == InstallerForm.PageMaintain, "«Назад» from the provider screen returns to the maintenance screen");

            form.ApplyStatus(Status("ours_completed", true, false, "0.1.3"));
            Check(Array.IndexOf(form.MaintenanceActions, "Сменить провайдера или ключ") < 0 && form.MaintenanceNote.Contains("вне установщика"), "a model block changed outside the installer is never offered for replacement");
            Check(form.PrimaryMaintenanceAction == null, "an up-to-date set recommends nothing");
            var connected = Status("ours_completed", true, true, "0.1.3"); connected.Telegram = true;
            form.ApplyStatus(connected);
            Check(Array.IndexOf(form.MaintenanceActions, "Подтвердить Телеграм") >= 0, "a connected bot offers the owner confirmation instead of a new token");
            form.OpenTelegramApproval(null);
            Check(form.PageIndex == InstallerForm.PageDone, "the Done-screen approval block is reused");

            form.ApplyStatus(Status("foreign", true, false, ""));
            Check(form.MaintenanceHeading == "У вас уже есть Hermes" && form.MaintenanceSummary.Contains("Настройки модели и ключи не изменятся"), "foreign: our set is offered, model and keys untouched");
            Check(String.Join("|", form.MaintenanceActions) == "Добавить набор|Подключить Телеграм|Запасные ключи", "foreign: add set, Telegram, backups; never a provider change");
            Check(form.PrimaryMaintenanceAction == "Добавить набор", "foreign: «Добавить набор» is recommended");
            form.ApplyStatus(Status("foreign", false, false, ""));
            Check(form.MaintenanceActions.Length == 0 && form.MaintenanceNote.Contains("Python"), "foreign without a usable Python: nothing is offered, the reason is shown");
        }} finally { File.Delete(catalogPath); }
    }

    // --- failure / stopped screen: two ways forward after ANY terminal outcome -------------
    static void FailureActionTests(string catalogPath)
    {
        var auth = New(null); auth.Feed("{\"type\":\"error\",\"code\":\"AUTH\",\"message\":\"ключ отклонён\"}");
        Check(auth.Finish(1).Code == "AUTH", "an error record carries its protocol code");
        Check(New(null).Finish(0).Code == "INSTALL", "no terminal record = INSTALL code");
        var garbage = New(null); garbage.Feed("{oops");
        Check(garbage.Finish(0).Code == "VERIFY", "protocol violation = VERIFY code");
        Check(Outcome.Failure("NETWORK: прокси AUTH-шлюза не отвечает").Code == "NETWORK", "the code is the message prefix, never a substring (AUTH inside a NETWORK text)");
        Check(Outcome.Failure("Просто текст без кода").Code == null, "text without a known prefix has no code");
        Check(!InstallerForm.StopQuestion.Contains("с того же места продолж") && InstallerForm.StopQuestion.Contains("заново"), "the stop question does not promise a resume the worker cannot do");

        File.WriteAllText(catalogPath, "{\"providers\":[" +
            "{\"id\":\"gwarden\",\"label\":\"GWarden\",\"endpoint\":\"https://gwarden.su/v1\",\"model\":\"glm-5.3\"}," +
            "{\"id\":\"dahl\",\"label\":\"Dahl\",\"endpoint\":\"https://inference.dahl.global/v1\",\"model\":\"MiniMaxAI/MiniMax-M2.7\"}," +
            "{\"id\":\"custom\",\"label\":\"Другой (точный адрес API)\"}]}");
        try { using (var form = new InstallerForm()) {
            foreach (string code in new[] { "QUOTA", "NETWORK", "CONFIG", "VERIFY", "INSTALL", "BUSY", "INTERNAL", Outcome.CodeTimeout, Outcome.CodeCancelled })
            {
                form.SelectProvider(1); form.ToggleModelField(); form.Model.Text = "kept-model"; form.ApiKey.Text = "PRIMARYKEY123";
                form.GoTo(InstallerForm.PageInstall);
                // The message mentions AUTH on purpose: only the code decides.
                form.ShowFailure(Outcome.Failure(code, code + ": текст упоминает AUTH"));
                var offered = form.FailureActions;
                Check(offered.Length == 2 && offered[0] == "Повторить" && offered[1] == "Изменить провайдера или ключ", code + ": «Повторить» and «Изменить провайдера или ключ» are offered");
                form.ChangeProviderOrKey();
                Check(form.PageIndex == InstallerForm.PageChoose && form.SelectedProvider == 1 && form.Model.Text == "kept-model" &&
                      form.ApiKey.TextLength == 0 && form.FailureActions.Length == 0, code + ": change goes to the provider screen, keeps the choices, clears the key");
                form.ToggleModelField();
            }
            form.ApiKey.Text = "PRIMARYKEY123";
            form.GoTo(InstallerForm.PageInstall);
            form.ShowFailure(Outcome.Failure("AUTH", "AUTH: ключ отклонён"));
            Check(form.FailureActions.Length == 2 && form.FailureActions[0] == "Повторить" && form.FailureActions[1] == "Изменить ключ", "AUTH: «Повторить» and «Изменить ключ»");
            form.ChangeProviderOrKey();
            Check(form.PageIndex == InstallerForm.PageKey && form.ApiKey.TextLength == 0, "AUTH: change goes straight to the key screen with the rejected key cleared");
            form.SelectProvider(2);
            form.Endpoint.Text = "other.test/custom/api"; form.ConfirmEndpoint.Checked = true;
            form.GoTo(InstallerForm.PageInstall);
            form.ShowFailure(Outcome.Failure("QUOTA", "QUOTA: лимит"));
            form.ChangeProviderOrKey();
            Check(form.IsCustomSelected && form.Endpoint.Text == "other.test/custom/api" && form.CanChoose, "a custom API address and its confirmation survive «Изменить провайдера»");
        }} finally { File.Delete(catalogPath); }
    }

    // --- backup providers (issue #10) ------------------------------------------------
    static string Shown(System.Windows.Forms.ComboBox combo) { return combo.SelectedItem == null ? null : combo.SelectedItem.ToString(); }
    static Request WithBackups(params FallbackEntry[] entries)
    {
        return new Request { endpoint = "https://primary.test/v1", api_key = "TESTKEY123", fallbacks = new System.Collections.Generic.List<FallbackEntry>(entries) };
    }
    static FallbackEntry Fb(string id, string endpoint, string key)
    {
        return new FallbackEntry { provider_id = id, endpoint = endpoint, model = "m-1", api_key = key };
    }
    static void FallbackTests(string catalogPath)
    {
        var ok = WithBackups(Fb("dahl", " dahl.test/v1/ ", "BACKUP KEY 123"), Fb("atria", "https://atria.test/v1", "BACKUPKEY456"));
        Check(ok.Validate() == null, "two well-formed backups are accepted");
        Check(ok.fallbacks[0].endpoint == "https://dahl.test/v1" && ok.fallbacks[0].api_key == "BACKUPKEY123", "backup endpoint normalized and key whitespace cleaned like the primary");
        Check(WithBackups().Validate() == null && new Request { endpoint = "https://x/v1", api_key = "TESTKEY123", fallbacks = null }.Validate() == null, "no backups is valid");
        Check(WithBackups(Fb("a", "https://a.test/v1", "BACKUPKEY1"), Fb("b", "https://b.test/v1", "BACKUPKEY2"), Fb("c", "https://c.test/v1", "BACKUPKEY3")).Validate() == null, "three backups are accepted (every shipped preset)");
        Check(WithBackups(Fb("a", "https://a.test/v1", "BACKUPKEY1"), Fb("b", "https://b.test/v1", "BACKUPKEY2"), Fb("c", "https://c.test/v1", "BACKUPKEY3"), Fb("d", "https://d.test/v1", "BACKUPKEY4")).Validate() != null, "more than three backups rejected");
        Check(WithBackups(Fb("dahl", "https://a.test/v1", "BACKUPKEY1"), Fb("dahl", "https://b.test/v1", "BACKUPKEY2")).Validate() != null, "duplicate backup provider rejected");
        Check(WithBackups(Fb("a", "https://a.test/v1", "BACKUPKEY1"), Fb("b", "https://A.test/v1/", "BACKUPKEY2")).Validate() != null, "duplicate backup endpoint rejected");
        Check(WithBackups(Fb("p", "https://PRIMARY.test/v1/", "BACKUPKEY1")).Validate() != null, "backup equal to the primary endpoint rejected");
        Check(WithBackups(Fb("a", "http://a.test/v1", "BACKUPKEY1")).Validate() != null, "http backup endpoint rejected");
        Check(WithBackups(Fb("a", "https://a.test", "BACKUPKEY1")).Validate() != null, "backup website address (no API path) rejected");
        Check(WithBackups(Fb("a", "https://a.test/v1", "short")).Validate() != null, "short backup key rejected");
        Check(WithBackups(Fb("a", "https://a.test/v1", "ключ-кириллицей")).Validate() != null, "non-ASCII backup key rejected");
        Check(WithBackups(Fb("bad id", "https://a.test/v1", "BACKUPKEY1")).Validate() != null, "malformed provider id rejected");
        Check(WithBackups((FallbackEntry)null).Validate() != null, "null backup entry rejected");
        var redact = new Protocol("TESTSECRET123", delegate { }, delegate { return true; }) { ExtraSecrets = ok.Secrets() };
        Check(!redact.SafeText("x BACKUPKEY456 y").Contains("BACKUPKEY456"), "backup keys are redacted from backend text");
        ok.ClearSecrets();
        Check(ok.api_key == null && ok.fallbacks[0].api_key == null && ok.fallbacks[1].api_key == null, "ClearSecrets drops every key");

        File.WriteAllText(catalogPath, "{\"providers\":[" +
            "{\"id\":\"gwarden\",\"label\":\"GWarden — рекомендуем\",\"endpoint\":\"https://gwarden.su/v1\",\"model\":\"glm-5.3\"}," +
            "{\"id\":\"dahl\",\"label\":\"Dahl — бесплатно\",\"endpoint\":\"https://inference.dahl.global/v1\",\"model\":\"MiniMaxAI/MiniMax-M2.7\"}," +
            "{\"id\":\"atria\",\"label\":\"Atria\",\"endpoint\":\"https://api.atria-asi.ai/v1\",\"model\":\"Atria-Dawn-Preview\"}," +
            "{\"id\":\"custom\",\"label\":\"Другой (точный адрес API)\"}]}");
        try { using (var form = new InstallerForm()) {
            form.ApiKey.Text = "PRIMARYKEY123";
            Check(form.BuildRequest().fallbacks == null && form.BackupCount == 0 && form.BackupSummary == "Добавить запасной ключ (необязательно)", "backups are off by default; the key screen shows only the link");
            var ids = new System.Collections.Generic.List<string>();
            foreach (var p in form.BackupCandidates()) ids.Add(p.id);
            Check(String.Join(",", ids.ToArray()) == "dahl,atria", "backup list excludes custom and the primary");
            using (var d = form.CreateBackupDialog()) {
                Check(d.RowCount == 1 && Shown(d.Providers[0]) == "Dahl", "dialog opens with one row on the first remaining provider (short name)");
                Check(d.TryAccept() != null && d.ErrorText.Contains("1") && d.Result == null, "an empty key is reported inside the dialog");
                d.Keys[0].Text = "BACKUP KEY111";
                d.AddRow();
                Check(d.RowCount == 2 && Shown(d.Providers[1]) == "Atria", "«Ещё один» opens row 2 on a provider row 1 does not use");
                d.AddRow();
                Check(d.RowCount == 2, "never more rows than remaining providers (two here)");
                d.Keys[1].Text = "short";
                Check(d.TryAccept() != null && d.ErrorText.StartsWith("Запасной ключ 2"), "bad backup key is reported inside the dialog");
                d.Keys[1].Text = "BACKUPKEY222";
                d.Providers[1].SelectedIndex = d.Providers[0].SelectedIndex;
                Check(d.TryAccept() != null && d.ErrorText.Length > 0, "the same provider twice is rejected inside the dialog");
                d.Providers[1].SelectedIndex = 1;
                Check(d.TryAccept() == null && d.ErrorText == "", "valid rows are accepted");
                form.ApplyBackupDialog(d);
            }
            Check(form.BackupSummary == "Запасные: Dahl, Atria · изменить", "after ОК the link becomes a one-line summary");
            var request = form.BuildRequest();
            Check(request.Validate() == null && request.fallbacks.Count == 2 && request.fallbacks[0].provider_id == "dahl" &&
                  request.fallbacks[0].endpoint == "https://inference.dahl.global/v1" && request.fallbacks[0].model == "MiniMaxAI/MiniMax-M2.7" &&
                  request.fallbacks[0].api_key == "BACKUPKEY111" && request.fallbacks[1].api_key == "BACKUPKEY222", "BuildRequest carries both backups with preset endpoint, model and cleaned key");
            using (var d = form.CreateBackupDialog()) {
                Check(d.RowCount == 2 && d.Keys[0].Text == "BACKUPKEY111" && Shown(d.Providers[1]) == "Atria", "«изменить» reopens the dialog with the entered values");
                d.RemoveRow(0);
                Check(d.RowCount == 1 && Shown(d.Providers[0]) == "Atria" && d.Keys[0].Text == "BACKUPKEY222" && d.Keys[1].TextLength == 0, "«Убрать» drops a row and moves the next one up");
                // Cancel = the dialog result is simply not applied.
            }
            Check(form.BackupCount == 2, "Отмена keeps the previous backups");
            form.ToggleModelField(); form.ToggleTelegramField();
            Check(form.TelegramShown && form.BackupCount == 2, "model and Telegram panels work as before alongside backups");
            form.ToggleTelegramField(); form.ToggleModelField();
            form.SelectProvider(1);
            Check(form.BackupCount == 1 && form.BackupSummary == "Запасные: Atria · изменить", "a backup that becomes the primary is dropped");
            form.SelectProvider(0);
            using (var d = form.CreateBackupDialog()) { d.RemoveRow(0); Check(d.RowCount == 0 && d.TryAccept() == null, "zero rows + ОК = no backups"); form.ApplyBackupDialog(d); }
            Check(form.BackupCount == 0 && form.BuildRequest().fallbacks == null, "removing every row turns backups off");
        }} finally { File.Delete(catalogPath); }
    }
}
