using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using HermesSetup;

// Layout QA for the four-screen wizard. Captures every screen at 560/680 width
// and 100/150/200% simulated scale, then asserts nothing is clipped and no
// scrollbar ever appears inside the window. Scaling is SIMULATED, not OS DPI:
// f.Scale is the same transformation WinForms autoscale applies to geometry,
// and ScaleFonts reproduces the DPI text rendering that WinForms does not do
// in-process (it never scales explicitly-set fonts).
class LayoutTests {
 [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h,int m,IntPtr w,IntPtr l);
 static void Require(bool b,string msg) { if(!b) throw new Exception(msg); }

 // Snapshot every font first, then reassign scaled fonts: a child that inherits
 // its parent's font must not be multiplied twice.
 static void CollectFonts(Control root,List<KeyValuePair<Control,Font>> list) {
  list.Add(new KeyValuePair<Control,Font>(root,root.Font));
  foreach(Control c in root.Controls) CollectFonts(c,list);
 }
 static void ScaleFonts(Control root,float factor) {
  var pairs=new List<KeyValuePair<Control,Font>>();
  CollectFonts(root,pairs);
  foreach(var p in pairs) {
   Font f=p.Value; if(f==null) continue;
   float s=f.SizeInPoints*factor; if(s<4f) s=4f;
   try { p.Key.Font=new Font(f.FontFamily,s,f.Style,GraphicsUnit.Point); } catch {}
  }
 }

 static bool AnyAutoScroll(Control c) {
  var sc=c as ScrollableControl;
  if(sc!=null && sc.AutoScroll && (sc.VerticalScroll.Visible || sc.HorizontalScroll.Visible)) return true;
  foreach(Control ch in c.Controls) if(AnyAutoScroll(ch)) return true;
  return false;
 }
 static void CheckInside(Control root,Form f,string where) {
  foreach(Control c in root.Controls) {
   if(c.Visible && c.Width>0 && c.Height>0 && (c is Label || c is ButtonBase || c is TextBoxBase || c is ProgressBar)) {
    var r=new Rectangle(f.PointToClient(c.PointToScreen(Point.Empty)),c.Size);
    Require(f.ClientRectangle.Contains(r), where+": "+c.GetType().Name+" '"+c.Name+"' text='"+c.Text+"' outside client area "+r);
   }
   CheckInside(c,f,where);
  }
 }
 // Lowest visible edge of the page's content, in form client coordinates. Clipping
 // checks alone missed a card that slid UNDER the footer, so this measures overlap
 // against the footer's top edge instead of the window border.
 static int MaxBottom(Control root,Form f) {
  int max=0;
  foreach(Control c in root.Controls) {
   if(!c.Visible) continue;
   if(c.Width>0 && c.Height>0) {
    var r=new Rectangle(f.PointToClient(c.PointToScreen(Point.Empty)),c.Size);
    if(r.Bottom>max) max=r.Bottom;
   }
   int inner=MaxBottom(c,f); if(inner>max) max=inner;
  }
  return max;
 }

 static void Settle(Form f) { Application.DoEvents(); f.PerformLayout(); Application.DoEvents(); }
 static void CheckKeyPage(Form f,Control page,int footerTop,string where) {
  int bottom=MaxBottom(page,f);
  Require(bottom<=footerTop, where+": content overlaps the footer ("+bottom+" > "+footerTop+")");
  Require(!AnyAutoScroll(f), where+": scrollbar appeared");
  CheckInside(f,f,where);
 }
 static void Snap(Form f,string dir,string name) {
  using(var image=new Bitmap(f.Width,f.Height)) {
   f.DrawToBitmap(image,new Rectangle(Point.Empty,f.Size));
   image.Save(Path.Combine(dir,name+".png"));
  }
 }

