using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

namespace HermesSetup
{
    // One palette for the whole wizard (UI_DESIGN_SPEC.md). Screens never hard-code
    // colours: they read them from here, so a single accent and one text scale hold.
    internal static class UiTheme
    {
        public static readonly Color Background = Color.FromArgb(0xF4, 0xF5, 0xF7);
        public static readonly Color Card = Color.FromArgb(0xFF, 0xFF, 0xFF);
        public static readonly Color Accent = Color.FromArgb(0x15, 0x65, 0xC0);
        public static readonly Color AccentHover = Color.FromArgb(0x0D, 0x4F, 0x99);
        public static readonly Color Text = Color.FromArgb(0x1C, 0x1E, 0x21);
        public static readonly Color Muted = Color.FromArgb(0x5E, 0x65, 0x70);
        public static readonly Color Border = Color.FromArgb(0xD8, 0xDC, 0xE3);
        public static readonly Color Success = Color.FromArgb(0x13, 0x7A, 0x45);
        public static readonly Color Error = Color.FromArgb(0xB4, 0x23, 0x18);
        public static readonly Color CardHover = Color.FromArgb(0xF3, 0xF7, 0xFC);
        public static readonly Color CardSelected = Color.FromArgb(0xE8, 0xF1, 0xFB);
        public static readonly Color DisabledFill = Color.FromArgb(0xE6, 0xE8, 0xEC);
        public static readonly Color DisabledText = Color.FromArgb(0x9A, 0xA1, 0xAB);
        public static readonly Color SecondaryHover = Color.FromArgb(0xEE, 0xF0, 0xF3);
        public static readonly Color DisabledBorder = Color.FromArgb(0xB4, 0xB8, 0xBE);

        public static Font Font(float points) { return new Font("Segoe UI", points); }
        public static Font Font(float points, FontStyle style) { return new Font("Segoe UI", points, style); }
    }

