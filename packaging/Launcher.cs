using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("gTR")]
[assembly: AssemblyProduct("gTR")]
[assembly: AssemblyCompany("Gruzin")]
[assembly: AssemblyCopyright("Copyright 2026 Gruzin")]

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string runtime = Path.Combine(root, "runtime", "pythonw.exe");
        if (!File.Exists(runtime))
        {
            MessageBox.Show("Распакуйте весь комплект gTR. Папка runtime должна находиться рядом с gTR.exe.",
                            "gTR", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
        try
        {
            var launch = new ProcessStartInfo(runtime, "-m gtr gui");
            launch.WorkingDirectory = root;
            launch.UseShellExecute = false;
            using (Process.Start(launch)) { }
            return 0;
        }
        catch (Win32Exception)
        {
            MessageBox.Show("Windows не запустила среду приложения. Откройте gTR.cmd из этой же папки.",
                            "gTR", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 2;
        }
    }
}
