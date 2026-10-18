#Requires -Version 5.1
. "$PSScriptRoot\_common.ps1"

function Write-Usage {
    Write-Host "사용법: .\script\package.ps1"
    Write-Host "  PyInstaller onedir(엔진)와 Electron 앱(app/)을 빌드하고 스모크한 뒤 Inno Setup 설치기를 dist/ 에 만든다."
}

$Mode = if ($args.Count -ge 1) { [string]$args[0] } else { "" }
switch ($Mode) {
    "" { }
    { $_ -in "-h", "--help", "help" } { Write-Usage; exit 0 }
    default { Write-Usage; Write-Fail "알 수 없는 인자: $Mode" }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Fail "uv가 필요합니다: https://docs.astral.sh/uv/" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Write-Fail "Node.js 22.12 이상(npm)이 필요합니다: https://nodejs.org/" }

Write-Info "의존성 동기화 중 (dev, ocr, windows, package)..."
& uv sync --locked --extra dev --extra ocr --extra windows --extra package
if ($LASTEXITCODE -ne 0) { Write-Fail "uv sync 실패" }

$Version = & uv run --no-sync python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or -not $Version) { Write-Fail "pyproject.toml version을 읽지 못했습니다" }
$App = "dist\KillfeedLens-win32-x64"
$Setup = "dist\killfeed-lens-$Version-setup.exe"

Write-Info "PyInstaller 빌드 중 (엔진 onedir, v$Version)..."
& uv run --no-sync pyinstaller --noconfirm --distpath dist --workpath build packaging\killfeed-lens.spec
if ($LASTEXITCODE -ne 0) { Write-Fail "PyInstaller 빌드 실패" }

Write-Info "Electron 앱 의존성 설치 중 (npm ci)..."
& npm ci --prefix app --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Write-Fail "npm ci 실패" }

Write-Info "Electron 앱 패키징 중 (@electron/packager + fuses, 엔진 포함)..."
& node app\package.mjs $Version
if ($LASTEXITCODE -ne 0) { Write-Fail "Electron 패키징 실패" }

Write-Info "스모크: $App"
& uv run --no-sync python scripts\smoke_packaged.py $App
if ($LASTEXITCODE -ne 0) { Write-Fail "스모크 실패: $App" }

$Iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue | ForEach-Object Source),
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Iscc) { Write-Fail "Inno Setup 6(ISCC.exe)이 없습니다: winget install JRSoftware.InnoSetup" }

Write-Info "설치기 빌드 중 ($Iscc)..."
& $Iscc /Qp "/DAppVersion=$Version" packaging\killfeed-lens.iss
if ($LASTEXITCODE -ne 0) { Write-Fail "Inno Setup 빌드 실패" }

if (-not (Test-Path $Setup)) { Write-Fail "산출물이 없습니다: $Setup" }
Write-Ok ("{0} ({1:N1} MB)" -f $Setup, ((Get-Item $Setup).Length / 1MB))
Write-Ok "패키징 완료."

exit 0
