using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace HermesSetup
{
    public sealed class Outcome
    {
        public bool Success;
        public string Message;
        // Backend error code (AUTH/NETWORK/QUOTA/INSTALL/CONFIG/VERIFY/...). Null on success
        // or a non-protocol failure. The UI picks recovery actions from this, not from the text.
        public string Code;
        public string LaunchPath;
        public string[] LaunchArgs;
        // Install success only: bot username ("-" = connected, name unknown) when the Telegram bot
        // is connected and the Done screen should offer the owner-approval step.
        public string TelegramBot;
        // Telegram actions only (telegram_pending / telegram_approve).
        public string TelegramStatus;
        public TelegramRequest[] TelegramRequests = new TelegramRequest[0];
        public static Outcome Failure(string text) { return new Outcome { Message = text }; }
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
                    final = new Outcome { Code = code, Message = code + ": " + advice + "\r\n" + SafeText(message) };
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
                if (TelegramMode || type != "success" || (record.Count != 4 && !(record.Count == 5 && record.ContainsKey("telegram_bot")))) throw new FormatException();
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
