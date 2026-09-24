using System;
using System.Diagnostics;
using System.Drawing;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace HermesSetup
{
    // Four-screen wizard: Choose provider -> Key -> Install -> Done.
    // Presets never expose their API address; only "Другой" reveals the manual
    // endpoint field, normalisation and the confirmation checkbox.
    //
    // Scaling rule: ONE mechanism. The form declares AutoScaleMode.Dpi with an
    // explicit 96-DPI design baseline and lays everything out with AutoSize/Dock,
    // so WinForms autoscale moves geometry as one unit. No code in the wizard
    // multiplies a size by a factor by hand. WinForms deliberately does not scale
    // explicitly-set fonts, so the layout test scales fonts alongside geometry to
    // reproduce DPI text rendering (it calls the same Scale transformation).
    //
    // Layout rule for every screen: one 24 px left/right inset, content starts
    // directly under the heading (AutoSize rows, no spacer/percent rows that
    // could open a vertical hole), navigation buttons live in a docked footer
    // panel so they can never be positioned outside the client area.
    public sealed class InstallerForm : Form
    {
        public const int PageChoose = 0, PageKey = 1, PageInstall = 2, PageDone = 3;
        const int Inset = 24;
        static readonly string[] StageChain = { "Проверяем компьютер", "Скачиваем файлы", "Устанавливаем", "Подключаем ключ" };

        // --- public control surface (production + offline tests) ---
        public readonly TextBox Endpoint = Field("Endpoint", 4096, false, 26);
        public readonly TextBox CustomProvider = Field("CustomProvider", 256, false, 26);
        public readonly TextBox ApiKey = Field("ApiKey", 8192, true, 30);
        public readonly TextBox Model = Field("Model", 256, false, 26);
        // Optional Telegram bot token (masked). Empty or hidden = Telegram skipped.
        public readonly TextBox TelegramToken = Field("TelegramToken", 128, true, 30);
        public readonly Label TelegramNote = new Label { Name = "TelegramNote", AutoSize = true, Tag = "wrap", Visible = false, ForeColor = UiTheme.Text, Font = UiTheme.Font(10f), Margin = new Padding(0, 8, 0, 4) };
        public readonly TextBox Log = new TextBox { Name = "Log", ReadOnly = true, Multiline = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, BackColor = UiTheme.Card, Font = new Font("Consolas", 8.5f) };
        public readonly Label Status = new Label { Name = "Status", Text = "Готово к установке.", AutoSize = true, Tag = "wrap", Font = UiTheme.Font(11.5f), ForeColor = UiTheme.Text, Margin = new Padding(0, 2, 0, 4) };
        public readonly Label EndpointPreview = new Label { Name = "EndpointPreview", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 2, 0, 4) };
        public readonly Label KeyError = new Label { Name = "KeyError", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Error, Margin = new Padding(0, 6, 0, 0) };
        public readonly CheckBox ConfirmEndpoint = new CheckBox { Name = "ConfirmEndpoint", Text = "Подтверждаю: это адрес API; ключ будет отправлен сюда", AutoSize = true, Margin = new Padding(0, 4, 0, 0) };
        public readonly RoundedButton Back = new RoundedButton { Name = "Back", Text = "Назад", Primary = false, Width = 100, Height = 36, Font = UiTheme.Font(10F) };
        public readonly RoundedButton Next = new RoundedButton { Name = "Next", Text = "Далее", Primary = true, Width = 180, Height = 40, Font = UiTheme.Font(11F, FontStyle.Bold) };
        public readonly RoundedButton Launch = new RoundedButton { Name = "Launch", Text = "Открыть Hermes", Primary = true, Width = 220, Height = 44, Font = UiTheme.Font(12F, FontStyle.Bold) };
        // Always a way out of a running install: stops the worker's whole process tree.
        public readonly RoundedButton CancelInstall = new RoundedButton { Name = "CancelInstall", Text = "Отменить", Primary = false, Width = 120, Height = 36, Font = UiTheme.Font(10F) };
        public RoundedButton Install { get { return Next; } }
        // Truthful about resume: backend\checkpoint.ps1 Preserve-IncompleteInstall parks a
        // partial tree aside and reinstalls the same version from scratch; nothing continues mid-stage.
        public const string StopQuestion = "Остановить установку?\r\n\r\nСкачанные файлы не удаляются, но продолжить точно с того же места нельзя: следующая попытка обычно начинает установку заново и снова занимает 10–20 минут.";

        readonly Label stepLabel = new Label { Name = "StepLabel", AutoSize = true, ForeColor = UiTheme.Muted, Font = UiTheme.Font(9.5f) };
        readonly Label keyHint = new Label { Name = "KeyHint", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 6, 0, 0) };
        readonly Label keySafety = new Label { Name = "KeySafety", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 8, 0, 0) };
        readonly Label modelLink = new Label { Name = "ModelLink", Text = "Выбрать другую модель", AutoSize = true, ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(9.5f, FontStyle.Underline), Margin = new Padding(0, 10, 0, 0) };
        readonly TableLayoutPanel modelPanel = new TableLayoutPanel { Name = "ModelPanel", Visible = false, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Dock = DockStyle.Top, ColumnCount = 1, RowCount = 2, BackColor = UiTheme.Background };
        readonly Label telegramLink = new Label { Name = "TelegramLink", Text = "Подключить Телеграм (необязательно)", AutoSize = true, ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(9.5f, FontStyle.Underline), Margin = new Padding(0, 8, 0, 0) };
        readonly TableLayoutPanel telegramPanel = new TableLayoutPanel { Name = "TelegramPanel", Visible = false, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Dock = DockStyle.Top, ColumnCount = 1, RowCount = 2, BackColor = UiTheme.Background };
        bool telegramVisible;
        // Optional backup providers (issue #10): chosen in BackupKeysDialog; the key screen
        // shows only this link, or after ОК a one-line summary that reopens the dialog.
        // It shares the model link's line (wraps below it only when it does not fit).
        readonly Label backupLink = new Label { Name = "BackupLink", Text = "Добавить запасной ключ (необязательно)", AutoSize = true, ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(9.5f, FontStyle.Underline), Margin = Padding.Empty };
        System.Collections.Generic.List<BackupSelection> backups = new System.Collections.Generic.List<BackupSelection>();
        // Done screen: owner approval step (shown only when the bot is connected).
        readonly TableLayoutPanel tgStep = new TableLayoutPanel { Name = "TelegramStep", Visible = false, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Dock = DockStyle.Top, ColumnCount = 1, RowCount = 5, BackColor = UiTheme.Background, Margin = new Padding(0, 6, 0, 0) };
        readonly Label tgStepText = new Label { Name = "TelegramStepText", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Text, Font = UiTheme.Font(10.5f), Margin = new Padding(0, 2, 0, 6) };
        readonly Label tgStatus = new Label { Name = "TelegramStatus", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Font = UiTheme.Font(10f), Margin = new Padding(0, 6, 0, 2) };
        readonly TableLayoutPanel tgRequests = new TableLayoutPanel { Name = "TelegramRequests", AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Dock = DockStyle.Top, ColumnCount = 1, BackColor = UiTheme.Background, Margin = Padding.Empty };
        readonly RoundedButton tgCheck = new RoundedButton { Name = "TelegramCheck", Text = "Проверить", Primary = true, Width = 150, Height = 36, Font = UiTheme.Font(10.5F, FontStyle.Bold), Margin = new Padding(0, 0, 8, 0) };
        readonly RoundedButton tgOpenBot = new RoundedButton { Name = "TelegramOpenBot", Text = "Открыть бота", Primary = false, Width = 150, Height = 36, Font = UiTheme.Font(10F), Margin = Padding.Empty };
        readonly System.Collections.Generic.List<Control> doneExamples = new System.Collections.Generic.List<Control>();
        string tgBotName;
        bool tgBusy;
        readonly TableLayoutPanel customPanel = new TableLayoutPanel { Name = "CustomPanel", Visible = false, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Dock = DockStyle.Top, ColumnCount = 1, RowCount = 7, BackColor = UiTheme.Background, Margin = new Padding(0, 6, 0, 0) };
        readonly Panel logHost = new Panel { Name = "LogHost", Dock = DockStyle.Top, Height = 180, MinimumSize = new Size(0, 180), Visible = false };
        readonly RoundedButton details = new RoundedButton { Name = "Details", Text = "Подробности ▸", Primary = false, Width = 140, Height = 32, Font = UiTheme.Font(9.5f), Anchor = AnchorStyles.Left };
        // After any terminal failure or a stop: «Повторить» plus a way back to change provider/key.
        readonly RoundedButton action = new RoundedButton { Name = "Action", Text = "Повторить", Primary = true, Width = 190, Height = 40, Font = UiTheme.Font(10.5F, FontStyle.Bold), Margin = new Padding(0, 0, 8, 6) };
        readonly RoundedButton change = new RoundedButton { Name = "ChangeProvider", Text = "Изменить провайдера или ключ", Primary = false, Width = 250, Height = 40, Font = UiTheme.Font(10F), Margin = new Padding(0, 0, 0, 6) };
        readonly FlowLayoutPanel actions = new FlowLayoutPanel { Name = "FailureActions", AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = true, Dock = DockStyle.Top, BackColor = UiTheme.Background, Margin = new Padding(0, 4, 0, 0), Visible = false, Tag = "wrap" };
        int changeTarget = PageChoose;
        readonly ProgressBar bar = new ProgressBar { Name = "Bar", Dock = DockStyle.Top, Height = 8, Style = ProgressBarStyle.Continuous, Maximum = 100 };
        readonly Label elapsed = new Label { AutoSize = true, ForeColor = UiTheme.Muted, Margin = new Padding(0, 2, 0, 2) };
        readonly Label stageStep = new Label { Name = "StageStep", Text = "", AutoSize = true, ForeColor = UiTheme.Muted, Font = UiTheme.Font(8.5f), Anchor = AnchorStyles.Right, Margin = new Padding(0, 2, 0, 2) };
        // The four human phrases the 16 upstream stages collapse into. Each phrase
        // is its own label so the current one can be bold/accent (a single Label
        // cannot style part of its text).
        readonly Label[] chainLabels = new Label[StageChain.Length];
        readonly TableLayoutPanel chain = new TableLayoutPanel { Name = "StepChain", Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = StageChain.Length, BackColor = UiTheme.Background, Margin = new Padding(0, 4, 0, 0) };
        readonly Label progressNote = new Label { Name = "ProgressNote", Text = "10–20 минут. Можно отойти: окно само дойдёт до конца.", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Text, Margin = new Padding(0, 8, 0, 0) };
        readonly Label silence = new Label { Name = "Silence", Text = "ещё идёт, так и должно быть", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Visible = false, Margin = new Padding(0, 4, 0, 0) };
        readonly Label statusHint = new Label { Name = "StatusHint", Text = "Проверка отправит короткий запрос модели и может расходовать небольшую квоту подписки.", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 8, 0, 0) };
        readonly Label copyHint = new Label { Name = "CopyHint", Text = "Клик по строке копирует её.", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 4, 0, 0) };

        readonly Panel[] pages = new Panel[4];
        readonly ProviderCard[] cards = new ProviderCard[0];
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer { Interval = 1000 };
        int selected = 0;
        bool catalogValid = true;
        bool running;
        CancellationTokenSource stop;
        bool closeAfterStop;
        bool modelVisible;
        bool keyShown;
        bool manualEndpointShown;
        DateTime started;
        DateTime lastProgress;
        Outcome result;

        public int PageIndex { get; private set; }
        public bool IsRunning { get { return running; } }
        bool Stopping { get { return stop != null && stop.IsCancellationRequested; } }
        // What the failure/stopped screen offers (tests read it; Control.Visible is false while hidden).
        public string[] FailureActions { get { return failureShown ? new[] { action.Text, change.Text } : new string[0]; } }
        bool failureShown;
        public int ProviderCount { get { return cards.Length; } }
        public int SelectedProvider { get { return selected; } }
        public ProviderPreset SelectedPreset { get { return cards.Length > 0 ? cards[selected].Preset : null; } }
        public bool IsCustomSelected { get { return SelectedPreset != null && SelectedPreset.IsCustom; } }
        public ProviderPreset PresetAt(int index) { return (index >= 0 && index < cards.Length) ? cards[index].Preset : null; }
        // Reflects the wizard's intent independently of whether the window is shown
        // (Control.Visible returns effective visibility, false while the form is hidden).
        public bool ManualEndpointShown { get { return manualEndpointShown; } }
        public void ToggleModelField()
        {
            modelVisible = !modelVisible;
            modelPanel.Visible = modelVisible;
            modelLink.Text = modelVisible ? "Скрыть поле модели" : "Выбрать другую модель";
        }
        public bool TelegramShown { get { return telegramVisible; } }
        public void ToggleTelegramField()
        {
            telegramVisible = !telegramVisible;
            telegramPanel.Visible = telegramVisible;
            telegramLink.Text = telegramVisible ? "Не подключать Телеграм" : "Подключить Телеграм (необязательно)";
            if (!telegramVisible) TelegramToken.Clear();
            UpdateWraps();
        }

        // --- backup providers (issue #10) ---------------------------------------
        public int BackupCount { get { return backups.Count; } }
        public string BackupSummary { get { return backupLink.Text; } }
        string PrimaryEndpoint
        {
            get
            {
                ProviderPreset primary = SelectedPreset;
                if (primary == null) return "";
                return primary.IsCustom ? Request.NormalizeEndpoint(Endpoint.Text) : primary.endpoint;
            }
        }
        // Presets a backup may use: never "Другой", never the current primary.
        public System.Collections.Generic.List<ProviderPreset> BackupCandidates()
        {
            var list = new System.Collections.Generic.List<ProviderPreset>();
            ProviderPreset primary = SelectedPreset;
            string primaryEndpoint = PrimaryEndpoint;
            foreach (var card in cards)
            {
                ProviderPreset p = card.Preset;
                if (p.IsCustom || p == primary || String.IsNullOrEmpty(p.endpoint)) continue;
                if (String.Equals(p.endpoint, primaryEndpoint, StringComparison.OrdinalIgnoreCase)) continue;
                list.Add(p);
            }
            return list;
        }
        // Production opens it modally; tests drive the same object without showing it.
        public BackupKeysDialog CreateBackupDialog()
        {
            return new BackupKeysDialog(BackupCandidates(), backups, PrimaryEndpoint);
        }
        public void ApplyBackupDialog(BackupKeysDialog dialog)
        {
            if (dialog == null || dialog.Result == null) return;
            backups = new System.Collections.Generic.List<BackupSelection>(dialog.Result);
            RefreshBackupLink();
        }
        void OpenBackupDialog()
        {
            if (running) return;
            using (var dialog = CreateBackupDialog())
                if (dialog.ShowDialog(this) == DialogResult.OK) ApplyBackupDialog(dialog);
        }
        public void ClearBackups()
        {
            foreach (var b in backups) b.Key = null;
            backups.Clear();
            RefreshBackupLink();
        }
        // A backup can never become the primary: drop any that now are, then redraw the link.
        void RefreshBackupLink()
        {
            var allowed = BackupCandidates();
            backups.RemoveAll(delegate(BackupSelection b) { return b == null || !allowed.Contains(b.Preset); });
            var names = new System.Collections.Generic.List<string>();
            foreach (var b in backups) names.Add(BackupKeysDialog.ShortName(b.Preset));
            backupLink.Text = names.Count == 0 ? "Добавить запасной ключ (необязательно)" : "Запасные: " + String.Join(", ", names.ToArray()) + " · изменить";
            backupLink.Visible = allowed.Count > 0;
            UpdateWraps();
        }

        public InstallerForm()
        {
            Text = "Hermes — установка для подписчиков";
            Font = UiTheme.Font(10F);
            AutoScaleMode = AutoScaleMode.Dpi;
            AutoScaleDimensions = new SizeF(96F, 96F);
            ClientSize = new Size(680, 600);
            // The compact card (one note line) plus the top-aligned content rows fit
            // at 560x500 with room to spare, so this is the smallest window the
            // wizard is allowed to be shrunk to; nothing is ever clipped.
            MinimumSize = new Size(560, 500);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = UiTheme.Background;

            ProviderPreset[] presets;
            try { presets = ProviderCatalog.Load(System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "providers.json")); }
            catch { presets = new ProviderPreset[0]; catalogValid = false; }
            cards = new ProviderCard[presets.Length];
            for (int i = 0; i < presets.Length; i++) cards[i] = new ProviderCard(presets[i], i == 0);

            var shell = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, BackColor = UiTheme.Background };
            shell.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            shell.RowStyles.Add(new RowStyle(SizeType.Absolute, 54));
            shell.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            shell.RowStyles.Add(new RowStyle(SizeType.Absolute, 66));

            var header = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = UiTheme.Background, Padding = new Padding(Inset, 0, Inset, 0) };
            header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            var headerTitle = new Label { Text = "Установка Hermes", Font = UiTheme.Font(18F, FontStyle.Bold), ForeColor = UiTheme.Text, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(0, 8, 0, 0) };
            stepLabel.Anchor = AnchorStyles.Right; stepLabel.Margin = new Padding(0, 14, 0, 0);
            header.Controls.Add(headerTitle, 0, 0);
            header.Controls.Add(stepLabel, 1, 0);

            var content = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 1, BackColor = UiTheme.Background, Padding = new Padding(Inset, 0, Inset, 8) };
            content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

            pages[PageChoose] = BuildChoosePage();
            pages[PageKey] = BuildKeyPage();
            pages[PageInstall] = BuildInstallPage();
            pages[PageDone] = BuildDonePage();
            string[] pageNames = { "PageChoose", "PageKey", "PageInstall", "PageDone" };
            for (int i = 0; i < pages.Length; i++) { pages[i].Name = pageNames[i]; pages[i].Dock = DockStyle.Fill; pages[i].Visible = false; content.Controls.Add(pages[i]); }
            content.Resize += delegate { UpdateWraps(); };

            // Footer hosted by a layout panel: the buttons are placed by the layout
            // engine, never by coordinate math, so no scaling factor can push them
            // outside the client area.
            var footer = new TableLayoutPanel { Name = "Footer", Dock = DockStyle.Fill, ColumnCount = 5, RowCount = 1, BackColor = UiTheme.Background, Padding = new Padding(Inset, 8, Inset, 8) };
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            Back.Anchor = AnchorStyles.Left; Back.Margin = new Padding(0, 0, 8, 0);
            Next.Anchor = AnchorStyles.Right; Next.Margin = new Padding(8, 0, 0, 0);
            Launch.Anchor = AnchorStyles.Right; Launch.Margin = new Padding(8, 0, 0, 0);
            CancelInstall.Anchor = AnchorStyles.Right; CancelInstall.Margin = new Padding(8, 0, 0, 0);
            footer.Controls.Add(Back, 0, 0);
            footer.Controls.Add(Next, 2, 0);
            footer.Controls.Add(Launch, 3, 0);
            footer.Controls.Add(CancelInstall, 4, 0);
            Back.Click += delegate { if (PageIndex == PageKey) GoTo(PageChoose); };
            Next.Click += delegate { OnNext(); };
            CancelInstall.Click += delegate { if (running && !Stopping && AskStop()) StopInstall(); };
            Launch.Click += delegate
            {
                try { LaunchPolicy.Start(result); Status.ForeColor = UiTheme.Success; Status.Text = "Hermes запущен. Дождитесь появления окна Desktop."; }
                catch { Status.Text = "Не удалось открыть Hermes. Повторите проверку установки; если ошибка повторяется, обратитесь в поддержку."; Launch.Enabled = false; }
            };

            shell.Controls.Add(header, 0, 0);
            shell.Controls.Add(content, 0, 1);
            shell.Controls.Add(footer, 0, 2);
            Controls.Add(shell);

            AcceptButton = Next;
            Status.TextChanged += delegate { UpdateProgressFromStatus(); };
            timer.Tick += delegate
            {
                TimeSpan span = DateTime.UtcNow - started;
                if (span < TimeSpan.Zero) span = TimeSpan.Zero;
                elapsed.Text = "Прошло " + span.ToString(@"m\:ss");
                if (running && DateTime.UtcNow - lastProgress >= TimeSpan.FromSeconds(20)) silence.Visible = true;
            };
            // Closing while installing = the same question as «Отменить»; after «Да» the window
            // closes as soon as the process tree is gone. Windows shutdown is never blocked: the
            // job object kills the tree when this process ends.
            FormClosing += delegate(object sender, FormClosingEventArgs e)
            {
                if (!running || e.CloseReason == CloseReason.WindowsShutDown || e.CloseReason == CloseReason.TaskManagerClosing) return;
                e.Cancel = true;
                if (Stopping || AskStop()) { closeAfterStop = true; StopInstall(); }
            };
            FormClosed += delegate { timer.Dispose(); ApiKey.Clear(); TelegramToken.Clear(); ClearBackups(); };

            SelectProvider(0);
            GoTo(PageChoose);
        }

        // --- the single scaling mechanism -------------------------------------
        // The form declares AutoScaleMode.Dpi against a 96-DPI design baseline and
        // lays everything out with AutoSize/Dock, so WinForms scaling (runtime DPI
        // and the layout test's Scale call) moves geometry as one unit. No code in
        // the wizard multiplies a size by a factor by hand. WinForms deliberately
        // does not scale explicitly-set fonts, so the layout test scales fonts
        // alongside geometry to reproduce DPI text rendering.

        static TextBox Field(string name, int maxLength, bool secret, int height)
        {
            return new TextBox
            {
                Name = name, MaxLength = maxLength, Dock = DockStyle.Fill, BorderStyle = BorderStyle.FixedSingle,
                Font = UiTheme.Font(11F), AutoSize = false, UseSystemPasswordChar = secret,
                MinimumSize = new Size(0, height), MaximumSize = new Size(0, height)
            };
        }
        static Label Head(string text)
        {
            return new Label { Text = text, AutoSize = true, Font = UiTheme.Font(18F, FontStyle.Bold), ForeColor = UiTheme.Text, Margin = new Padding(0, 2, 0, 6) };
        }
        static Label Body(string text)
        {
            return new Label { Text = text, AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 0, 0, 10) };
        }
        // Column of AutoSize rows plus one trailing Percent(100) filler row that
        // absorbs all leftover height. Without that filler WinForms spreads the
        // surplus across the content rows, which opened an empty gap (e.g. between
        // the key field and its hint). With it, every content row keeps its
        // preferred height and the blank space stays below the content, so nothing
        // can drift toward the footer.
        static TableLayoutPanel Stack(int rows)
        {
            int content = Math.Max(1, rows);
            var t = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = content + 1, AutoSize = false, BackColor = UiTheme.Background };
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            for (int i = 0; i < content; i++) t.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            t.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            return t;
        }

        Panel BuildChoosePage()
        {
            int rows = 2 + Math.Max(1, cards.Length) + 1;
            var page = new Panel { BackColor = UiTheme.Background };
            var root = Stack(rows);
            int r = 0;
            root.Controls.Add(Head("Выберите провайдера"), 0, r++);
            root.Controls.Add(Body("Выберите сервис и возьмите ключ. Ключ — это пароль сервиса."), 0, r++);
            if (cards.Length == 0)
                root.Controls.Add(new Label { Text = "Файл providers.json повреждён. Распакуйте пакет заново.", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Error }, 0, r++);
            for (int i = 0; i < cards.Length; i++)
            {
                int index = i;
                cards[i].Dock = DockStyle.Fill;
                cards[i].Activated += delegate { SelectProvider(index); };
                cards[i].KeyRequested += delegate { OpenKeyLink(cards[index].Preset); };
                root.Controls.Add(cards[i], 0, r++);
            }

            customPanel.ColumnStyles.Clear(); customPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            customPanel.Controls.Add(new Label { Text = "Точный адрес API", AutoSize = true, ForeColor = UiTheme.Text, Margin = new Padding(0, 4, 0, 4) }, 0, 0);
            customPanel.Controls.Add(Endpoint, 0, 1);
            customPanel.Controls.Add(EndpointPreview, 0, 2);
            customPanel.Controls.Add(new Label { Text = "Например: https://api.example.com/v1 — адрес из вашей подписки", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 2, 0, 6) }, 0, 3);
            customPanel.Controls.Add(new Label { Text = "Название поставщика (необязательно)", AutoSize = true, ForeColor = UiTheme.Text, Margin = new Padding(0, 4, 0, 4) }, 0, 4);
            customPanel.Controls.Add(CustomProvider, 0, 5);
            customPanel.Controls.Add(ConfirmEndpoint, 0, 6);
            root.Controls.Add(customPanel, 0, r++);

            Endpoint.TextChanged += delegate
            {
                ConfirmEndpoint.Checked = false;
                var preview = new Request { endpoint = Endpoint.Text, api_key = "PREVIEW-ONLY" };
                string problem = preview.Validate();
                EndpointPreview.Text = problem == null ? "Будет использован адрес: " + preview.endpoint : problem;
                ConfirmEndpoint.Enabled = problem == null;
                RefreshButtons();
            };
            ConfirmEndpoint.CheckedChanged += delegate { RefreshButtons(); };
            page.Controls.Add(root);
            return page;
        }

        Panel BuildKeyPage()
        {
            var page = new Panel { BackColor = UiTheme.Background };
            var root = Stack(10);
            root.Controls.Add(Head("Вставьте ключ"), 0, 0);
            root.Controls.Add(new Label { Text = "Ключ — это пароль сервиса", AutoSize = true, ForeColor = UiTheme.Muted, Margin = new Padding(0, 6, 0, 4) }, 0, 1);

            var keyRow = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 3, RowCount = 1, BackColor = UiTheme.Background, Margin = new Padding(0, 0, 0, 2) };
            keyRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            keyRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            keyRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            var paste = new RoundedButton { Text = "Вставить из буфера", Primary = false, Width = 168, Height = 32, Font = UiTheme.Font(9.5f), Margin = new Padding(8, 0, 0, 0) };
            var show = new RoundedButton { Text = "Показать", Primary = false, Width = 104, Height = 32, Font = UiTheme.Font(9.5f), Margin = new Padding(8, 0, 0, 0) };
            paste.Click += delegate
            {
                try { if (Clipboard.ContainsText()) { ApiKey.Text = Clipboard.GetText().Trim(); ApiKey.SelectionStart = ApiKey.TextLength; KeyError.Text = ""; } }
                catch { KeyError.Text = "Не удалось прочитать буфер обмена. Вставьте ключ вручную (Ctrl+V)."; }
            };
            show.Click += delegate
            {
                keyShown = !keyShown;
                ApiKey.UseSystemPasswordChar = !keyShown;
                ApiKey.PasswordChar = '\0';
                show.Text = keyShown ? "Скрыть" : "Показать";
            };
            keyRow.Controls.Add(ApiKey, 0, 0);
            keyRow.Controls.Add(paste, 1, 0);
            keyRow.Controls.Add(show, 2, 0);
            root.Controls.Add(keyRow, 0, 2);
            // note right under the key field, then the key-safety line under it.
            root.Controls.Add(keyHint, 0, 3);
            keySafety.Text = "Ключ хранится только на этом компьютере и не попадает в журнал. Никому его не отправляйте.";
            root.Controls.Add(keySafety, 0, 4);
            root.Controls.Add(KeyError, 0, 5);
            // Model link and backup link on one wrapping line: no extra row in the common case.
            var links = new FlowLayoutPanel { Name = "KeyLinks", AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = true, Dock = DockStyle.Top, BackColor = UiTheme.Background, Margin = modelLink.Margin, Tag = "wrap" };
            modelLink.Margin = new Padding(0, 0, 16, 0);
            links.Controls.Add(modelLink);
            links.Controls.Add(backupLink);
            root.Controls.Add(links, 0, 6);

            modelPanel.ColumnStyles.Clear(); modelPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            modelPanel.Controls.Add(new Label { Text = "Идентификатор модели", AutoSize = true, ForeColor = UiTheme.Text, Margin = new Padding(0, 6, 0, 4) }, 0, 0);
            modelPanel.Controls.Add(Model, 0, 1);
            root.Controls.Add(modelPanel, 0, 7);

            // Optional Telegram bot: collapsed by default so the key screen is unchanged.
            telegramPanel.ColumnStyles.Clear(); telegramPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            telegramPanel.Controls.Add(new Label { Text = "Создайте бота у @BotFather → /newbot → скопируйте токен", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 6, 0, 4) }, 0, 0);
            var tgRow = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = 1, BackColor = UiTheme.Background, Margin = Padding.Empty };
            tgRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            tgRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            var botFather = new RoundedButton { Name = "BotFather", Text = "Открыть @BotFather", Primary = false, Width = 168, Height = 32, Font = UiTheme.Font(9.5f), Margin = new Padding(8, 0, 0, 0) };
            botFather.Click += delegate
            {
                try { Process.Start(new ProcessStartInfo("https://t.me/BotFather") { UseShellExecute = true }); }
                catch { MessageBox.Show(this, "Не удалось открыть ссылку. Откройте в Телеграм бота @BotFather вручную.", "Hermes", MessageBoxButtons.OK, MessageBoxIcon.Information); }
            };
            tgRow.Controls.Add(TelegramToken, 0, 0);
            tgRow.Controls.Add(botFather, 1, 0);
            telegramPanel.Controls.Add(tgRow, 0, 1);
            root.Controls.Add(telegramLink, 0, 8);
            root.Controls.Add(telegramPanel, 0, 9);

            modelLink.Click += delegate { ToggleModelField(); };
            telegramLink.Click += delegate { ToggleTelegramField(); };
            backupLink.Click += delegate { OpenBackupDialog(); };
            page.Controls.Add(root);
            return page;
        }

        Panel BuildInstallPage()
        {
            // No duplicate "Установка" heading: the window header already says it.
            var page = new Panel { BackColor = UiTheme.Background };
            var root = Stack(11);
            root.Controls.Add(Status, 0, 0);
            var meta = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = 1, BackColor = UiTheme.Background, Margin = Padding.Empty };
            meta.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            meta.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            elapsed.Anchor = AnchorStyles.Left;
            meta.Controls.Add(elapsed, 0, 0);
            meta.Controls.Add(stageStep, 1, 0);
            root.Controls.Add(meta, 0, 1);
            root.Controls.Add(bar, 0, 2);
            root.RowStyles[2] = new RowStyle(SizeType.Absolute, 12);
            // Build the four-phrase chain: phrase, arrow, phrase, ... so the current
            // phrase can be emphasised on its own.
            chain.ColumnStyles.Clear();
            // One phrase per row: a single-row chain overflowed at 560 px (sandbox
            // layout run 2026-09-22), and a vertical list reads as a checklist.
            chain.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            for (int i = 0; i < chainLabels.Length; i++)
            {
                chainLabels[i] = new Label { Name = "Chain" + i, Text = StageChain[i], AutoSize = true, ForeColor = UiTheme.Muted, Font = UiTheme.Font(9f), Margin = new Padding(0, 2, 0, 2), Anchor = AnchorStyles.Left };
                chain.Controls.Add(chainLabels[i], 0, i);
            }
            elapsed.Text = "Прошло 0:00";
            root.Controls.Add(chain, 0, 3);
            root.Controls.Add(progressNote, 0, 4);
            root.Controls.Add(silence, 0, 5);
            root.Controls.Add(details, 0, 6);
            actions.Controls.Add(action);
            actions.Controls.Add(change);
            root.Controls.Add(actions, 0, 7);
            root.Controls.Add(statusHint, 0, 8);
            logHost.Controls.Clear();
            logHost.Controls.Add(Log);
            // The journal takes whatever height is left (min 60 px) instead of a fixed
            // 180 px: with the failure actions + hint shown, 180 px pushed it out of the
            // window at 680x560 (sandbox layout run, v0.1.2).
            logHost.Dock = DockStyle.Fill; logHost.MinimumSize = new Size(0, 60);
            root.Controls.Add(logHost, 0, 9);
            root.RowStyles[9] = new RowStyle(SizeType.Percent, 100);
            root.RowStyles[root.RowStyles.Count - 1] = new RowStyle(SizeType.Absolute, 0);
            root.Controls.Add(new Label { AutoSize = false, Height = 0, Margin = Padding.Empty }, 0, 10);
            details.Click += delegate
            {
                logHost.Visible = !logHost.Visible;
                details.Text = logHost.Visible ? "Подробности ▾" : "Подробности ▸";
            };
            action.Click += delegate { HideFailure(); StartInstall(); };
            change.Click += delegate { ChangeProviderOrKey(); };
            page.Controls.Add(root);
            return page;
        }

        Panel BuildDonePage()
        {
            var page = new Panel { BackColor = UiTheme.Background };
            var root = Stack(10);
            root.Controls.Add(Head("Готово — Hermes установлен"), 0, 0);
            root.Controls.Add(Body("Провайдер подключён, ответ модели проверен. Нажмите «Открыть Hermes»."), 0, 1);
            var examplesHead = new Label { Text = "Пишите обычными словами. Примеры первых запросов:", AutoSize = true, Font = UiTheme.Font(11F, FontStyle.Bold), ForeColor = UiTheme.Text, Margin = new Padding(0, 8, 0, 6) };
            doneExamples.Add(examplesHead);
            doneExamples.Add(Example("Привет! Что ты умеешь и с чего начнём?"));
            doneExamples.Add(Example("Поищи в интернете, что нового вышло по <тема>, и дай ссылки"));
            doneExamples.Add(Example("Каждое утро в 9 присылай сводку по <тема>"));
            doneExamples.Add(Example("Объясни простыми словами, что такое проценты по вкладу"));
            doneExamples.Add(copyHint);
            for (int i = 0; i < doneExamples.Count; i++) root.Controls.Add(doneExamples[i], 0, 2 + i);
            // Backend's fixed Telegram outcome when the bot is NOT connected (why skipped).
            root.Controls.Add(TelegramNote, 0, 8);
            // Owner approval step when the bot IS connected: replaces the examples.
            tgStep.ColumnStyles.Clear(); tgStep.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            tgStep.Controls.Add(new Label { Text = "Подключите Телеграм к себе", AutoSize = true, Font = UiTheme.Font(11F, FontStyle.Bold), ForeColor = UiTheme.Text, Margin = new Padding(0, 6, 0, 4) }, 0, 0);
            tgStep.Controls.Add(tgStepText, 0, 1);
            var tgButtons = new FlowLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = true, BackColor = UiTheme.Background, Margin = Padding.Empty };
            tgButtons.Controls.Add(tgCheck); tgButtons.Controls.Add(tgOpenBot);
            tgStep.Controls.Add(tgButtons, 0, 2);
            tgStep.Controls.Add(tgStatus, 0, 3);
            tgRequests.ColumnStyles.Clear(); tgRequests.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            tgStep.Controls.Add(tgRequests, 0, 4);
            tgCheck.Click += delegate { TelegramCheck(); };
            tgOpenBot.Click += delegate
            {
                if (String.IsNullOrEmpty(tgBotName)) return;
                try { Process.Start(new ProcessStartInfo("https://t.me/" + tgBotName) { UseShellExecute = true }); }
                catch { tgStatus.Text = "Не удалось открыть ссылку. Найдите бота @" + tgBotName + " в Телеграм вручную."; }
            };
            root.Controls.Add(tgStep, 0, 9);
            page.Controls.Add(root);
            return page;
        }

        // A click on an example copies it: the user's very first Hermes request is
        // then one Ctrl+V away instead of a retyped sentence.
        Label Example(string text)
        {
            var label = new Label { Text = "• " + text, AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(10.5f), Margin = new Padding(0, 2, 0, 2) };
            label.Click += delegate
            {
                try { Clipboard.SetText(text); copyHint.Text = "Скопировано в буфер обмена."; }
                catch { copyHint.Text = "Не удалось скопировать. Выделите строку вручную."; }
            };
            return label;
        }

        void OpenKeyLink(ProviderPreset preset)
        {
            string url = preset == null ? "" : preset.KeyUrl;
            if (url.Length == 0) return;
            try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
            catch { MessageBox.Show(this, "Не удалось открыть ссылку в браузере. Откройте её вручную.", "Hermes", MessageBoxButtons.OK, MessageBoxIcon.Information); }
        }

        public void SelectProvider(int index)
        {
            if (cards.Length == 0) { RefreshButtons(); return; }
            if (index < 0 || index >= cards.Length) index = 0;
            selected = index;
            for (int i = 0; i < cards.Length; i++) cards[i].Selected = (i == index);
            bool custom = cards[index].Preset.IsCustom;
            manualEndpointShown = custom;
            customPanel.Visible = custom;
            if (custom) { EndpointPreview.Text = ""; ConfirmEndpoint.Checked = false; ConfirmEndpoint.Enabled = false; }
            string note = cards[index].Preset.note;
            keyHint.Text = String.IsNullOrEmpty(note) ? "" : note;
            RefreshBackupLink();
            RefreshButtons();
        }

        public bool CanChoose
        {
            get
            {
                if (!catalogValid || cards.Length == 0) return false;
                if (!IsCustomSelected) return true;
                var probe = new Request { endpoint = Endpoint.Text, api_key = "VALIDATION-ONLY" };
                return probe.Validate() == null && ConfirmEndpoint.Checked;
            }
        }

        public Request BuildRequest()
        {
            var request = new Request();
            request.api_key = ApiKey.Text;
            ProviderPreset preset = SelectedPreset;
            if (preset != null && !preset.IsCustom)
            {
                request.endpoint = preset.endpoint;
                request.model = modelVisible ? Model.Text.Trim() : (preset.model == null ? "" : preset.model);
                request.provider_name = preset.label;
            }
            else
            {
                request.endpoint = Endpoint.Text;
                request.model = modelVisible ? Model.Text.Trim() : "";
                request.provider_name = CustomProvider.Text.Trim();
            }
            request.telegram_bot_token = telegramVisible && TelegramToken.TextLength > 0 ? TelegramToken.Text : null;
            var entries = new System.Collections.Generic.List<FallbackEntry>();
            foreach (var b in backups)
                if (b != null && b.Preset != null && !String.IsNullOrEmpty(b.Key))
                    entries.Add(new FallbackEntry { provider_id = b.Preset.id, endpoint = b.Preset.endpoint, model = b.Preset.model ?? "", api_key = b.Key });
            request.fallbacks = entries.Count > 0 ? entries : null;
            return request;
        }

        public void GoTo(int page)
        {
            if (page < 0 || page > PageDone) return;
            PageIndex = page;
            // A custom primary address may have changed on screen 1.
            if (page == PageKey) RefreshBackupLink();
            for (int i = 0; i < pages.Length; i++) pages[i].Visible = (i == page);
            stepLabel.Text = "Экран " + (page + 1) + " из 4";
            UpdateWraps();
            RefreshButtons();
        }

        // AutoSize wrapping labels need an explicit MaximumSize; recompute it from
        // the live page width so DPI scaling never pushes text outside the window.
        void UpdateWraps()
        {
            if (pages == null || pages[PageIndex] == null) return;
            int width = Math.Max(160, pages[PageIndex].ClientSize.Width);
            ApplyWrap(pages[PageIndex], width);
            if (cards == null) return;
            for (int i = 0; i < cards.Length; i++) if (cards[i] != null) cards[i].WrapTo(cards[i].Width);
        }
        static void ApplyWrap(Control root, int width)
        {
            foreach (Control c in root.Controls)
            {
                var label = c as Label;
                if (label != null && Object.Equals(label.Tag, "wrap")) label.MaximumSize = new Size(width, 0);
                // A wrapping link line needs the same bound or it grows sideways instead.
                if (c is FlowLayoutPanel && Object.Equals(c.Tag, "wrap")) c.MaximumSize = new Size(width, 0);
                ApplyWrap(c, width);
            }
        }

        void RefreshButtons()
        {
            Back.Visible = (PageIndex == PageKey) && !running;
            Next.Visible = (PageIndex == PageChoose || PageIndex == PageKey);
            // "Открыть Hermes" is always present on the last screen but only
            // becomes active after a verified success.
            Launch.Visible = (PageIndex == PageDone);
            Launch.Enabled = !running && result != null && result.Success;
            Next.Text = PageIndex == PageChoose ? "Далее" : "Установить";
            Next.Enabled = !running && catalogValid && (PageIndex == PageChoose ? CanChoose : true);
            CancelInstall.Visible = running && PageIndex == PageInstall;
            CancelInstall.Enabled = !Stopping;
        }

        void OnNext()
        {
            if (running) return;
            if (PageIndex == PageChoose) { if (CanChoose) GoTo(PageKey); return; }
            if (PageIndex != PageKey) return;
            var request = BuildRequest();
            string validation = request.Validate();
            if (validation != null) { KeyError.Text = validation; return; }
            StartInstall();
        }

        void StartInstall()
        {
            if (running) return;
            var request = BuildRequest();
            string validation = request.Validate();
            if (validation != null) { KeyError.Text = validation; GoTo(PageKey); return; }
            running = true; result = null; KeyError.Text = ""; HideFailure(); statusHint.Visible = false;
            stop = new CancellationTokenSource(); closeAfterStop = false;
            GoTo(PageInstall);
            Next.Enabled = false; Back.Visible = false;
            Status.ForeColor = UiTheme.Text;
            bar.Style = ProgressBarStyle.Marquee; bar.Value = 0;
            stageStep.Text = "";
            silence.Visible = false;
            Status.Text = "Запускаю установку. Дождитесь результата; системные запросы разрешений появятся отдельно.";
            started = DateTime.UtcNow; lastProgress = started; elapsed.Text = "Прошло 0:00"; timer.Start();
            LogAppend("Запуск установки: " + (SelectedPreset == null ? "свой поставщик" : SelectedPreset.label));
            RunWorker(request);
        }

        async void RunWorker(Request request)
        {
            bool telegramRequested = !String.IsNullOrEmpty(request.telegram_bot_token);
            try
            {
                result = await WorkerClient.RunAsync(System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "backend", "worker.ps1"), request,
                    delegate(string message) { if (!IsDisposed) BeginInvoke((Action)delegate { OnProgress(message); }); },
                    delegate(int index, int total, string stage) { if (!IsDisposed) BeginInvoke((Action)delegate { OnStage(index, total, stage); }); },
                    stop.Token);
                if (result.Success)
                {
                    Status.Text = result.Message;
                    Status.ForeColor = UiTheme.Success;
                    LogAppend("Установка подтверждена.");
                    ApiKey.Clear(); TelegramToken.Clear();
                    bool connected = telegramRequested && result.TelegramBot != null;
                    // Not connected: show why (backend's fixed text). Connected: the approval step.
                    TelegramNote.Text = telegramRequested && !connected ? result.Message : "";
                    TelegramNote.Visible = telegramRequested && !connected;
                    if (connected) ShowTelegramStep(result.TelegramBot);
                    GoTo(PageDone);
                }
                else ShowFailure(result);
            }
            catch
            {
                ShowFailure(Outcome.Failure("INTERNAL", "Не удалось завершить проверку. Обратитесь в поддержку; установка не подтверждена."));
            }
            finally
            {
                request.ClearSecrets(); running = false; timer.Stop(); silence.Visible = false;
                if (stop != null) { stop.Dispose(); stop = null; }
                // Success already moved to PageDone while running was true; refresh
                // on any page so «Открыть Hermes» becomes active.
                if (PageIndex == PageInstall) statusHint.Visible = !failureShown;
                RefreshButtons();
                if (closeAfterStop && !IsDisposed) BeginInvoke((Action)Close);
            }
        }

        bool AskStop()
        {
            return MessageBox.Show(this, StopQuestion, "Остановить установку", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes;
        }
        // Kills the worker's whole process tree (WorkerClient); RunWorker then shows the stopped screen.
        public void StopInstall()
        {
            if (!running || stop == null || Stopping) return;
            stop.Cancel();
            Status.ForeColor = UiTheme.Text;
            Status.Text = "Останавливаю установку…";
            LogAppend("Остановка по запросу пользователя.");
            RefreshButtons();
        }

        // Every terminal failure and a stop offer the same two ways forward: retry as is, or
        // go back and change provider/key. AUTH is decided by the protocol code, never by text.
        public void ShowFailure(Outcome outcome)
        {
            bool stopped = outcome.Cancelled;
            Status.Text = outcome.Message ?? "";
            Status.ForeColor = stopped ? UiTheme.Text : UiTheme.Error;
            LogAppend(stopped ? "Установка остановлена." : "Ошибка: " + outcome.Message);
            if (bar.Style == ProgressBarStyle.Marquee) { bar.Style = ProgressBarStyle.Continuous; bar.Value = 0; }
            bool auth = outcome.Code == "AUTH";
            change.Text = auth ? "Изменить ключ" : "Изменить провайдера или ключ";
            changeTarget = auth ? PageKey : PageChoose;
            failureShown = true;
            actions.Visible = true;
            progressNote.Visible = false;
            statusHint.Visible = false;
            UpdateWraps();
        }
        void HideFailure()
        {
            failureShown = false;
            actions.Visible = false;
            progressNote.Visible = true;
        }
        // Back to the provider screen (the key screen for AUTH) with every choice kept. The
        // primary key is cleared: it is the thing being changed, and it is not kept on screen.
        public void ChangeProviderOrKey()
        {
            if (running) return;
            HideFailure();
            statusHint.Visible = true;
            ApiKey.Clear();
            GoTo(changeTarget);
            if (changeTarget == PageKey) ApiKey.Focus();
        }

        // --- Telegram owner approval (Done screen) -------------------------------
        // Never approves anyone without the owner's explicit «Да, это я» click.
        public void ShowTelegramStep(string bot)
        {
            tgBotName = bot == "-" ? null : bot;
            string who = tgBotName == null ? "вашему боту" : "вашему боту @" + tgBotName;
            tgStepText.Text = "1. Напишите " + who + " в Телеграм любое сообщение. Он может ответить служебным текстом на английском — это нормально.\r\n2. Нажмите «Проверить» и подтвердите, что это вы.";
            tgOpenBot.Visible = tgBotName != null;
            tgStatus.Text = ""; tgStatus.ForeColor = UiTheme.Muted;
            tgRequests.Controls.Clear(); tgRequests.RowStyles.Clear(); tgRequests.RowCount = 0;
            foreach (Control c in doneExamples) c.Visible = false;
            tgStep.Visible = true;
            UpdateWraps();
        }

        string WorkerPath { get { return System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "backend", "worker.ps1"); } }

        void SetTelegramBusy(bool busy, string text)
        {
            tgBusy = busy;
            tgCheck.Enabled = !busy;
            foreach (Control row in tgRequests.Controls) SetEnabledDeep(row, !busy);
            if (text != null) { tgStatus.ForeColor = UiTheme.Muted; tgStatus.Text = text; }
        }
        static void SetEnabledDeep(Control root, bool enabled)
        {
            if (root is Button) root.Enabled = enabled;
            foreach (Control c in root.Controls) SetEnabledDeep(c, enabled);
        }

        async void TelegramCheck()
        {
            if (tgBusy) return;
            SetTelegramBusy(true, "Проверяю сообщения боту…");
            Outcome outcome;
            try { outcome = await WorkerClient.RunTelegramAsync(WorkerPath, "telegram_pending", null, null); }
            catch { outcome = Outcome.Failure("Не удалось проверить Телеграм. Повторите через минуту."); }
            if (IsDisposed) return;
            ShowTelegramRequests(outcome);
            SetTelegramBusy(false, null);
        }

        public void ShowTelegramRequests(Outcome outcome)
        {
            tgRequests.Controls.Clear(); tgRequests.RowStyles.Clear(); tgRequests.RowCount = 0;
            tgStatus.Text = outcome.Message ?? "";
            tgStatus.ForeColor = !outcome.Success || outcome.TelegramStatus == "failed" ? UiTheme.Error
                : outcome.TelegramStatus == "done" ? UiTheme.Success : UiTheme.Muted;
            if (outcome.Success && outcome.TelegramStatus == "pending")
                foreach (var request in outcome.TelegramRequests) AddTelegramCandidate(request);
            UpdateWraps();
        }

        void AddTelegramCandidate(TelegramRequest request)
        {
            var row = new TableLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Dock = DockStyle.Top, ColumnCount = 1, RowCount = 2, BackColor = UiTheme.Background, Margin = new Padding(0, 4, 0, 4) };
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            row.Controls.Add(new Label { Text = "Написал: " + request.Display + ". Это вы?", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Text, Font = UiTheme.Font(10.5f), Margin = new Padding(0, 2, 0, 4) }, 0, 0);
            var buttons = new FlowLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = UiTheme.Background, Margin = Padding.Empty };
            var yes = new RoundedButton { Text = "Да, это я", Primary = true, Width = 140, Height = 34, Font = UiTheme.Font(10F, FontStyle.Bold), Margin = new Padding(0, 0, 8, 0) };
            var no = new RoundedButton { Text = "Нет", Primary = false, Width = 90, Height = 34, Font = UiTheme.Font(10F), Margin = Padding.Empty };
            yes.Click += delegate { TelegramApprove(request); };
            // «Нет» only hides the row: nothing is granted, the stranger's request expires in 1 h.
            no.Click += delegate { tgRequests.Controls.Remove(row); row.Dispose(); if (tgRequests.Controls.Count == 0) tgStatus.Text = "Если это не вы, ничего не делайте: доступа у этого человека нет. Напишите боту сами и нажмите «Проверить»."; };
            buttons.Controls.Add(yes); buttons.Controls.Add(no);
            row.Controls.Add(buttons, 0, 1);
            tgRequests.RowCount += 1;
            tgRequests.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            tgRequests.Controls.Add(row, 0, tgRequests.RowCount - 1);
        }

        async void TelegramApprove(TelegramRequest request)
        {
            if (tgBusy) return;
            SetTelegramBusy(true, "Подключаю бота к вам и перезапускаю его… Это займёт 1–3 минуты.");
            Outcome outcome;
            try { outcome = await WorkerClient.RunTelegramAsync(WorkerPath, "telegram_approve", request.Id, request.UserId); }
            catch { outcome = Outcome.Failure("Не удалось подключить бота. Повторите через минуту."); }
            if (IsDisposed) return;
            bool approved = outcome.Success && (outcome.TelegramStatus == "approved" || outcome.TelegramStatus == "approved_partial");
            tgRequests.Controls.Clear(); tgRequests.RowStyles.Clear(); tgRequests.RowCount = 0;
            tgStatus.Text = outcome.Message ?? "";
            tgStatus.ForeColor = approved ? UiTheme.Success : UiTheme.Error;
            SetTelegramBusy(false, null);
            if (approved) tgCheck.Visible = false;
            UpdateWraps();
        }

        // Raw backend progress is technical (16 upstream stages): it goes to the
        // hidden journal only. The visible line is one of four human phrases,
        // chosen from the protocol's stage id, plus "N из 16".
        void OnProgress(string message)
        {
            LogAppend(message);
            lastProgress = DateTime.UtcNow;
            silence.Visible = false;
            if (!message.StartsWith("Этап ", StringComparison.Ordinal)) Status.Text = message;
        }

        void OnStage(int index, int total, string stage)
        {
            lastProgress = DateTime.UtcNow;
            silence.Visible = false;
            int phrase = StageIndex(stage);
            if (phrase >= 0) { Status.Text = StageChain[phrase]; HighlightChain(phrase); }
            if (total > 0) stageStep.Text = index + " из " + total;
            if (total > 0)
            {
                bar.Style = ProgressBarStyle.Continuous;
                bar.Value = Math.Max(0, Math.Min(100, (int)(100L * index / total)));
            }
        }

        // Emphasise the phrase the install is currently in; the other three stay muted.
        void HighlightChain(int active)
        {
            for (int i = 0; i < chainLabels.Length; i++)
            {
                if (chainLabels[i] == null) continue;
                bool on = (i == active);
                chainLabels[i].Font = on ? UiTheme.Font(9f, FontStyle.Bold) : UiTheme.Font(9f);
                chainLabels[i].ForeColor = on ? UiTheme.Accent : UiTheme.Muted;
            }
        }

        internal static int StageIndex(string stage)
        {
            switch (stage)
            {
                case "uv": case "git": case "node": case "system-packages": case "platform-sdks":
                    return 0;
                case "repository": case "config-templates":
                    return 1;
                case "python": case "venv": case "dependencies": case "node-deps": case "desktop": case "path": case "bootstrap-marker":
                    return 2;
                case "configure": case "gateway":
                    return 3;
                default:
                    return -1;
            }
        }

        internal static string HumanStage(string stage)
        {
            int index = StageIndex(stage);
            return index < 0 ? null : StageChain[index];
        }

        // The progress bar tracks "Этап index/count" from the status line, so it
        // fills proportionally no matter which backend stage reported it.
        void UpdateProgressFromStatus()
        {
            int n, m;
            if (TryParseStage(Status.Text, out n, out m))
            {
                bar.Style = ProgressBarStyle.Continuous;
                bar.Value = Math.Max(0, Math.Min(100, (int)(100L * n / m)));
            }
        }

        static bool TryParseStage(string message, out int n, out int m)
        {
            n = 0; m = 0;
            if (message == null || !message.StartsWith("Этап ")) return false;
            int p = message.IndexOf('/');
            if (p < 0) return false;
            int start = message.IndexOf(' ');
            int a, b;
            if (!int.TryParse(message.Substring(start + 1, p - start - 1).Trim(), out a)) return false;
            int colon = message.IndexOf(':', p);
            if (colon < 0) return false;
            if (!int.TryParse(message.Substring(p + 1, colon - p - 1).Trim(), out b)) return false;
            if (a < 1 || b < 1 || a > b) return false;
            n = a; m = b; return true;
        }

        [System.Runtime.InteropServices.DllImport("user32.dll")]
        static extern IntPtr SendMessage(IntPtr handle, int message, IntPtr wParam, IntPtr lParam);
        void LogAppend(string line)
        {
            if (IsDisposed || string.IsNullOrEmpty(line)) return;
            int first = (int)SendMessage(Log.Handle, 0xCE, IntPtr.Zero, IntPtr.Zero);
            int lastVisible = Log.GetCharIndexFromPosition(new Point(Math.Max(0, Log.ClientSize.Width - 2), Math.Max(0, Log.ClientSize.Height - 2)));
            bool follow = Log.TextLength == 0 || lastVisible >= Log.TextLength - 2;
            int selection = Log.SelectionStart, length = Log.SelectionLength;
            Log.AppendText((Log.TextLength > 0 ? "\r\n" : "") + DateTime.Now.ToString("HH:mm:ss") + "  " + line);
            if (follow) { Log.SelectionStart = Log.TextLength; Log.ScrollToCaret(); }
            else
            {
                Log.Select(selection, length);
                int now = (int)SendMessage(Log.Handle, 0xCE, IntPtr.Zero, IntPtr.Zero);
                SendMessage(Log.Handle, 0xB6, IntPtr.Zero, new IntPtr(first - now));
            }
        }
    }
    internal static class Program
    {
        [STAThread] static void Main()
        {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            bool created;
            using (var mutex = new Mutex(true, @"Local\HermesSubscriberSetup.Ui.v1", out created))
            {
                if (!created) { MessageBox.Show("Установщик уже открыт. Переключитесь в его окно.", "Hermes", MessageBoxButtons.OK, MessageBoxIcon.Information); return; }
                try { Application.Run(new InstallerForm()); }
                finally { mutex.ReleaseMutex(); }
            }
        }
    }
}
