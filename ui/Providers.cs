using System;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;
namespace HermesSetup {
    // Opens the owner-supplied "get a key" link only when it is a plain https URL.
    // No shell interpretation, no file/http/UNC/command strings, no whitespace or
    // quote characters: the URL is handed to the browser verbatim, nothing else.
    public static class KeyLinkPolicy {
        public static string Clean(string raw) {
            if(String.IsNullOrEmpty(raw)) return "";
            string value=raw.Trim();
            if(value.Length<9 || value.Length>2048) return "";
            if(!value.StartsWith("https://",StringComparison.OrdinalIgnoreCase)) return "";
            foreach(char c in value) if(Char.IsWhiteSpace(c) || Char.IsControl(c)) return "";
            if(value.IndexOf('"')>=0 || value.IndexOf('\'')>=0 || value.IndexOf('`')>=0) return "";
            return value;
        }
    }
    public sealed class ProviderPreset {
        public string id, label, endpoint, note, model, key_url;
        public bool IsCustom { get { return id=="custom"; } }
        public string KeyUrl { get { return KeyLinkPolicy.Clean(key_url); } }
        public override string ToString() { return label; }
    }
    public static class ProviderCatalog {
        public static ProviderPreset[] Load(string path) {
            if(!File.Exists(path)) return new[] {
                new ProviderPreset { id="gwarden",label="GWarden — рекомендуем (7 дней бесплатно)",endpoint="https://gwarden.su/v1",model="glm-5.3",note="Ключ — в личном кабинете gwarden.su или в Telegram-боте @GwardenAIBot (кнопка «Get API»).",key_url="https://gwarden.su/cabinet?ref=1ROA74D4OH" },
                new ProviderPreset { id="custom",label="Другой (точный адрес API)" }
            };
            string text=File.ReadAllText(path);
            if(text.Length>32768) throw new FormatException();
            JsonSyntax.Check(text);
            object parsed=new JavaScriptSerializer().DeserializeObject(text);
            var wrapper=parsed as Dictionary<string,object>;
            if(wrapper!=null) { object inner; if(!wrapper.TryGetValue("providers",out inner)) throw new FormatException(); parsed=inner; }
            var objects=parsed as object[];
            if(objects==null || objects.Length<2 || objects.Length>30) throw new FormatException();
            var entries=new List<ProviderPreset>();
            foreach(object item in objects) {
                var dict=item as Dictionary<string,object>;
                if(dict==null) throw new FormatException();
                var entry=new ProviderPreset();
                object idValue;
                if(dict.TryGetValue("id",out idValue) && idValue is string) entry.id=(string)idValue;
                object label,endpoint,note,model,keyUrl;
                if(dict.TryGetValue("label",out label) && label is string) entry.label=(string)label;
                if(dict.TryGetValue("endpoint",out endpoint) && endpoint is string) entry.endpoint=(string)endpoint;
                if(dict.TryGetValue("note",out note) && note is string) entry.note=(string)note;
                if(dict.TryGetValue("model",out model) && model is string) entry.model=(string)model;
                if(dict.TryGetValue("key_url",out keyUrl) && keyUrl is string) entry.key_url=(string)keyUrl;
                entries.Add(entry);
            }
            var ids=new HashSet<string>(); bool custom=false;
            foreach(var entry in entries) {
                if(entry==null || String.IsNullOrWhiteSpace(entry.id) || String.IsNullOrWhiteSpace(entry.label) || !ids.Add(entry.id)) throw new FormatException();
                if(entry.id=="custom") { if(!String.IsNullOrEmpty(entry.endpoint)) throw new FormatException(); custom=true; }
                else {
                    var request=new Request {endpoint=entry.endpoint,api_key="VALIDATION-ONLY"};
                    if(request.Validate()!=null) throw new FormatException();
                    entry.endpoint=request.endpoint;
                    if(entry.id=="openrouter" && entry.endpoint!="https://openrouter.ai/api/v1") throw new FormatException();
                }
            }
            if(!custom) throw new FormatException();
            return entries.ToArray();
        }
    }
}
