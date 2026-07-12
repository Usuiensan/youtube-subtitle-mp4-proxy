param(
    [string]$BaseRef = "origin/main",
    [string]$ConfigPath = ".ai-quality.yml"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python が見つかりません。Windows マシンに Python を入れて PATH を通してください。"
}

git fetch origin main | Out-Null

$diffFile = Join-Path $env:TEMP "ai-quality-diff.txt"
git diff --binary "$BaseRef...HEAD" | Set-Content -Path $diffFile -Encoding utf8

Write-Host "Running AI quality review against $BaseRef..."
python -m ai_quality_platform.cli --config $ConfigPath --diff $diffFile

Write-Host ""
Write-Host "レポートを確認して必要な修正を入れ、もう一度このスクリプトを実行してから push してください。"
