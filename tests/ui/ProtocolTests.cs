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
        return Environment.ExitCode;
    }
}
