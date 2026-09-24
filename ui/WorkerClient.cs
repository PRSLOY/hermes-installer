using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace HermesSetup
{
    // One optional backup provider (issue #10). Hermes switches to it by itself when
    // the primary fails (fallback_providers). The key goes only to backend\fallbacks.py.
    public sealed class FallbackEntry
    {
        public string provider_id, endpoint, model, api_key;
    }
    public sealed class Request
    {
        public const int MaxFallbacks = 3;   // primary + 3 backups = every shipped preset
        public int protocol = 1;
        public string action = "install";
        public string endpoint, api_key, model, provider_name;
        // Optional; null/empty = Telegram skipped. Never logged or shown back.
        public string telegram_bot_token;
        // Optional; null/empty = no backup providers. 0-2 entries, keys never shown back.
        public List<FallbackEntry> fallbacks;
        public static readonly System.Text.RegularExpressions.Regex ProviderIdShape =
            new System.Text.RegularExpressions.Regex("^[A-Za-z0-9_.-]{1,64}$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);
        public static readonly System.Text.RegularExpressions.Regex TelegramTokenShape =
            new System.Text.RegularExpressions.Regex("^[0-9]{1,20}:[A-Za-z0-9_-]{30,64}$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);
        public static string NormalizeEndpoint(string raw)
        {
            string value=(raw??"").Trim().TrimEnd('/');
            if(value.Length>0 && value.IndexOf("://",StringComparison.Ordinal)<0) value="https://"+value;
            return value;
        }
        static bool HasWhitespace(string value)
        { foreach(char c in value) if(Char.IsWhiteSpace(c)||Char.IsControl(c)) return true; return false; }
        public string Validate()
        {
            endpoint = NormalizeEndpoint(endpoint);
            // Auto-clean pasted/typed keys: drop all whitespace and control chars.
            // This turns a novice's "key with a space" into a usable key instead of a dead-end error.
            api_key = (api_key ?? "").Replace(" ", "").Replace("\t", "").Replace("\r", "").Replace("\n", "").Replace("\u00A0", "").Replace("\u2007", "").Replace("\u200B", "");
            Uri uri;
            if (String.IsNullOrWhiteSpace(endpoint) || endpoint.Length > 4096 || endpoint.IndexOf('\\')>=0 || HasWhitespace(endpoint) ||
                !Uri.TryCreate(endpoint, UriKind.Absolute, out uri) || uri.Scheme != "https" ||
                String.IsNullOrEmpty(uri.Host) || uri.UserInfo != "" || uri.Query != "" || uri.Fragment != "")
                return "Введите HTTPS-адрес API без логина, пароля, параметров и фрагмента. Например: https://api.example.com/v1";
            if (uri.AbsolutePath == "/") return "Это адрес сайта. Введите точный адрес API из документации провайдера (например /api/v1). Мы не добавляем /v1 наугад.";
            if (api_key == null || api_key.Length < 8 || api_key.Length > 8192)
                return "Введите API-ключ провайдера (от 8 до 8192 символов).";
            foreach (char c in api_key) if (c < 33 || c > 126) return "API-ключ не должен содержать пробелы или переносы строк. Скопируйте ключ заново.";
            foreach (string field in new[] { model, provider_name })
                if (field != null) { if (field.Length > 256) return "Название модели слишком длинное."; foreach(char c in field) if(c < 32) return "Удалите переносы строк из названия модели."; }
            if (telegram_bot_token != null)
            {
                var tg = new StringBuilder();
                foreach (char c in telegram_bot_token) if (!Char.IsWhiteSpace(c) && !Char.IsControl(c) && c != '\u200B') tg.Append(c);
                telegram_bot_token = tg.Length == 0 ? null : tg.ToString();
                if (telegram_bot_token != null && !TelegramTokenShape.IsMatch(telegram_bot_token))
                    return "Токен Телеграм-бота выглядит неверно. Скопируйте его из @BotFather целиком (вида 123456789:AA…) или оставьте поле пустым.";
            }
            return ValidateFallbacks();
        }
        // Backup keys follow the primary's rules. Short messages: they must fit the one
        // error line of the key screen while the backup rows are open (560x500 budget).
        public string ValidateFallbacks()
        {
            if (fallbacks == null) return null;
            if (fallbacks.Count > MaxFallbacks) return "Можно добавить не больше трёх запасных ключей.";
            var ids = new HashSet<string>(StringComparer.Ordinal);
            var endpoints = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            endpoints.Add(endpoint);
            for (int i = 0; i < fallbacks.Count; i++)
            {
                FallbackEntry entry = fallbacks[i];
                string n = (i + 1).ToString();
                if (entry == null || entry.provider_id == null || !ProviderIdShape.IsMatch(entry.provider_id))
                    return "Запасной ключ " + n + ": выберите провайдера.";
                entry.endpoint = NormalizeEndpoint(entry.endpoint);
                var probe = new Request { endpoint = entry.endpoint, api_key = "VALIDATION-ONLY" };
                if (probe.Validate() != null) return "Запасной провайдер " + n + ": неверный адрес API.";
                string original = entry.api_key;
                var keyProbe = new Request { endpoint = entry.endpoint, api_key = original };
                if (keyProbe.Validate() != null) return "Запасной ключ " + n + ": от 8 до 8192 символов, без пробелов.";
                entry.api_key = keyProbe.api_key;
                if (entry.model != null && entry.model.Length > 256) return "Запасной провайдер " + n + ": слишком длинное имя модели.";
                if (entry.model != null) foreach (char c in entry.model) if (c < 32) return "Запасной провайдер " + n + ": неверное имя модели.";
                if (!ids.Add(entry.provider_id)) return "Запасной провайдер " + n + " уже выбран. Выберите другой.";
                if (!endpoints.Add(entry.endpoint)) return "Запасной провайдер " + n + " совпадает с основным или другим.";
            }
            return null;
        }
        // Every key this request carries: redacted from any text the backend sends back.
        public string[] Secrets()
        {
            var list = new List<string>();
            if (!String.IsNullOrEmpty(api_key)) list.Add(api_key);
            if (!String.IsNullOrEmpty(telegram_bot_token)) list.Add(telegram_bot_token);
            if (fallbacks != null) foreach (var f in fallbacks) if (f != null && !String.IsNullOrEmpty(f.api_key)) list.Add(f.api_key);
            return list.ToArray();
        }
        public void ClearSecrets()
        {
            api_key = null; telegram_bot_token = null;
            if (fallbacks != null) foreach (var f in fallbacks) if (f != null) f.api_key = null;
        }
    }
    public static class WorkerClient
    {
        public static string Quote(string value)
        {
            // Windows argv quoting. No shell is ever involved.
            var b = new StringBuilder("\""); int slash = 0;
            foreach(char c in value)
            {
                if (c == '\\') { slash++; continue; }
                if (c == '"') b.Append('\\', slash*2+1).Append(c);
                else b.Append('\\', slash).Append(c);
                slash=0;
            }
            return b.Append('\\', slash*2).Append('"').ToString();
        }
        public static ProcessStartInfo StartInfo(string worker)
        {
            string system = Environment.GetFolderPath(Environment.SpecialFolder.System);
            var psi = new ProcessStartInfo(Path.Combine(system, "WindowsPowerShell", "v1.0", "powershell.exe"),
                "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(Path.GetFullPath(worker)));
            psi.WorkingDirectory = Path.GetDirectoryName(Path.GetDirectoryName(Path.GetFullPath(worker)));
            psi.UseShellExecute=false; psi.CreateNoWindow=true;
            psi.RedirectStandardInput=true; psi.RedirectStandardOutput=true; psi.RedirectStandardError=true;
            psi.StandardOutputEncoding=new UTF8Encoding(false,true); psi.StandardErrorEncoding=new UTF8Encoding(false,true);
            // Do not forward unrelated provider secrets or test/fixture switches from the launching agent.
            var inherited = new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
            foreach(string name in new[] { "SystemRoot", "WINDIR", "COMSPEC", "OS", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "ALLUSERSPROFILE", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "HERMES_HOME",
                // System (non-secret) variables. Without them the elevated Microsoft VC++
                // redistributable exits 0x80070003 (path not found): voice notes and
                // greenlet-based marketplaces then fail to load (sandbox 2026-09-23).
                "SystemDrive", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "HOMEDRIVE", "HOMEPATH", "PUBLIC" })
            { string value=Environment.GetEnvironmentVariable(name); if(value!=null) inherited[name]=value; }
            psi.EnvironmentVariables.Clear();
            foreach(var entry in inherited) psi.EnvironmentVariables[entry.Key]=entry.Value;
            return psi;
        }
        // Hung-worker guard: this long without a single stdout record = the worker is stuck.
        // Its longest own child bound is 40 min (install.ps1) and it streams stage records,
        // so 90 silent minutes are never a healthy install.
        public static readonly TimeSpan InstallSilenceLimit = TimeSpan.FromMinutes(90);
        // Telegram actions print only their terminal record; the worker bounds telegram.py at 300 s.
        public static readonly TimeSpan TelegramLimit = TimeSpan.FromMinutes(8);
        public const string StoppedMessage = "Установка остановлена. Скачанные файлы не удалены. Нажмите «Повторить», чтобы запустить установку снова, или измените провайдера и ключ.";
        public const string HungMessage = "Установка слишком долго не сообщала о ходе работы и остановлена. Проверьте интернет или VPN и нажмите «Повторить». Скачанные файлы не удалены.";

        public static async Task<Outcome> RunAsync(string worker, Request request, Action<string> progress)
        { return await RunAsync(worker, request, progress, LaunchPolicy.Validate, null); }
        // Tests may inject a launch validator. Production calls the overload above only.
        public static async Task<Outcome> RunAsync(string worker, Request request, Action<string> progress, Func<string,string[],bool> validator)
        { return await RunAsync(worker, request, progress, validator, null); }
        // The wizard subscribes to the structured stage id (auto-mapped to a human
        // phrase) while keeping the same numbered progress text in the journal.
        // Cancelling the token kills the worker's whole process tree.
        public static Task<Outcome> RunAsync(string worker, Request request, Action<string> progress, Action<int,int,string> stage, CancellationToken cancel)
        { return RunAsync(worker, request, progress, LaunchPolicy.Validate, stage, cancel, InstallSilenceLimit); }
        public static Task<Outcome> RunAsync(string worker, Request request, Action<string> progress, Func<string,string[],bool> validator, Action<int,int,string> stage)
        { return RunAsync(worker, request, progress, validator, stage, CancellationToken.None, InstallSilenceLimit); }
        // Tests inject a short silence limit to drive the hung-worker path.
        public static async Task<Outcome> RunAsync(string worker, Request request, Action<string> progress, Func<string,string[],bool> validator, Action<int,int,string> stage, CancellationToken cancel, TimeSpan silenceLimit)
        {
            string error = request.Validate();
            if(error!=null) return Outcome.Failure("INPUT", error);
            if(!File.Exists(worker)) return Outcome.Failure("INSTALL: Не найден backend/worker.ps1. Распакуйте весь ZIP в одну папку и запустите HermesSetup.exe оттуда.");
            var protocol = new Protocol(request.api_key,progress,validator);
            protocol.ExtraSecrets = request.Secrets();
            if (stage != null) protocol.StageProgress = stage;
            byte[] bytes = new UTF8Encoding(false,true).GetBytes(new JavaScriptSerializer().Serialize(request)+"\n");
            try { return await Execute(worker, bytes, protocol, false, cancel, silenceLimit); }
            finally { Array.Clear(bytes,0,bytes.Length); }
        }
        // One worker session: one JSON object on stdin, NDJSON out, exactly one terminal record.
        // The whole process tree lives in a job: cancel or silenceLimit without any stdout record
        // kills all of it. The install job also dies with the window (killOnClose); a Telegram
        // action is left to finish if the window closes, as before (a gateway restart cut in
        // half would leave the bot stopped).
        static async Task<Outcome> Execute(string worker, byte[] payload, Protocol protocol, bool telegram, CancellationToken cancel, TimeSpan silenceLimit)
        {
            using(var job = new ProcessJob(!telegram))
            using(var process = new Process { StartInfo=StartInfo(worker) })
            {
                try
                {
                    if(!process.Start()) return Outcome.Failure(telegram ? "INSTALL: Не удалось запустить Windows PowerShell." : "INSTALL: Не удалось запустить Windows PowerShell. Обратитесь в поддержку.");
                }
                catch { return Outcome.Failure(telegram ? "INSTALL: Windows заблокировала запуск PowerShell." : "INSTALL: Windows заблокировала запуск установщика или PowerShell недоступен. Обратитесь в поддержку; не отключайте защиту Windows."); }
                job.Assign(process);
                var lastRecord = new long[] { DateTime.UtcNow.Ticks };
                Task stdout = Task.Run(delegate { ReadLines(process.StandardOutput, protocol, lastRecord); });
                Task<bool> stderr = Task.Run(delegate {
                    bool any=false; char[] chars=new char[1024];
                    try { int n; while((n=process.StandardError.Read(chars,0,chars.Length))>0) any=true; }
                    catch { any=true; } return any;
                });
                bool writeFailed=false;
                try { await process.StandardInput.BaseStream.WriteAsync(payload,0,payload.Length); await process.StandardInput.BaseStream.FlushAsync(); }
                catch { writeFailed=true; }
                finally { try { process.StandardInput.Close(); } catch {} }
                string stop = await Task.Run(delegate {
                    while(!process.WaitForExit(250))
                    {
                        if(cancel.IsCancellationRequested) return Outcome.CodeCancelled;
                        if(DateTime.UtcNow.Ticks - Interlocked.Read(ref lastRecord[0]) > silenceLimit.Ticks) return Outcome.CodeTimeout;
                    }
                    return null;
                });
                if(stop!=null)
                {
                    job.Kill();
                    await Task.Run(delegate { process.WaitForExit(15000); });
                    // Bounded drain: a descendant that broke away may still hold a pipe open.
                    await Task.WhenAny(Task.WhenAll(stdout, stderr), Task.Delay(5000));
                    if(stop==Outcome.CodeCancelled) return Outcome.Failure(Outcome.CodeCancelled, StoppedMessage);
                    return Outcome.Failure(Outcome.CodeTimeout, telegram ? "Телеграм не ответил вовремя, проверка остановлена. Повторите через минуту." : HungMessage);
                }
                job.Release();
                await stdout;
                if(await stderr || writeFailed) protocol.Invalidate();
                return protocol.Finish(process.ExitCode);
            }
        }
        // Short Done-screen actions: "telegram_pending" (no arguments) or "telegram_approve"
        // (request id + user id picked by the owner's click). Same process contract as install:
        // one JSON object on stdin, NDJSON out, exactly one terminal record.
        public static Task<Outcome> RunTelegramAsync(string worker, string action, string requestId, string userId)
        { return RunTelegramAsync(worker, action, requestId, userId, TelegramLimit); }
        // Tests inject a short limit to drive the hung-action path.
        public static async Task<Outcome> RunTelegramAsync(string worker, string action, string requestId, string userId, TimeSpan limit)
        {
            var payload = new Dictionary<string,object> { { "protocol", 1 }, { "action", action } };
            if (action == "telegram_approve")
            {
                if (requestId == null || userId == null) return Outcome.Failure("INPUT: Не выбран запрос.");
                payload["request_id"] = requestId; payload["user_id"] = userId;
            }
            else if (action != "telegram_pending") return Outcome.Failure("INPUT: Неизвестное действие.");
            if(!File.Exists(worker)) return Outcome.Failure("INSTALL: Не найден backend/worker.ps1. Распакуйте весь ZIP в одну папку и запустите HermesSetup.exe оттуда.");
            var protocol = new Protocol(null, delegate { }, delegate(string p, string[] a) { return false; }) { TelegramMode = true };
            byte[] bytes = new UTF8Encoding(false,true).GetBytes(new JavaScriptSerializer().Serialize(payload)+"\n");
            return await Execute(worker, bytes, protocol, true, CancellationToken.None, limit);
        }
        static void ReadLines(StreamReader reader, Protocol protocol, long[] lastRecord)
        {
            // Bounded NDJSON framing even if the backend misbehaves. Drain after an oversized line.
            var line = new StringBuilder(); bool overflow=false;
            try
            {
                int value;
                while((value=reader.Read())!=-1)
                {
                    if(value=='\n')
                    {
                        Interlocked.Exchange(ref lastRecord[0], DateTime.UtcNow.Ticks);
                        if(overflow) protocol.Invalidate();
                        else protocol.Feed(line.ToString().TrimEnd('\r'));
                        line.Clear(); overflow=false;
                    }
                    else if(line.Length < 32768) line.Append((char)value);
                    else overflow=true;
                }
                // Protocol explicitly requires newline-delimited records, not a truncated last record.
                if(line.Length>0 || overflow) protocol.Invalidate();
            }
            catch { protocol.Invalidate(); }
        }
    }
}
