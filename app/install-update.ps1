#Requires -Version 5.1
param([Parameter(Mandatory = $true)][string]$ConfigFile)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# All output paths are derived from the one job directory, outside the installed app.
$jobDir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ConfigFile))
$updatesDir = [IO.Path]::GetDirectoryName($jobDir)
$resultFile = Join-Path $updatesDir 'result.json'
$abortFile = Join-Path $jobDir 'abort'
$utf8 = New-Object Text.UTF8Encoding($false)
$job = $null
$failureCode = 'INVALID_JOB'

function Write-Event([string]$Stage) {
    [IO.File]::AppendAllText((Join-Path $jobDir 'helper-events.log'),
        "$([DateTime]::UtcNow.ToString('O')) $PID $Stage`n", $utf8)
}

function Get-InstallerHash([string]$File) {
    $stream = [IO.File]::OpenRead($File)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Write-Result([string]$Status, [string]$Message, [string]$Code = '') {
    $version = if ($null -ne $job) { $job.version } else { '' }
    $json = @{ status = $Status; version = $version; code = $Code; message = $Message } | ConvertTo-Json -Compress
    $temporary = Join-Path $updatesDir "result-$PID.tmp"
    [IO.File]::WriteAllText($temporary, $json, $utf8)
    Move-Item -LiteralPath $temporary -Destination $resultFile -Force
}

try {
    Write-Event 'started'
    $job = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Event 'job-read'
    if ($job.version -notmatch '^\d+\.\d+\.\d+$' -or $job.sha256 -notmatch '^[a-f0-9]{64}$' -or
        -not [IO.Path]::IsPathRooted($job.appExe) -or [IO.Path]::GetFileName($job.appExe) -ne 'KillfeedLens.exe' -or
        -not (Test-Path -LiteralPath $job.appExe -PathType Leaf) -or
        $job.appPid -le 0 -or $job.enginePid -lt 0) { throw 'Invalid update job.' }
    $installer = Join-Path $jobDir "killfeed-lens-$($job.version)-setup.exe"
    $failureCode = 'HASH_MISMATCH'
    if ((Get-InstallerHash $installer) -ne $job.sha256) {
        throw 'Installer checksum mismatch.'
    }
    [IO.File]::WriteAllText((Join-Path $jobDir 'ready.json'), '{"ready":true}', $utf8)
    Write-Event 'ready'

    $failureCode = 'WAIT_TIMEOUT'
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $processIds = @($job.appPid, $job.enginePid) | Where-Object { $_ -gt 0 } | Select-Object -Unique
    while ($true) {
        if (Test-Path -LiteralPath $abortFile) { throw 'Update cancelled before application exit.' }
        $running = @(foreach ($processId in $processIds) {
            Get-Process -Id $processId -ErrorAction SilentlyContinue
        })
        if ($running.Count -eq 0) { break }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Application or engine did not exit in time.' }
        Start-Sleep -Milliseconds 100
    }
    Write-Event 'parents-exited'
    $failureCode = 'HASH_MISMATCH'
    if ((Get-InstallerHash $installer) -ne $job.sha256) {
        throw 'Installer changed while waiting for application exit.'
    }
    if (Test-Path -LiteralPath $abortFile) { throw 'Update cancelled.' }

    $installDir = [IO.Path]::GetDirectoryName($job.appExe)
    $logFile = Join-Path $jobDir 'install.log'
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/CURRENTUSER', '/NORESTART',
        '/NOCLOSEAPPLICATIONS', '/NOFORCECLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', '/RESTARTEXITCODE=3010',
        "/DIR=`"$installDir`"", "/LOG=`"$logFile`"")
    $failureCode = 'INSTALL_FAILED'
    Write-Event 'installer-starting'
    $installed = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
    if ($installed.ExitCode -eq 3010) {
        $failureCode = 'RESTART_REQUIRED'
        throw 'Windows restart required; automatic restart was disabled.'
    }
    if ($installed.ExitCode -ne 0) { throw "Installer exited with code $($installed.ExitCode)." }
    Write-Event 'installer-finished'
    Write-Result 'installed' 'Update installed.'
    $failureCode = 'APP_LAUNCH_FAILED'
    Start-Process -FilePath $job.appExe -WorkingDirectory $installDir
    exit 0
} catch {
    Write-Event "error-$failureCode"
    Write-Result 'error' $_.Exception.Message $failureCode
    exit 1
}
