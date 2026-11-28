using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: System.Reflection.AssemblyTitle("gAssets Update Center")]
[assembly: System.Reflection.AssemblyCompany("Gruzin")]
[assembly: System.Reflection.AssemblyProduct("gAssets Update Center")]
[assembly: System.Reflection.AssemblyCopyright("Copyright © 2026 Gruzin")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("1.0.0.0")]

namespace GAssets
{
    internal sealed class Runtime : IDisposable
    {
#if LOCAL_TEST
        public const bool Test = true;
        public const string ProductFolder = "gAssets-Test";
        public const string Diagnostic = "--diagnostic-profile beta7-gassets";
#else
        public const bool Test = false;
        public const string ProductFolder = "gAssets";
        public const string Diagnostic = "";
#endif
        public readonly Publisher Publisher;
        public readonly Gau Gau;
        public readonly GitHub Connector;
        public readonly Product Main;
        public readonly IReadOnlyDictionary<string, Product> Products;
        public readonly string CatalogIssue;
        public readonly string Preferences, Programs, State, Data;
        public readonly bool Isolated;
        private readonly GitHubTransport transport = new GitHubTransport();

        public Runtime(string isolated = null, string selected = null)
        {
            var local = isolated ?? Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            Isolated = isolated != null;
            var suffix = Test ? ".test" : "";
            State = Locations.Local(Path.Combine(local, ProductFolder, "UpdateCenter"));
            Data = Locations.Local(Path.Combine(local, "gAssets", "gTR"));
            Preferences = Path.Combine(State, "preferences.json");
            Programs = Path.Combine(local, "Programs", ProductFolder);
            if (isolated == null)
            {
                using (var key = Registry.CurrentUser.OpenSubKey(@"Software\gAssets\UpdateCenter" + suffix))
                    Programs = key?.GetValue("ProgramRoot", Programs) as string ?? Programs;
            }
            using (var resource = typeof(Runtime).Assembly.GetManifestResourceStream("gAssets.Publisher"))
                Publisher = new Publisher(Locations.Read(resource, 72));
            Gau = new Gau(Programs, State, Data, Publisher);
            Connector = new GitHub(transport, Locations.Under(State, "downloads"), Publisher);
            var productMutex = @"Local\gAssets.gtr.install" + (Test ? ".diag.beta7-gassets" : "");
            if (Isolated)
                productMutex += "." + Hash.Bytes(Json.Utf8.GetBytes(local)).Substring(0, 16);
            var builtIn = new Product("gassets.gtr", "gTR", "Spalov/gTR", "gTR.exe", "gTR", Publisher.Id,
                productMutex, new[] { "stable", "rc", "beta" });
            try { Products = Catalog.Open(builtIn, Publisher, AppDomain.CurrentDomain.BaseDirectory, State); }
            catch (UpdateError error)
            {
                CatalogIssue = error.Code;
                Products = new Dictionary<string, Product> { { builtIn.Id, builtIn } };
            }
            Main = selected == null ? builtIn : Products.TryGetValue(selected, out var known) ? known :
                throw new UpdateError("PRODUCT_MISMATCH");
        }

        public Dictionary<string, object> Settings() => File.Exists(Preferences)
            ? Json.Read(Locations.Read(Preferences, 32768)) : new Dictionary<string, object>();

        public void Save(Dictionary<string, object> prefs) => Locations.Atomic(Preferences, Json.Write(prefs));
        public string Setting(string key, string fallback) => Settings().TryGetValue(key, out var item) && item is string text ? text : fallback;

        public string Language => Setting("language", System.Globalization.CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "uk" ? "uk" : "ru");
        public string Text(string ru, string uk) => Language == "uk" ? uk : ru;

