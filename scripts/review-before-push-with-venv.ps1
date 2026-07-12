param(
    [string]$BaseRef = "origin/main",
    [string]$ConfigPath = ".ai-quality.yml",
    [string]$AiQualityPlatformPath = "C:\private\ai-quality-platform-2"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$activateScript = Join-Path $repoRoot ".venv\Scripts\Activate.ps1"
$reviewScript = Join-Path $PSScriptRoot "review-before-push.ps1"

if (-not (Test-Path $activateScript)) {
    throw "仮想環境が見つかりません: $activateScript"
}

. $activateScript
& $reviewScript -BaseRef $BaseRef -ConfigPath $ConfigPath -AiQualityPlatformPath $AiQualityPlatformPath
