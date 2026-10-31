// GUI-subsystem launcher: Node's detached PowerShell 5.1 can exit without running -File,
// while a non-detached child can be reaped with Electron. This detached GUI process owns
// the hidden PowerShell child until the update finishes. It runs only its sibling job files.
using System;
using System.Diagnostics;
using System.IO;

internal static class UpdateLauncher {
    private static readonly object LogLock = new object();

    private static string Quoted(string value) { return "\"" + value + "\""; }

    private static int Main(string[] args) {
        string jobDir = AppDomain.CurrentDomain.BaseDirectory;
        string log = Path.Combine(jobDir, "launcher.log");
        try {
            if (args.Length != 0) return 2;
            // Derive siblings from the running image instead of comparing equivalent short/
            // long path strings (e.g. RUNNER~1 versus runneradmin). No caller-selected files.
            string script = Path.Combine(jobDir, "install-update.ps1");
            string config = Path.Combine(jobDir, "job.json");
            if (!File.Exists(script) || !File.Exists(config)) return 2;

            string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell", "v1.0", "powershell.exe");
            var start = new ProcessStartInfo(powershell,
                "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quoted(script)
                + " -ConfigFile " + Quoted(config));
            start.WorkingDirectory = jobDir;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            using (var child = new Process()) {
                child.StartInfo = start;
                DataReceivedEventHandler record = (sender, e) => {
                    if (e.Data != null) lock (LogLock) File.AppendAllText(log, e.Data + Environment.NewLine);
                };
                child.OutputDataReceived += record;
                child.ErrorDataReceived += record;
                child.Start();
                child.BeginOutputReadLine();
                child.BeginErrorReadLine();
                child.WaitForExit();
                return child.ExitCode;
            }
        } catch (Exception error) {
            File.AppendAllText(log, error.ToString() + Environment.NewLine);
            return 1;
        }
    }
}