    // Owner-drawn button with rounded corners (spec: 6-8 px radius, one accent colour).
    // Three visual roles: primary (filled), secondary (outline), link (accent text only).
    public class RoundedButton : Button
    {
        public int CornerRadius = 6;
        public Color AccentColor = UiTheme.Accent;
        public bool Primary = true;
        public bool Link;
        bool hover;
        public RoundedButton()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            FlatStyle = FlatStyle.Flat;
            FlatAppearance.BorderSize = 0;
            UseVisualStyleBackColor = false;
            Cursor = Cursors.Hand;
            BackColor = UiTheme.Background;
        }
        public static GraphicsPath RoundedPath(RectangleF r, float radius)
        {
            var p = new GraphicsPath();
            float d = radius * 2f;
            if (d > r.Width) d = r.Width;
            if (d > r.Height) d = r.Height;
            if (d <= 0) { p.AddRectangle(r); p.CloseFigure(); return p; }
            p.AddArc(r.X, r.Y, d, d, 180, 90);
            p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }
        protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hover = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color parent = BehindColor();
            e.Graphics.Clear(parent);
            var r = new RectangleF(0.5f, 0.5f, Width - 1.5f, Height - 1.5f);
            Color text;
            using (GraphicsPath path = RoundedPath(r, CornerRadius))
            {
                if (!Enabled)
                {
                    text = UiTheme.DisabledText;
                    if (!Link)
                    {
                        using (var brush = new SolidBrush(UiTheme.DisabledFill)) e.Graphics.FillPath(brush, path);
                        if (!Primary)
                            using (var pen = new Pen(UiTheme.DisabledBorder, 1f)) e.Graphics.DrawPath(pen, path);
                    }
                }
                else if (Link)
                {
                    text = hover ? UiTheme.AccentHover : AccentColor;
                }
                else if (Primary)
                {
                    text = Color.White;
                    using (var brush = new SolidBrush(hover ? UiTheme.AccentHover : AccentColor)) e.Graphics.FillPath(brush, path);
                }
                else
                {
                    text = UiTheme.Text;
                    if (hover) using (var brush = new SolidBrush(UiTheme.SecondaryHover)) e.Graphics.FillPath(brush, path);
                    using (var pen = new Pen(UiTheme.Border, 1f)) e.Graphics.DrawPath(pen, path);
                }
            }
            TextRenderer.DrawText(e.Graphics, Text, Font, Rectangle.Round(r), text,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPrefix);
        }
        // Transparent children are not used here, but a link button sitting on a
        // coloured card must blend with the card, not with a stale control grey.
        Color BehindColor()
        {
            Color back = Parent != null ? Parent.BackColor : UiTheme.Background;
            if (back.A == 0 || back == Color.Transparent) back = UiTheme.Card;
            return back;
        }
    }

    // One selectable provider card: title, a note that wraps inside the space left
    // of the "get a key" link, and that link. Every child label paints the card's
    // own colour (no white plate over the selected/hover background).
    public class ProviderCard : Panel
    {
        public readonly ProviderPreset Preset;
        public readonly bool IsRecommended;
        public event EventHandler Activated;
        public event EventHandler KeyRequested;
        readonly Label title;
        readonly Label badge;
        readonly Label note;
        readonly Label key;
        readonly TableLayoutPanel grid;
        readonly ToolTip tip = new ToolTip();
        bool selected;
        bool hover;

        public ProviderCard(ProviderPreset preset, bool recommended)
        {
            Preset = preset;
            IsRecommended = recommended;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Margin = new Padding(0, 0, 0, 5);
            // 26 px on the left leaves room for the selection dot without moving
            // the text when selection changes. Compact padding (7 px) keeps five
            // cards inside the content area even at 560x500.
            Padding = new Padding(26, 7, 12, 7);
            BackColor = UiTheme.Card;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            bool hasKey = preset.KeyUrl.Length > 0;
            grid = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 2, RowCount = 2, BackColor = UiTheme.Card, Margin = Padding.Empty, Padding = Padding.Empty, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            title = new Label { Text = preset.label, AutoSize = true, Font = UiTheme.Font(10.5f, FontStyle.Bold), ForeColor = UiTheme.Text, Cursor = Cursors.Hand, Anchor = AnchorStyles.Left, Margin = new Padding(0), BackColor = Color.Transparent };
            // The spec calls for ONE line of note per card. AutoSize is off so the
            // row keeps a fixed 16 px height; AutoEllipsis truncates the rest and the
            // tooltip still carries the full text.
            note = new Label { Text = preset.note == null ? "" : preset.note, AutoSize = false, AutoEllipsis = true, Height = 16, ForeColor = UiTheme.Muted, Cursor = Cursors.Hand, Font = UiTheme.Font(9f), Margin = new Padding(0, 1, 4, 0), BackColor = Color.Transparent, Anchor = AnchorStyles.Left | AnchorStyles.Right };
            grid.Controls.Add(title, 0, 0);
            grid.Controls.Add(note, 0, 1);
            if (recommended)
            {
                badge = new Label { Text = "Рекомендуем", AutoSize = true, ForeColor = UiTheme.Accent, Font = UiTheme.Font(8f), Anchor = AnchorStyles.Right, Margin = new Padding(8, 0, 0, 0), BackColor = Color.Transparent };
                grid.Controls.Add(badge, 1, 0);
            }
            if (hasKey)
            {
                // A plain accent link label keeps the card's second row at the note's
                // height instead of a 28 px button, which is what made five cards
                // overflow the 560x500 content area. Behaviour is unchanged.
                key = new Label { Text = "Получить ключ", AutoSize = true, ForeColor = UiTheme.Accent, Cursor = Cursors.Hand, Font = UiTheme.Font(9.5f, FontStyle.Bold), Anchor = AnchorStyles.Right, Margin = new Padding(8, 0, 0, 0), BackColor = Color.Transparent };
                key.Click += delegate { if (KeyRequested != null) KeyRequested(this, EventArgs.Empty); };
                key.MouseEnter += delegate { key.ForeColor = UiTheme.AccentHover; };
                key.MouseLeave += delegate { key.ForeColor = UiTheme.Accent; };
                grid.Controls.Add(key, 1, 1);
            }
            Controls.Add(grid);
            title.Click += delegate { Raise(); };
            note.Click += delegate { Raise(); };
            grid.Click += delegate { Raise(); };
            Click += delegate { Raise(); };
            title.MouseEnter += delegate { hover = true; ApplyColors(); };
            note.MouseEnter += delegate { hover = true; ApplyColors(); };
            title.MouseLeave += delegate { hover = false; ApplyColors(); };
            note.MouseLeave += delegate { hover = false; ApplyColors(); };

            string tipText = preset.note == null ? "" : preset.note;
            if (hasKey)
            {
                if (tipText.Length > 0) tipText += "\r\n";
                tipText += "Где получить ключ: " + preset.KeyUrl;
            }
            if (tipText.Length > 0)
            {
                tip.SetToolTip(this, tipText);
                tip.SetToolTip(grid, tipText);
                tip.SetToolTip(title, tipText);
                tip.SetToolTip(note, tipText);
                if (key != null) tip.SetToolTip(key, tipText);
            }
            ApplyColors();
            WrapTo(Width);
        }

        public bool Selected
        {
            get { return selected; }
            set { if (selected != value) { selected = value; ApplyColors(); Invalidate(); } }
        }

        Color Fill { get { return selected ? UiTheme.CardSelected : (hover ? UiTheme.CardHover : UiTheme.Card); } }

        void ApplyColors()
        {
            Color fill = Fill;
            BackColor = fill;
            if (grid != null) grid.BackColor = fill;
            if (title != null) title.BackColor = Color.Transparent;
            if (note != null) note.BackColor = Color.Transparent;
            if (badge != null) badge.BackColor = Color.Transparent;
            Invalidate(true);
        }

        protected override void OnMouseEnter(EventArgs e) { hover = true; ApplyColors(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hover = false; ApplyColors(); base.OnMouseLeave(e); }

        // The note is a fixed-height, ellipsising label anchored Left|Right inside
        // the space left of the key column, so WinForms sizes it to the live column
        // width itself. The method is kept for API stability and only clears any
        // stale width cap.
        public void WrapTo(int cardWidth)
        {
            if (note == null) return;
            note.MaximumSize = Size.Empty;
        }
        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            WrapTo(ClientSize.Width);
        }
        void Raise() { if (Activated != null) Activated(this, EventArgs.Empty); }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color page = Parent != null ? Parent.BackColor : UiTheme.Background;
            if (page.A == 0 || page == Color.Transparent) page = UiTheme.Background;
            e.Graphics.Clear(page);
            var r = new Rectangle(1, 1, Width - 3, Height - 3);
            using (GraphicsPath path = RoundedButton.RoundedPath(r, 8))
            {
                using (var brush = new SolidBrush(Fill)) e.Graphics.FillPath(brush, path);
                using (var pen = new Pen(selected ? UiTheme.Accent : (hover ? UiTheme.Accent : UiTheme.Border), selected ? 2f : 1f))
                    e.Graphics.DrawPath(pen, path);
            }
            if (selected)
            {
                int d = Math.Max(10, Padding.Left / 2);
                int x = Math.Max(3, Padding.Left / 2 - d / 2);
                int y = Math.Max(3, (Height - d) / 2);
                using (var dot = new SolidBrush(UiTheme.Accent)) e.Graphics.FillEllipse(dot, x, y, d, d);
            }
            base.OnPaint(e);
        }
    }
}
