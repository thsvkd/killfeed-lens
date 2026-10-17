# script/*.ps1이 공통으로 dot-source하는 헤더.
# dot-source이므로 여기서 설정한 preference/변수/함수는 호출한 스크립트의 스코프에 적용된다.
# 사용법: 각 스크립트 첫 줄에서 `. "$PSScriptRoot\_common.ps1"`

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# UTF-8 출력 보장
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# dot-source된 이 파일의 위치가 곧 script/ 디렉토리다.
$ScriptDir = $PSScriptRoot
$ProjectDir = Split-Path -Parent $ScriptDir
Set-Location $ProjectDir

# -- Colors --
function Write-Info  ($msg) { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Write-Ok    ($msg) { Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Skip  ($msg) { Write-Host "[SKIP]  $msg" -ForegroundColor Yellow }
function Write-Fail  ($msg) { Write-Host "[FAIL]  $msg" -ForegroundColor Red; exit 1 }