 [STAThread] static int Main(string[] args) {
  Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
  try {
   string dir=args.Length>0?args[0]:AppDomain.CurrentDomain.BaseDirectory;
   string[] names={"screen1-providers","screen2-key","screen3-install","screen4-done"};
   string[] pageNames={"PageChoose","PageKey","PageInstall","PageDone"};
   var onStage=typeof(InstallerForm).GetMethod("OnStage",BindingFlags.NonPublic|BindingFlags.Instance);
   var resultField=typeof(InstallerForm).GetField("result",BindingFlags.NonPublic|BindingFlags.Instance);
   foreach(float scale in new[]{1f,1.5f,2f}) foreach(Size size in new[]{new Size(680,560),new Size(560,500)}) {
    string tag=size.Width+"-"+scale.ToString(CultureInfo.InvariantCulture);
    using(var f=new InstallerForm()) {
     f.AutoScaleMode=AutoScaleMode.None;
     f.ClientSize=size;
     if(scale!=1) { f.Scale(new SizeF(scale,scale)); ScaleFonts(f,scale); }
     f.Show(); Application.DoEvents();
     for(int page=0; page<4; page++) {
      if(page==InstallerForm.PageInstall) {
       // Drive the real structured-stage path (not a raw Status string) so the
       // screenshot shows the human phrase, "N из 16", the elapsed timer and the
       // highlighted chain phrase exactly as production does.
       onStage.Invoke(f,new object[]{3,16,"repository"});
       for(int i=0;i<12;i++) f.Log.AppendText("Стадия "+i+" — служебная запись журнала\r\n");
      }
      if(page==InstallerForm.PageDone) {
       // Screen 4 is captured in the verified-success state; "Открыть Hermes" is
       // enabled only after result != null && result.Success.
       resultField.SetValue(f,new Outcome{Success=true,Message="Готово"});
      }
      f.GoTo(page);
      Application.DoEvents(); f.PerformLayout(); Application.DoEvents();
      // The window-border clip check alone let a fifth card slide under the footer.
      // Assert the page's lowest visible content stays above the footer's top edge.
      var pageCtl=f.Controls.Find(pageNames[page],true);
      var footerCtl=f.Controls.Find("Footer",true);
      Require(pageCtl.Length==1 && footerCtl.Length==1, tag+" page "+page+": page and footer present");
      int footerTop=f.PointToClient(footerCtl[0].PointToScreen(Point.Empty)).Y;
      int contentBottom=MaxBottom(pageCtl[0],f);
      Require(contentBottom<=footerTop, tag+" page "+page+": content overlaps the footer ("+contentBottom+" > "+footerTop+")");
      Require(!AnyAutoScroll(f), tag+" page "+page+": scrollbar appeared inside the window");
      CheckInside(f,f,tag+" page "+page);
      if(page==InstallerForm.PageKey) {
       // Every provider's full note (Dahl carries a 4-step guide) must fit the
       // key screen, not only the first preset's.
       for(int p=0;p<f.ProviderCount;p++) {
        f.SelectProvider(p); Application.DoEvents(); f.PerformLayout(); Application.DoEvents();
        int bottom=MaxBottom(pageCtl[0],f);
        Require(bottom<=footerTop, tag+" key screen, provider "+f.PresetAt(p).id+": content overlaps the footer ("+bottom+" > "+footerTop+")");
        Require(!AnyAutoScroll(f), tag+" key screen, provider "+f.PresetAt(p).id+": scrollbar appeared");
        CheckInside(f,f,tag+" key screen "+f.PresetAt(p).id);
       }
       // Backup providers (issue #10): after ОК in the dialog the key screen shows only a
       // one-line summary ("Запасные: A, B · изменить") on the model link's line. For every
       // primary provider, with the most rows the catalog allows, it must stay above the footer.
       for(int p=0;p<f.ProviderCount;p++) {
        f.SelectProvider(p); Settle(f);
        string where=tag+" key screen backup summary, provider "+f.PresetAt(p).id;
        if(f.BackupCandidates().Count==0) continue;
        using(var d=f.CreateBackupDialog()) {
         d.AddRow();
         for(int r=0;r<d.RowCount;r++) d.Keys[r].Text="LAYOUTFAKEKEY"+r;
         Require(d.TryAccept()==null, where+": dialog did not accept two fake keys: "+d.ErrorText);
         f.ApplyBackupDialog(d);
        }
        Settle(f);
        Require(f.BackupCount>0 && f.BackupSummary.StartsWith("Запасные: "), where+": summary line missing");
        CheckKeyPage(f,pageCtl[0],footerTop,where);
        if(p==1) Snap(f,dir,"screen2-key-backup-"+tag);
        f.ClearBackups(); Settle(f);
       }
       f.SelectProvider(0); Application.DoEvents(); f.PerformLayout(); Application.DoEvents();
      }
      if(page==InstallerForm.PageDone) {
       var launch=(Button)f.Controls.Find("Launch",true)[0];
       Require(launch.Enabled, tag+": screen 4 must be captured with 'Открыть Hermes' active");
      }
      if(page==InstallerForm.PageInstall) {
       var active=(Label)f.Controls.Find("Chain1",true)[0];
       Require(active.Font.Bold, tag+": current chain phrase is not emphasised");
      }
      using(var image=new Bitmap(f.Width,f.Height)) {
       f.DrawToBitmap(image,new Rectangle(Point.Empty,f.Size));
       image.Save(Path.Combine(dir,names[page]+"-"+tag+".png"));
      }
     }
     // «Отменить» in the footer while installing; then the stopped screen and a real, long
     // INSTALL error, each with «Повторить» + «Изменить провайдера или ключ».
     {
      var runningField=typeof(InstallerForm).GetField("running",BindingFlags.NonPublic|BindingFlags.Instance);
      var installPage=f.Controls.Find("PageInstall",true)[0];
      var footer=f.Controls.Find("Footer",true)[0];
      var cancelBtn=(Button)f.Controls.Find("CancelInstall",true)[0];
      runningField.SetValue(f,true);
      f.GoTo(InstallerForm.PageInstall); Settle(f);
      int footerTop=f.PointToClient(footer.PointToScreen(Point.Empty)).Y;
      Require(cancelBtn.Visible && cancelBtn.Enabled, tag+": «Отменить» is shown while installing");
      CheckKeyPage(f,installPage,footerTop,tag+" install screen with «Отменить»");
      Snap(f,dir,"screen3-cancel-"+tag);
      runningField.SetValue(f,false);
      var error=new Protocol(null,delegate{},delegate{return true;});
      error.Feed("{\"type\":\"error\",\"code\":\"INSTALL\",\"message\":\"Официальная установка остановлена: этап repository, код 1. Скачивание кода Hermes не удалось. Проверьте интернет или VPN и повторите. Проверка API ещё не запускалась. Сохраните этот код для диагностики; не удаляйте папку установки.\"}");
      var outcomes=new[]{ Outcome.Failure(Outcome.CodeCancelled,WorkerClient.StoppedMessage), error.Finish(1) };
      string[] shots={"screen3-stopped-","screen3-error-"};
      for(int o=0;o<outcomes.Length;o++) {
       string where=tag+" "+shots[o].Trim('-');
       f.GoTo(InstallerForm.PageInstall);
       f.ShowFailure(outcomes[o]); Settle(f);
       Require(!cancelBtn.Visible, where+": «Отменить» hidden once nothing runs");
       var retry=(Button)f.Controls.Find("Action",true)[0];
       var change=(Button)f.Controls.Find("ChangeProvider",true)[0];
       Require(retry.Visible && change.Visible && retry.Text=="Повторить" && change.Text=="Изменить провайдера или ключ", where+": both actions shown");
       footerTop=f.PointToClient(footer.PointToScreen(Point.Empty)).Y;
       CheckKeyPage(f,installPage,footerTop,where);
       Snap(f,dir,shots[o]+tag);
      }
      f.ChangeProviderOrKey(); Settle(f);
     }
     // Expanded details must still fit, expose the log, and keep the user's scroll position.
     f.GoTo(InstallerForm.PageInstall);
     var details=f.Controls.Find("Details",true);
     Require(details.Length==1,"details toggle present");
     ((Button)details[0]).PerformClick(); Application.DoEvents(); f.PerformLayout(); Application.DoEvents();
     Require(!AnyAutoScroll(f), tag+": expanding details introduced a scrollbar");
     var logRect=new Rectangle(f.PointToClient(f.Log.PointToScreen(Point.Empty)),f.Log.Size);
     Require(f.ClientRectangle.Contains(logRect), tag+": log outside client area");
     Require(logRect.Height>=60*scale,"log panel too small: height="+logRect.Height);
     for(int i=0;i<200;i++) f.Log.AppendText("Строка журнала "+i+" — только отображение\r\n");
     f.Log.Focus(); f.Log.SelectionStart=0; f.Log.ScrollToCaret(); Application.DoEvents();
     int before=(int)SendMessage(f.Log.Handle,0xCE,IntPtr.Zero,IntPtr.Zero);
     typeof(InstallerForm).GetMethod("LogAppend",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(f,new object[]{"новая служебная стадия"});
     Application.DoEvents();
     int after=(int)SendMessage(f.Log.Handle,0xCE,IntPtr.Zero,IntPtr.Zero);
     Require(before==after,"incoming log yanked scroll: "+before+" -> "+after);
     var timer=(System.Windows.Forms.Timer)typeof(InstallerForm).GetField("timer",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(f);
     int length=f.Log.TextLength;
     typeof(System.Windows.Forms.Timer).GetMethod("OnTick",BindingFlags.NonPublic|BindingFlags.Instance).Invoke(timer,new object[]{EventArgs.Empty});
     Require(f.Log.TextLength==length,"elapsed timer pollutes log");
     f.Close();
    }
   }
   // The backup dialog itself at 100/150/200%: two rows plus a validation error, nothing
   // clipped, content above its footer, no scrollbar.
   foreach(float scale in new[]{1f,1.5f,2f}) {
    string tag="dialog-"+scale.ToString(CultureInfo.InvariantCulture);
    string catalog=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"providers.json");
    var presets=ProviderCatalog.Load(catalog);
    var candidates=new List<ProviderPreset>();
    for(int i=1;i<presets.Length;i++) if(!presets[i].IsCustom) candidates.Add(presets[i]);
    using(var d=new BackupKeysDialog(candidates,null,presets[0].endpoint)) {
     d.AutoScaleMode=AutoScaleMode.None;
     if(scale!=1) { d.Scale(new SizeF(scale,scale)); ScaleFonts(d,scale); }
     d.Show(); Settle(d);
     d.AddRow(); d.AddRow();
     int expected=Math.Min(Request.MaxFallbacks,candidates.Count);
     d.Keys[0].Text="LAYOUTFAKEKEY0"; d.Keys[1].Text="short";
     if(expected>2) d.Keys[2].Text="LAYOUTFAKEKEY2";
     Require(d.TryAccept()!=null, tag+": a short key must be rejected");
     Settle(d);
     Require(d.RowCount==expected, tag+": "+expected+" rows expected, got "+d.RowCount);
     var content=d.Controls.Find("DialogContent",true); var footer=d.Controls.Find("DialogFooter",true);
     Require(content.Length==1 && footer.Length==1, tag+": dialog content and footer present");
     int footerTop=d.PointToClient(footer[0].PointToScreen(Point.Empty)).Y;
     int bottom=MaxBottom(content[0],d);
     Require(bottom<=footerTop, tag+": dialog content overlaps its footer ("+bottom+" > "+footerTop+")");
     Require(!AnyAutoScroll(d), tag+": scrollbar appeared in the dialog");
     CheckInside(d,d,tag);
     Snap(d,dir,"backup-"+tag);
     d.Close();
    }
   }
   Console.WriteLine("PASS layout: 4 screens x 6 sizes plus install-with-cancel, stopped and error screens x 6 sizes, key screen with the backup summary per provider, backup dialog at 3 scales, nothing clipped, no in-window scrollbars, log scroll preserved; scaling is SIMULATED, not OS DPI.");
   return 0;
  } catch(Exception e) { Console.Error.WriteLine(e); return 1; }
 }
}
