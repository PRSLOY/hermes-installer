using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace HermesSetup
{
    public sealed class Outcome
    {
        public bool Success;
        public string Message;
        public string LaunchPath;
        public string[] LaunchArgs;
        // Install success only: bot username ("-" = connected, name unknown) when the Telegram bot
        // is connected and the Done screen should offer the owner-approval step.
        public string TelegramBot;
        // Telegram actions only (telegram_pending / telegram_approve).
        public string TelegramStatus;
        public TelegramRequest[] TelegramRequests = new TelegramRequest[0];
        // Failure only: the protocol error code (AUTH, QUOTA, ...), or one of the two codes the
        // UI itself produces when it stops the worker. The UI decides by this, never by text.
        public string Code;
        // "status" action only: what is installed (the «Hermes уже установлен» screen).
        public InstallStatus Status;
        // Maintenance actions only: "ok" or "partial" (a non-terminal step fell short; Hermes works).
        public string MaintenanceStatus;
        // Install only: the key was verified and saved (worker "configured" record), then an
        // optional step was stopped or timed out. Hermes works; the Done screen says what was skipped.
        public bool Partial;
        public const string CodeCancelled = "CANCELLED", CodeTimeout = "TIMEOUT";
        public bool Cancelled { get { return Code == CodeCancelled; } }
        // "CODE: text" messages carry their code; any other text has none.
        public static Outcome Failure(string text)
        {
            int colon = text == null ? -1 : text.IndexOf(':');
            string code = colon > 0 && Protocol.ErrorAdvice(text.Substring(0, colon)) != null ? text.Substring(0, colon) : null;
            return Failure(code, text);
        }
        public static Outcome Failure(string code, string text) { return new Outcome { Code = code, Message = text }; }
    }

    // Reply of the worker's "status" action. Never carries a secret: the key and the bot token
    // are only reported as present or not; BaseUrl/Model are the provider's public address.
    public sealed class InstallStatus
    {
        public const string None = "none", OursIncomplete = "ours_incomplete", OursCompleted = "ours_completed", Foreign = "foreign";
        public string State, Message, LaunchPath, BaseUrl, Model, SetVersion, PackageSetVersion;
        public bool SetSupported, ModelOurs, Telegram;
        // A provider change was killed between its two writes: key and address may not match.
        public bool ChangeInterrupted;
        public int Backups;
        public bool Existing { get { return State == OursCompleted || State == Foreign; } }
        // «Обновить набор» is recommended when the package ships a newer set than the one recorded.
        public bool UpdateAvailable
        {
            get
            {
                Version shipped, installed;
                if (!Version.TryParse(Normalize(PackageSetVersion), out shipped)) return false;
                if (!Version.TryParse(Normalize(SetVersion), out installed)) return true;
                return shipped > installed;
            }
        }
        static string Normalize(string v) { return String.IsNullOrEmpty(v) ? "" : (v.IndexOf('.') < 0 ? v + ".0" : v); }
        public string ProviderHost
        {
            get { Uri uri; return Uri.TryCreate(BaseUrl ?? "", UriKind.Absolute, out uri) ? uri.Host : ""; }
        }
    }

    public sealed class TelegramRequest
    {
        public string Id, UserId, Name, Username;
        public string Display
        {
            get
            {
                string name = String.IsNullOrEmpty(Name) ? "Без имени" : Name;
                return String.IsNullOrEmpty(Username) ? name : name + " (@" + Username + ")";
            }
        }
    }

    // «Запуск от имени администратора» by a standard user with someone else's admin password runs
    // the installer AS that admin: Hermes would land in the admin's profile, not the person's.
    // Detected as: elevated token whose user differs from the owner of this session's desktop
    // shell (explorer.exe). Same-user elevation (and Windows Sandbox, elevated as its only user)
    // is fine. Any lookup failure = no warning: this guard never blocks a normal run.
    public static class ElevationGuard
    {
        public const string Message = "Запустите установщик двойным щелчком, без прав администратора.\r\n\r\nСейчас он запущен от имени другой учётной записи, и Hermes установился бы ей, а не вам.";
        [DllImport("user32.dll")] static extern IntPtr GetShellWindow();
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        const uint ProcessQueryLimitedInformation = 0x1000, TokenQuery = 0x0008;

        public static string ShellOwnerSid()
        {
            IntPtr process = IntPtr.Zero, token = IntPtr.Zero;
            try
            {
                uint pid;
                IntPtr shell = GetShellWindow();
                if (shell == IntPtr.Zero || GetWindowThreadProcessId(shell, out pid) == 0 || pid == 0) return null;
                process = OpenProcess(ProcessQueryLimitedInformation, false, pid);
                if (process == IntPtr.Zero || !OpenProcessToken(process, TokenQuery, out token)) return null;
                using (var identity = new WindowsIdentity(token)) return identity.User == null ? null : identity.User.Value;
            }
            catch { return null; }
            finally
            {
                if (token != IntPtr.Zero) CloseHandle(token);
                if (process != IntPtr.Zero) CloseHandle(process);
            }
        }
        // Pure decision (tests): warn only for an elevated run whose user is not the shell's.
        public static bool Warn(bool elevated, string runningSid, string shellSid)
        {
            return elevated && !String.IsNullOrEmpty(runningSid) && !String.IsNullOrEmpty(shellSid) && !String.Equals(runningSid, shellSid, StringComparison.OrdinalIgnoreCase);
        }
        public static bool ShouldWarn()
        {
            try
            {
                using (var me = WindowsIdentity.GetCurrent())
                {
                    bool elevated = new WindowsPrincipal(me).IsInRole(WindowsBuiltInRole.Administrator);
                    return Warn(elevated, me.User == null ? null : me.User.Value, elevated ? ShellOwnerSid() : null);
                }
            }
            catch { return false; }
        }
    }

    public static class LaunchPolicy
    {
        // Launch the PACKED desktop app, never `hermes desktop`: the CLI rebuilds the
        // desktop (npm install + electron-builder) on every launch. The official
        // installer's own shortcuts target this exe for exactly that reason.
        static string DesktopDir()
        {
            string name = String.Equals(Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE"), "ARM64", StringComparison.OrdinalIgnoreCase) ? "win-arm64-unpacked" : "win-unpacked";
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "hermes", "hermes-agent", "apps", "desktop", "release", name);
        }
        public static string ExpectedPath { get { return Path.Combine(DesktopDir(), "Hermes.exe"); } }
        public static bool Validate(string path, string[] args)
        {
            try
            {
                return !String.IsNullOrWhiteSpace(path) && Path.IsPathRooted(path) &&
                    String.Equals(Path.GetFullPath(path), ExpectedPath, StringComparison.OrdinalIgnoreCase) &&
                    File.Exists(path) && args != null && args.Length == 0;
            }
            catch { return false; }
        }
        public static void Start(Outcome result)
        {
            if (!result.Success || !Validate(result.LaunchPath, result.LaunchArgs))
                throw new InvalidOperationException("Запуск не подтверждён. Повторите проверку установки.");
            Process.Start(new ProcessStartInfo(result.LaunchPath) {
                UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(result.LaunchPath)
            });
        }
    }

    public sealed class Protocol
    {
        readonly string secret;
        readonly Action<string> progress;
        readonly Func<string,string[],bool> launchValidator;
        readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 32768, RecursionLimit = 8 };
        // Optional structured side channel: the numbered progress message stays
        // exactly as before ("Этап N/M: ..."), while the UI may map the upstream
        // stage id to a human phrase without parsing service words out of text.
        public Action<int,int,string> StageProgress;
        // Telegram action session: the only terminal success is {"type":"telegram",...};
        // an install "success" (launch path) is then a protocol violation, and vice versa.
        public bool TelegramMode;
        // "status" session: the only terminal success is {"type":"status",...}. Maintenance session
        // (update_set, change_provider, add_backups, telegram_connect, foreign_add_set): {"type":"done",...}.
        public bool StatusMode, MaintenanceMode;
        // Install session: set by the non-terminal {"type":"configured"} record once the key is
        // verified and saved (phase completed). Only a LaunchPolicy-valid Desktop path is accepted.
        public string ConfiguredLaunchPath { get; private set; }
        static readonly string[] States = { InstallStatus.None, InstallStatus.OursIncomplete, InstallStatus.OursCompleted, InstallStatus.Foreign };
        static readonly System.Text.RegularExpressions.Regex VersionShape = new System.Text.RegularExpressions.Regex("^([0-9]{1,4}(\\.[0-9]{1,4}){0,3})?$");
        static readonly string[] TelegramStatuses = { "pending", "none", "done", "off", "failed", "approved", "approved_partial", "expired" };
        static readonly System.Text.RegularExpressions.Regex RequestIdShape = new System.Text.RegularExpressions.Regex("^[0-9a-f]{16}$");
        static readonly System.Text.RegularExpressions.Regex UserIdShape = new System.Text.RegularExpressions.Regex("^[0-9]{1,20}$");
        static readonly System.Text.RegularExpressions.Regex UsernameShape = new System.Text.RegularExpressions.Regex("^([A-Za-z0-9_]{5,64})?$");
        static readonly System.Text.RegularExpressions.Regex BotShape = new System.Text.RegularExpressions.Regex("^([A-Za-z0-9_]{5,64}|-)$");
        bool invalid, terminal;
        Outcome final;
        int count;
        public Protocol(string secret, Action<string> progress) : this(secret, progress, LaunchPolicy.Validate) { }
        // Explicit dependency injection for offline tests; the GUI always uses the production policy.
        public Protocol(string secret, Action<string> progress, Func<string,string[],bool> launchValidator)
        { this.secret = secret; this.progress = progress; this.launchValidator = launchValidator; }
        // Other keys of the same request (Telegram token, backup keys): redacted the same way.
        public string[] ExtraSecrets;
        public void Invalidate() { invalid = true; }
        public string SafeText(string text)
        {
            if (!String.IsNullOrEmpty(secret))
            {
                text = text.Replace(secret, "[ключ скрыт]");
                text = text.Replace(Uri.EscapeDataString(secret), "[ключ скрыт]");
            }
            if (ExtraSecrets != null)
                foreach (string extra in ExtraSecrets)
                    if (!String.IsNullOrEmpty(extra))
                    {
                        text = text.Replace(extra, "[ключ скрыт]");
                        text = text.Replace(Uri.EscapeDataString(extra), "[ключ скрыт]");
                    }
            var safe = new StringBuilder();
            foreach (char c in text) if (!Char.IsControl(c) || c == '\n' || c == '\r') safe.Append(c);
            return safe.ToString();
        }
        static string Text(Dictionary<string,object> record, string name)
        {
            object value;
            if (!record.TryGetValue(name, out value) || !(value is string) || String.IsNullOrWhiteSpace((string)value)) throw new FormatException();
            return (string)value;
        }
        // A string field that may be empty, without control characters.
        static string Plain(Dictionary<string,object> record, string name, int max)
        {
            object value;
            if (!record.TryGetValue(name, out value) || !(value is string)) throw new FormatException();
            string text = (string)value;
            if (text.Length > max) throw new FormatException();
            foreach (char c in text) if (Char.IsControl(c)) throw new FormatException();
            return text;
        }
        static bool Flag(Dictionary<string,object> record, string name)
        {
            object value;
            if (!record.TryGetValue(name, out value) || !(value is bool)) throw new FormatException();
            return (bool)value;
        }
        InstallStatus ParseStatus(Dictionary<string,object> record)
        {
            if (!StatusMode || record.Count != 13) throw new FormatException();
            var s = new InstallStatus { State = Text(record, "state") };
            if (Array.IndexOf(States, s.State) < 0) throw new FormatException();
            s.BaseUrl = Plain(record, "base_url", 2048);
            if (s.BaseUrl.Length > 0)
            {
                Uri uri;
                if (!Uri.TryCreate(s.BaseUrl, UriKind.Absolute, out uri) || uri.Scheme != "https" || uri.UserInfo != "" || uri.Query != "" || uri.Fragment != "" || s.BaseUrl.IndexOf(' ') >= 0) throw new FormatException();
            }
            s.Model = SafeText(Plain(record, "model", 256));
            s.SetVersion = Plain(record, "set_version", 24);
            s.PackageSetVersion = Plain(record, "package_set_version", 24);
            if (!VersionShape.IsMatch(s.SetVersion) || !VersionShape.IsMatch(s.PackageSetVersion)) throw new FormatException();
            s.SetSupported = Flag(record, "set_supported");
            s.ModelOurs = Flag(record, "model_ours");
            s.Telegram = Flag(record, "telegram");
            s.ChangeInterrupted = Flag(record, "change_interrupted");
            object backups;
            if (!record.TryGetValue("backups", out backups) || !(backups is int) || (int)backups < 0 || (int)backups > 99) throw new FormatException();
            s.Backups = (int)backups;
            // Only the packed Desktop at the expected place may be launched; anything else = no launch.
            string launch = Plain(record, "launch_path", 1024);
            s.LaunchPath = launch.Length > 0 && launchValidator(launch, new string[0]) ? launch : "";
            return s;
        }
        public void Feed(string line)
        {
            if (invalid) return;
            if (++count > 10000 || terminal || String.IsNullOrWhiteSpace(line) || line.Length > 32768) { invalid = true; return; }
            try
            {
                // JavaScriptSerializer accepts some JavaScript extensions. Require JSON lexical syntax first.
                JsonSyntax.Check(line);
                var record = json.DeserializeObject(line) as Dictionary<string,object>;
                if (record == null) throw new FormatException();
                string type = Text(record, "type");
                string message = Text(record, "message");
                if (message.Length > 4000) throw new FormatException();
                if (type == "progress") {
                    int stageIndex = 0, stageTotal = 0; string stage = null;
                    if (record.Count != 2) {
                        string iKey = record.ContainsKey("index") ? "index" : "step";
                        string nKey = iKey == "index" ? "count" : "total";
                        if (record.Count != (iKey == "index" ? 5 : 4)) throw new FormatException();
                        object iv, nv;
                        if (!record.TryGetValue(iKey,out iv) || !record.TryGetValue(nKey,out nv) || !(iv is int) || !(nv is int)) throw new FormatException();
                        stageIndex=(int)iv; stageTotal=(int)nv;
                        if (stageIndex<1 || stageTotal<1 || stageIndex>stageTotal || stageTotal>1000) throw new FormatException();
                        if (iKey == "index") stage = Text(record,"stage");
                        message="Этап "+stageIndex+"/"+stageTotal+": "+message;
                    }
                    string rendered = SafeText(message);
                    progress(rendered);
                    if (stage != null && StageProgress != null) { try { StageProgress(stageIndex, stageTotal, stage); } catch { } }
                    return;
                }
                if (type == "error")
                {
                    if (record.Count != 3) throw new FormatException();
                    string code = Text(record, "code");
                    string advice = ErrorAdvice(code);
                    if (advice == null) throw new FormatException();
                    final = Outcome.Failure(code, code + ": " + advice + "\r\n" + SafeText(message));
                    terminal = true;
                    return;
                }
                if (type == "telegram")
                {
                    if (!TelegramMode || record.Count != 4) throw new FormatException();
                    string status = Text(record, "status");
                    if (Array.IndexOf(TelegramStatuses, status) < 0) throw new FormatException();
                    object listValue;
                    if (!record.TryGetValue("requests", out listValue)) throw new FormatException();
                    var list = listValue as object[] ?? (listValue is System.Collections.ArrayList ? ((System.Collections.ArrayList)listValue).ToArray() : null);
                    if (list == null || list.Length > 10) throw new FormatException();
                    var requests = new List<TelegramRequest>();
                    foreach (object item in list)
                    {
                        var r = item as Dictionary<string,object>;
                        if (r == null || r.Count != 4) throw new FormatException();
                        string id = Text(r, "id"), uid = Text(r, "user_id");
                        object nameValue, userValue;
                        if (!r.TryGetValue("name", out nameValue) || !(nameValue is string) || !r.TryGetValue("username", out userValue) || !(userValue is string)) throw new FormatException();
                        string name = (string)nameValue, username = (string)userValue;
                        if (!RequestIdShape.IsMatch(id) || !UserIdShape.IsMatch(uid) || !UsernameShape.IsMatch(username) || name.Length > 64) throw new FormatException();
                        requests.Add(new TelegramRequest { Id = id, UserId = uid, Name = SafeText(name).Replace("\r", " ").Replace("\n", " "), Username = username });
                    }
                    final = new Outcome { Success = true, Message = SafeText(message), TelegramStatus = status, TelegramRequests = requests.ToArray() };
                    terminal = true;
                    return;
                }
                if (type == "configured")
                {
                    if (TelegramMode || StatusMode || MaintenanceMode || ConfiguredLaunchPath != null || record.Count != 3) throw new FormatException();
                    string configured = Text(record, "launch_path");
                    if (!launchValidator(configured, new string[0])) throw new FormatException();
                    ConfiguredLaunchPath = configured;
                    progress(SafeText(message));
                    return;
                }
                if (type == "status")
                {
                    InstallStatus status = ParseStatus(record);
                    status.Message = SafeText(message);
                    final = new Outcome { Success = true, Message = status.Message, Status = status };
                    terminal = true;
                    return;
                }
                if (type == "done")
                {
                    if (!MaintenanceMode || (record.Count != 3 && !(record.Count == 4 && record.ContainsKey("telegram_bot")))) throw new FormatException();
                    string result = Text(record, "status");
                    if (result != "ok" && result != "partial") throw new FormatException();
                    string doneBot = null;
                    if (record.Count == 4) { doneBot = Text(record, "telegram_bot"); if (!BotShape.IsMatch(doneBot)) throw new FormatException(); }
                    final = new Outcome { Success = true, Message = SafeText(message), MaintenanceStatus = result, TelegramBot = doneBot };
                    terminal = true;
                    return;
                }
                if (TelegramMode || StatusMode || MaintenanceMode || type != "success" || (record.Count != 4 && !(record.Count == 5 && record.ContainsKey("telegram_bot")))) throw new FormatException();
                string bot = null;
                if (record.Count == 5) { bot = Text(record, "telegram_bot"); if (!BotShape.IsMatch(bot)) throw new FormatException(); }
                string path = Text(record, "launch_path");
                object argValue;
                if (!record.TryGetValue("launch_args", out argValue)) throw new FormatException();
                var array = argValue as object[];
                if (array == null || array.Length > 1 || (array.Length == 1 && !(array[0] is string))) throw new FormatException();
                var args = array.Length == 0 ? new string[0] : new[] { (string)array[0] };
                if (!launchValidator(path, args)) throw new FormatException();
                final = new Outcome { Success = true, Message = SafeText(message), LaunchPath = path, LaunchArgs = args, TelegramBot = bot };
                terminal = true;
            }
            catch { invalid = true; }
        }
        public Outcome Finish(int exitCode)
        {
            if (invalid) return Outcome.Failure("VERIFY: Ответ установщика повреждён или не соответствует протоколу. Установка не подтверждена. Скачайте пакет заново или обратитесь в поддержку.");
            if (final != null && !final.Success) return final;
            if (exitCode != 0 || !terminal || final == null) return Outcome.Failure("INSTALL: Установщик завершился без подтверждения успеха. Проверьте сеть и повторите; если ошибка повторяется, обратитесь в поддержку.");
            return final;
        }
        public static string ErrorAdvice(string code)
        {
            switch (code)
            {
                case "AUTH": return "Проверьте API-ключ и его срок действия, затем повторите.";
                case "NETWORK": return "Проверьте интернет, адрес сервера и VPN, затем повторите.";
                case "QUOTA": return "Проверьте баланс или лимит запросов у провайдера, затем повторите.";
                case "INSTALL": return "Установка не завершилась. Прочитайте причину ниже, проверьте интернет/VPN и свободное место, затем повторите.";
                case "CONFIG": return "Настройки не подтверждены. Следуйте сообщению ниже; не удаляйте существующие данные.";
                case "VERIFY": return "Не получен проверенный ответ Hermes. Проверьте доступность модели и повторите.";
                case "UNSUPPORTED": return "Используйте 64-разрядную Windows 10/11.";
                case "INPUT": return "Исправьте адрес сервера, ключ или название модели и повторите.";
                case "BUSY": return "Дождитесь завершения другого установщика и повторите.";
                case "INTERNAL": return "Повторите попытку; если ошибка повторяется, обратитесь в поддержку.";
                default: return null;
            }
        }
    }

    // Validates strict JSON tokens and duplicate object keys before deserialization.
    internal sealed class JsonSyntax
    {
        string s; int i;
        JsonSyntax(string value) { s = value; }
        public static void Check(string value) { var p = new JsonSyntax(value); p.Value(0); p.Ws(); if (p.i != value.Length) throw new FormatException(); }
        void Ws() { while (i < s.Length && " \t\r\n".IndexOf(s[i]) >= 0) i++; }
        bool Eat(char c) { Ws(); if (i < s.Length && s[i] == c) { i++; return true; } return false; }
        void Need(char c) { if (!Eat(c)) throw new FormatException(); }
        string Str()
        {
            Ws(); int start = i; Need('"');
            while (i < s.Length)
            {
                char c = s[i++];
                if (c == '"') return new JavaScriptSerializer().Deserialize<string>(s.Substring(start, i-start));
                if (c < 32) throw new FormatException();
                if (c == '\\')
                {
                    if (i >= s.Length) throw new FormatException();
                    char e = s[i++];
                    if (e == 'u') { for (int n=0; n<4; n++) if (i >= s.Length || !Uri.IsHexDigit(s[i++])) throw new FormatException(); }
                    else if ("\"\\/bfnrt".IndexOf(e) < 0) throw new FormatException();
                }
            }
            throw new FormatException();
        }
        void Value(int depth)
        {
            if (depth > 8) throw new FormatException();
            Ws(); if (i >= s.Length) throw new FormatException();
            if (s[i] == '"') { Str(); return; }
            if (Eat('{'))
            {
                var keys = new HashSet<string>();
                if (Eat('}')) return;
                do { if (!keys.Add(Str())) throw new FormatException(); Need(':'); Value(depth+1); } while (Eat(','));
                Need('}'); return;
            }
            if (Eat('[')) { if (Eat(']')) return; do { Value(depth+1); } while (Eat(',')); Need(']'); return; }
            foreach (string token in new[] { "true", "false", "null" })
                if (s.Substring(i).StartsWith(token, StringComparison.Ordinal)) { i += token.Length; return; }
            int start = i;
            if (i < s.Length && s[i] == '-') i++;
            if (i >= s.Length) throw new FormatException();
            if (s[i] == '0') i++;
            else { if (s[i] < '1' || s[i] > '9') throw new FormatException(); Digits(); }
            if (i < s.Length && s[i] == '.') { i++; int before=i; Digits(); if (i==before) throw new FormatException(); }
            if (i < s.Length && (s[i]=='e' || s[i]=='E')) { i++; if (i<s.Length && (s[i]=='+' || s[i]=='-')) i++; int before=i; Digits(); if (i==before) throw new FormatException(); }
            if (i == start) throw new FormatException();
        }
        void Digits() { while (i<s.Length && s[i]>='0' && s[i]<='9') i++; }
    }
}
