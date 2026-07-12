param(
    [string]$BaseRef = "origin/main",
    [string]$ConfigPath = ".ai-quality.yml",
    [string]$AiQualityPlatformPath = "C:\private\ai-quality-platform-2"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (Test-Path $VenvPython) {
    $python = $VenvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
} else {
    throw "python が見つかりません。Windows マシンに Python を入れて PATH を通してください。"
}

& $python -c "import ai_quality_platform" 2>$null
if ($LASTEXITCODE -ne 0) {
    if (-not (Test-Path $AiQualityPlatformPath)) {
        throw "ai-quality-platform が見つかりません。$AiQualityPlatformPath を確認してください。"
    }
    Write-Host "Installing ai-quality-platform from $AiQualityPlatformPath..."
    & $python -m pip install -e $AiQualityPlatformPath
}

git fetch origin main | Out-Null

$diffFile = Join-Path $env:TEMP "ai-quality-diff.txt"
$diffText = git diff --binary "$BaseRef...HEAD"
if ($LASTEXITCODE -ne 0) {
    throw "git diff の作成に失敗しました。BaseRef=$BaseRef"
}

Set-Content -Path $diffFile -Value $diffText -Encoding utf8

if ([string]::IsNullOrWhiteSpace($diffText)) {
    Write-Host "No diff against $BaseRef. Review skipped."
    exit 0
}

Write-Host "Running AI quality review against $BaseRef..."
& $python -m ai_quality_platform.cli --config $ConfigPath --diff $diffFile
if ($LASTEXITCODE -ne 0) {
    throw "AI quality review failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "レポートを確認して必要な修正を入れ、もう一度このスクリプトを実行してから push してください。"
