using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace HermesSetup
{
    // One chosen backup provider (issue #10). The key lives in memory only and is
    // dropped after install like the primary key.
    public sealed class BackupSelection
    {
        public ProviderPreset Preset;
        public string Key;
    }

    // Modal "Запасные ключи" dialog: up to two rows of [provider][key][Вставить из буфера][Убрать],
    // «Ещё один», ОК/Отмена. Validation errors stay inside the dialog. It lives outside the
    // key screen so that screen keeps its 560x500 budget (sandbox layout run 2026-09-23:
    // inline rows overflowed the footer by 111 px).
    //
    // Same scaling rule as the wizard: AutoScaleMode.Dpi against a 96-DPI baseline,
    // AutoSize/Dock layout only, no hand-multiplied sizes.
    public sealed class BackupKeysDialog : Form
    {
        const int Inset = 24;
        public readonly ComboBox[] Providers = new ComboBox[Request.MaxFallbacks];
        public readonly TextBox[] Keys = new TextBox[Request.MaxFallbacks];
        readonly TableLayoutPanel[] rows = new TableLayoutPanel[Request.MaxFallbacks];
        readonly Label more = new Label { Name = "BackupMore", Text = "Ещё один", AutoSize = true, ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(9.5f, FontStyle.Underline), Margin = new Padding(0, 6, 0, 0) };
        readonly Label error = new Label { Name = "BackupError", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Error, Margin = new Padding(0, 8, 0, 0) };
        readonly Label hint = new Label { Name = "BackupHint", Text = "Если основной провайдер не ответит, Hermes сам переключится на запасной.", AutoSize = true, Tag = "wrap", ForeColor = UiTheme.Muted, Margin = new Padding(0, 0, 0, 8) };
        readonly TableLayoutPanel content;
        readonly List<ProviderPreset> candidates;
        readonly string primaryEndpoint;
        int count;

        public int RowCount { get { return count; } }
        public string ErrorText { get { return error.Text; } }
        // Set by TryAccept on success; null otherwise.
        public List<BackupSelection> Result { get; private set; }

        // Short name ("Dahl", "Inception Mercury"): the long card label does not fit a row.
        public static string ShortName(ProviderPreset preset)
        {
            if (preset == null) return "";
            string label = preset.label ?? preset.id ?? "";
            int dash = label.IndexOf(" — ", StringComparison.Ordinal);
            return dash > 0 ? label.Substring(0, dash) : label;
        }
        sealed class Choice
        {
            public readonly ProviderPreset Preset;
            public Choice(ProviderPreset preset) { Preset = preset; }
            public override string ToString() { return ShortName(Preset); }
        }

        public BackupKeysDialog(IList<ProviderPreset> candidates, IList<BackupSelection> current, string primaryEndpoint)
        {
            this.candidates = new List<ProviderPreset>(candidates ?? new ProviderPreset[0]);
            this.primaryEndpoint = primaryEndpoint ?? "";
            Text = "Запасные ключи";
            Font = UiTheme.Font(10F);
            AutoScaleMode = AutoScaleMode.Dpi;
            AutoScaleDimensions = new SizeF(96F, 96F);
            ClientSize = new Size(560, 400);   // 330 clipped 2 rows + error by 34 px (sandbox layout test)
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; MinimizeBox = false; ShowInTaskbar = false;
            StartPosition = FormStartPosition.CenterParent;
            BackColor = UiTheme.Background;

            var shell = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, BackColor = UiTheme.Background };
            shell.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            shell.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            shell.RowStyles.Add(new RowStyle(SizeType.Absolute, 60));

            // Content rows, then one Percent filler row (the wizard's Stack rule).
            content = new TableLayoutPanel { Name = "DialogContent", Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 7, BackColor = UiTheme.Background, Padding = new Padding(Inset, 12, Inset, 0), Margin = Padding.Empty };
            content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            for (int i = 0; i < 6; i++) content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            content.Controls.Add(new Label { Text = "Запасные ключи", AutoSize = true, Font = UiTheme.Font(14F, FontStyle.Bold), ForeColor = UiTheme.Text, Margin = new Padding(0, 0, 0, 4) }, 0, 0);
            content.Controls.Add(hint, 0, 1);
            for (int i = 0; i < Request.MaxFallbacks; i++)
            {
                rows[i] = BuildRow(i);
                content.Controls.Add(rows[i], 0, 2 + i);
            }
            content.Controls.Add(more, 0, 4);
            content.Controls.Add(error, 0, 5);
            more.Click += delegate { AddRow(); };

            var footer = new TableLayoutPanel { Name = "DialogFooter", Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, BackColor = UiTheme.Background, Padding = new Padding(Inset, 10, Inset, 10) };
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            footer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            var cancel = new RoundedButton { Name = "BackupCancel", Text = "Отмена", Primary = false, Width = 110, Height = 36, Font = UiTheme.Font(10F), Anchor = AnchorStyles.Right, Margin = new Padding(8, 0, 0, 0) };
            var ok = new RoundedButton { Name = "BackupOk", Text = "ОК", Primary = true, Width = 110, Height = 36, Font = UiTheme.Font(10.5F, FontStyle.Bold), Anchor = AnchorStyles.Right, Margin = new Padding(8, 0, 0, 0) };
            footer.Controls.Add(cancel, 1, 0);
            footer.Controls.Add(ok, 2, 0);
            ok.Click += delegate { if (TryAccept() == null) { DialogResult = DialogResult.OK; Close(); } };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            AcceptButton = ok; CancelButton = cancel;

            shell.Controls.Add(content, 0, 0);
            shell.Controls.Add(footer, 0, 1);
            Controls.Add(shell);
            content.Resize += delegate { UpdateWraps(); };
            // The TextBoxes never outlive the dialog with a key in them.
            FormClosed += delegate { foreach (TextBox key in Keys) key.Clear(); };

            // Reopen with the values entered before; a first open starts with one row.
            if (current != null)
                foreach (BackupSelection s in current)
                {
                    if (count >= Request.MaxFallbacks || s == null || !this.candidates.Contains(s.Preset)) continue;
                    Fill(count, s.Preset);
                    Keys[count].Text = s.Key ?? "";
                    count++;
                }
            if (count == 0) AddRow();
            Apply();
        }

        TableLayoutPanel BuildRow(int i)
        {
            string n = (i + 1).ToString();
            // Fixed height + Dock.Fill: an AutoSize row collapsed the Percent (key) column to ~0 px.
            var line = new TableLayoutPanel { Name = "BackupRow" + n, Dock = DockStyle.Fill, AutoSize = false, Height = 40, ColumnCount = 4, RowCount = 1, BackColor = UiTheme.Background, Margin = new Padding(0, 0, 0, 6), Visible = false };
            line.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            line.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            line.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            line.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            Providers[i] = new ComboBox { Name = "Backup" + n + "Provider", DropDownStyle = ComboBoxStyle.DropDownList, Width = 140, DropDownWidth = 220, Font = UiTheme.Font(10f), Anchor = AnchorStyles.Left, Margin = new Padding(0, 2, 8, 0) };
            // Anchor Left|Right + explicit Height: MaximumSize(0, 30) scaled to a ~2 px wide field at 150% (sandbox shot).
            Keys[i] = new TextBox { Name = "Backup" + n + "Key", MaxLength = 8192, Anchor = AnchorStyles.Left | AnchorStyles.Right, BorderStyle = BorderStyle.FixedSingle, Font = UiTheme.Font(11F), AutoSize = false, Height = 30, UseSystemPasswordChar = true, Margin = Padding.Empty };
            var paste = new RoundedButton { Name = "Backup" + n + "Paste", Text = "Вставить из буфера", Primary = false, Width = 140, Height = 30, Font = UiTheme.Font(9f), Margin = new Padding(8, 0, 0, 0) };
            var remove = new Label { Name = "Backup" + n + "Remove", Text = "Убрать", AutoSize = true, ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(9.5f, FontStyle.Underline), Anchor = AnchorStyles.Left, Margin = new Padding(8, 0, 0, 0) };
            line.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            int row = i;
            paste.Click += delegate
            {
                try { if (Clipboard.ContainsText()) { Keys[row].Text = Clipboard.GetText().Trim(); Keys[row].SelectionStart = Keys[row].TextLength; error.Text = ""; } }
                catch { error.Text = "Не удалось прочитать буфер обмена. Вставьте ключ вручную (Ctrl+V)."; }
            };
            remove.Click += delegate { RemoveRow(row); };
            line.Controls.Add(Providers[i], 0, 0);
            line.Controls.Add(Keys[i], 1, 0);
            line.Controls.Add(paste, 2, 0);
            line.Controls.Add(remove, 3, 0);
            return line;
        }

        ProviderPreset PresetAt(int row)
        {
            var choice = Providers[row].SelectedItem as Choice;
            return choice == null ? null : choice.Preset;
        }
        // Refill one row's list and select `pick` (or the first provider no other open row uses).
        void Fill(int row, ProviderPreset pick)
        {
            ComboBox combo = Providers[row];
            combo.BeginUpdate();
            combo.Items.Clear();
            int index = -1;
            for (int i = 0; i < candidates.Count; i++)
            {
                combo.Items.Add(new Choice(candidates[i]));
                if (candidates[i] == pick) index = i;
            }
            if (index < 0)
                for (int i = 0; i < candidates.Count && index < 0; i++)
                {
                    bool used = false;
                    for (int r = 0; r < count; r++) if (r != row && PresetAt(r) == candidates[i]) used = true;
                    if (!used) index = i;
                }
            if (index < 0 && candidates.Count > 0) index = 0;
            combo.SelectedIndex = index;
            combo.EndUpdate();
        }

        // «Ещё один»: one more row, pre-set to the first provider the other row does not use.
        public void AddRow()
        {
            if (count >= Request.MaxFallbacks || count >= candidates.Count) return;
            Fill(count, null);
            Keys[count].Clear();
            count++;
            error.Text = "";
            Apply();
        }
        // «Убрать»: drop one row; row 2 moves up. Zero rows + ОК = no backups.
        public void RemoveRow(int row)
        {
            if (row < 0 || row >= count) return;
            for (int r = row; r < count - 1; r++)
            {
                Fill(r, PresetAt(r + 1));
                Keys[r].Text = Keys[r + 1].Text;
            }
            count--;
            Keys[count].Clear();
            error.Text = "";
            Apply();
        }

        // null = accepted (Result set); otherwise the Russian reason, also shown in the dialog.
        public string TryAccept()
        {
            Result = null;
            var entries = new List<FallbackEntry>();
            for (int r = 0; r < count; r++)
            {
                ProviderPreset p = PresetAt(r);
                if (p == null) return Report("Строка " + (r + 1) + ": выберите провайдера.");
                if (Keys[r].TextLength == 0) return Report("Вставьте ключ в строке " + (r + 1) + " или нажмите «Убрать».");
                entries.Add(new FallbackEntry { provider_id = p.id, endpoint = p.endpoint, model = p.model ?? "", api_key = Keys[r].Text });
            }
            // Same rules as the install request: key shape, no duplicates, never the primary.
            var probe = new Request { endpoint = Request.NormalizeEndpoint(primaryEndpoint), fallbacks = entries };
            string problem = probe.ValidateFallbacks();
            if (problem != null) return Report(problem);
            var result = new List<BackupSelection>();
            for (int r = 0; r < count; r++) result.Add(new BackupSelection { Preset = PresetAt(r), Key = entries[r].api_key });
            Result = result;
            error.Text = "";
            return null;
        }
        string Report(string message) { error.Text = message; UpdateWraps(); return message; }

        void Apply()
        {
            for (int i = 0; i < rows.Length; i++) rows[i].Visible = i < count;
            more.Visible = count < Request.MaxFallbacks && count < candidates.Count;
            more.Text = count == 0 ? "Добавить запасной ключ" : "Ещё один";
            UpdateWraps();
        }
        void UpdateWraps()
        {
            int width = Math.Max(160, content.ClientSize.Width - content.Padding.Horizontal);
            hint.MaximumSize = new Size(width, 0);
            error.MaximumSize = new Size(width, 0);
        }
    }
}
