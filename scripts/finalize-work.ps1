param(
    [string]$Message = ""
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$status = git status --short
if (-not $status) {
    Write-Host "No changes to commit."
} else {
    git add -A
    if (-not $Message) {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        $Message = "Update $timestamp"
    }
    git commit -m $Message
    git push
}

$updateScript = "C:\private\youtube-subtitle-mp4-proxy\youtube-subtitles-update.ps1"
if (Test-Path $updateScript) {
    & $updateScript
} else {
    Write-Host "Update script not found: $updateScript"
}