        public void LinkApplication(bool desktop)
        {
            if (Isolated) return;
            using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + Main.Id + (Test ? ".test" : "")))
            {
                key.SetValue("DisplayName", Main.Name + (Test ? " (gAssets Test)" : ""));
                key.SetValue("Publisher", "Gruzin");
                key.SetValue("DisplayVersion", Gau.Installed(Main).Version.ToString());
                key.SetValue("InstallLocation", Gau.ProgramRoot(Main));
                key.SetValue("DisplayIcon", Gau.Entry(Main));
                key.SetValue("UninstallString", "\"" + Application.ExecutablePath + "\" --remove " + Main.Id);
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            }
            string title = Main.Name + (Test ? " Test" : "");
            Shortcut(Environment.SpecialFolder.Programs, title, Gau.Entry(Main), Diagnostic);
            if (desktop)
                Shortcut(Environment.SpecialFolder.DesktopDirectory, title, Gau.Entry(Main), Diagnostic);
        }

        private static void Shortcut(Environment.SpecialFolder folder, string title, string executable, string args)
        {
            var name = Path.Combine(Environment.GetFolderPath(folder), title + ".lnk");
            var type = Type.GetTypeFromProgID("WScript.Shell", true);
            dynamic shell = Activator.CreateInstance(type);
            dynamic link = shell.CreateShortcut(name);
            try
            {
                link.TargetPath = executable;
                link.Arguments = args;
                link.WorkingDirectory = Path.GetDirectoryName(executable);
                link.IconLocation = executable + ",0";
                link.Save();
            }
            finally
            {
                Marshal.FinalReleaseComObject(link);
                Marshal.FinalReleaseComObject(shell);
            }
        }

        public void UnlinkApplication()
        {
            if (Isolated) return;
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + Main.Id +
                (Test ? ".test" : ""), false);
            foreach (var folder in new[] { Environment.SpecialFolder.Programs, Environment.SpecialFolder.DesktopDirectory })
                File.Delete(Path.Combine(Environment.GetFolderPath(folder), Main.Name + (Test ? " Test" : "") + ".lnk"));
        }

        public void Dispose() => transport.Dispose();
    }

    internal static class Program
    {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern int SetCurrentProcessExplicitAppUserModelID(string id);

        private static void VerifyInstall(string package)
        {
            var root = Locations.Local(Locations.Under(Path.GetTempPath(), "gassets-check-" + Guid.NewGuid().ToString("N")));
            Directory.CreateDirectory(root);
            try
            {
                using (var runtime = new Runtime(root))
                {
                    UpdateError.Require(runtime.CatalogIssue == null, "CATALOG_INVALID");
                    runtime.Gau.Install(runtime.Main, package, null, CancellationToken.None);
                    UpdateError.Require(runtime.Gau.Installed(runtime.Main, true) != null, "INSTALL_DAMAGED");
                }
            }
            finally { Directory.Delete(Locations.Local(root), true); }
        }

        [STAThread]
        private static int Main(string[] args)
        {
            bool verify = args.Length == 2 && args[0] == "--verify-install";
            bool setup = args.Length >= 2 && args[0] == "--first-install";
            bool quiet = verify || args.Contains("--quiet");
            int product = Array.IndexOf(args, "--product");
            string selected = args.Length >= 2 && args[0] == "--remove" ? args[1] :
                product >= 0 && product + 1 < args.Length ? args[product + 1] : null;
            string sandbox = null;
#if LOCAL_TEST
            var index = Array.IndexOf(args, "--test-root");
            if (index >= 0 && index + 1 < args.Length) sandbox = args[index + 1];
#endif
            try
            {
                if (verify)
                {
                    VerifyInstall(args[1]);
                    return 0;
                }
                using (var runtime = new Runtime(sandbox, selected))
                using (var mutex = new Mutex(false, @"Local\gAssets.UpdateCenter.1" + (Runtime.Test ? ".test" : "") +
                    (sandbox == null ? "" : "." + Hash.Bytes(Json.Utf8.GetBytes(sandbox)).Substring(0, 16))))
                {
                    bool locked;
                    try { locked = mutex.WaitOne(0); }
                    catch (AbandonedMutexException) { locked = true; }
                    UpdateError.Require(locked, "CENTER_RUNNING");
                    try
                    {
                        runtime.Gau.Recover(runtime.Main);
                        if (setup)
                        {
                            runtime.Gau.Install(runtime.Main, args[1], null, CancellationToken.None);
                            runtime.LinkApplication(args.Contains("--desktop"));
                            return 0;
                        }
                        Application.EnableVisualStyles();
                        Application.SetCompatibleTextRenderingDefault(false);
                        SetCurrentProcessExplicitAppUserModelID("gAssets.UpdateCenter" + (Runtime.Test ? ".test" : ""));
                        bool remove = args.Length == 2 && args[0] == "--remove" && args[1] == runtime.Main.Id;
                        bool open = args.Length > 0 && args[0].EndsWith(".gsu", StringComparison.OrdinalIgnoreCase);
                        using (var window = new Center(runtime, args.Contains("--offline"), open || remove))
                        {
                            if (remove)
                                window.Shown += (_, __) => window.OfferRemove();
                            else if (open)
                                window.Shown += (_, __) => window.OfferPackage(args[0]);
                            Application.Run(window);
                        }
                    }
                    finally { mutex.ReleaseMutex(); }
                }
                return 0;
            }
            catch (Exception error)
            {
                if (!quiet)
                    MessageBox.Show(Center.Error(error, false), "gAssets", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return error is UpdateError update && update.Code == "APP_CONTROL" ? Gau.AppControlExit : 2;
            }
        }
    }
}
