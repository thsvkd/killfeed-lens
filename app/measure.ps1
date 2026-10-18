# Samples the running app's Electron processes (the engine is not included): CPU (% of one core and
# of the whole machine), private memory, whether any loads a GPU driver, and the overlay window's
# capture/input flags.
# Usage (while `npm start` runs):  powershell -NoProfile -File measure.ps1 [-Seconds 30] [-MinimizeBoard]
# -MinimizeBoard first minimizes the board window, like a game covering it (only the overlay paints).
param([int]$Seconds = 30, [switch]$MinimizeBoard)

Add-Type @"
using System; using System.Runtime.InteropServices;
public static class W {
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool GetWindowDisplayAffinity(IntPtr h, out uint a);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
}
"@

$all = Get-CimInstance Win32_Process
$exe = Join-Path $PSScriptRoot 'node_modules\electron\dist\electron.exe'
$root = $all | Where-Object { $_.ExecutablePath -eq $exe -and $_.CommandLine -notlike '*--type=*' } | Select-Object -First 1
if (-not $root) { "the app is not running ($exe)"; exit 1 }
$ids = @($root.ProcessId) + @($all | Where-Object { $_.ParentProcessId -eq $root.ProcessId -and $_.Name -eq 'electron.exe' } | ForEach-Object ProcessId)
$kind = @{}
foreach ($p in $all | Where-Object { $ids -contains $_.ProcessId }) {
  $kind[$p.ProcessId] = if ($p.CommandLine -match '--type=([\w-]+)') { $Matches[1] } else { 'main' }
}

if ($MinimizeBoard) {
  # the board's title, spelled as code points: Windows PowerShell reads this file as ANSI
  $title = 'killfeed-lens ' + [char]0x00B7 + ' ' + [char]0xD300 + ' ' + [char]0xD604 + [char]0xD669
  $board = [W]::FindWindow([NullString]::Value, $title)
  if ($board -eq [IntPtr]::Zero) { "board window not found"; exit 1 }
  [void][W]::ShowWindow($board, 6) # SW_MINIMIZE
  Start-Sleep -Seconds 2
}
$cpu0 = @{}; foreach ($id in $ids) { $cpu0[$id] = (Get-Process -Id $id).TotalProcessorTime.TotalMilliseconds }
$t0 = Get-Date
Start-Sleep -Seconds $Seconds
$cores = [Environment]::ProcessorCount
$elapsedMs = ((Get-Date) - $t0).TotalMilliseconds

"{0,-14} {1,7} {2,9} {3,9} {4,10}" -f 'process', 'pid', 'cpu%core', 'cpu%all', 'private MB'
$tot = 0; $mem = 0
foreach ($id in $ids) {
  $p = Get-Process -Id $id -ErrorAction SilentlyContinue
  if (-not $p) { continue }
  $ms = $p.TotalProcessorTime.TotalMilliseconds - $cpu0[$id]
  $core = 100 * $ms / $elapsedMs
  $tot += $core; $mem += $p.PrivateMemorySize64
  "{0,-14} {1,7} {2,9:N2} {3,9:N3} {4,10:N1}" -f $kind[$id], $id, $core, ($core / $cores), ($p.PrivateMemorySize64 / 1MB)
}
"{0,-14} {1,7} {2,9:N2} {3,9:N3} {4,10:N1}" -f 'TOTAL', '', $tot, ($tot / $cores), ($mem / 1MB)
# GPU: whether any of our processes holds a device on a real GPU, i.e. loaded a vendor user-mode
# driver. (Utilisation counters were unreadable here: no counter sets, no GPUEngine WMI class.)
$umd = '^(nvwgf2umx|nvldumdx|nvoglv64|amdxx64|amdxc64|atidxx64|aticfx64|igd10iumd64|igd12umd64|igdumdim64)\.dll$'
$gpuUsers = foreach ($id in $ids) {
  $m = (Get-Process -Id $id -ErrorAction SilentlyContinue).Modules | Where-Object { $_.ModuleName -match $umd }
  if ($m) { "$($kind[$id]) ($(($m.ModuleName | Sort-Object -Unique) -join ','))" }
}
"GPU driver loaded by: {0}" -f $(if ($gpuUsers) { $gpuUsers -join '; ' } else { 'none (software compositing, no device on the game GPU)' })

$h = [W]::FindWindow([NullString]::Value, 'killfeed-lens overlay')
if ($h -ne [IntPtr]::Zero) {
  [uint32]$a = 0; [void][W]::GetWindowDisplayAffinity($h, [ref]$a)
  $ex = [W]::GetWindowLong($h, -20)
  $flags = @{ TRANSPARENT = 0x20; LAYERED = 0x80000; NOACTIVATE = 0x08000000; TOPMOST = 0x8; TOOLWINDOW = 0x80 }
  $on = ($flags.Keys | Where-Object { $ex -band $flags[$_] } | Sort-Object) -join ' '
  "overlay: visible={2}; display affinity 0x{0:X} (0x11 = excluded from capture); ex-style: {1}" -f $a, $on, [W]::IsWindowVisible($h)
} else { "overlay window not found" }
