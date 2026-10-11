[CmdletBinding()]
param([string]$SshTarget = 'furukawa@192.168.68.119')

$ErrorActionPreference = 'Stop'
$sound = Join-Path $PSScriptRoot '..\tada.wav'
$log = Join-Path $PSScriptRoot ("reburn-recent-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

try {
    & ssh.exe -o BatchMode=yes -o ConnectTimeout=10 $SshTarget 'sudo -n /usr/local/sbin/youtube-proxy-operator reburn-recent-translations' 2>&1 |
        Tee-Object -FilePath $log
    if ($LASTEXITCODE -ne 0) { throw "再焼き込みに失敗しました (exit=$LASTEXITCODE)" }
    Write-Host "完了しました。ログ: $log"
}
finally {
    if (Test-Path -LiteralPath $sound) {
        (New-Object System.Media.SoundPlayer $sound).PlaySync()
    }
}
