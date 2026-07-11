# これをPowerShellで実行してからSSHコマンドを打ってください
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8


ssh masato@192.168.68.117 "sudo /usr/local/sbin/youtube-proxy-update"

Write-Host ""
Write-Host "=== Restart Ollama ==="

# 既存の Ollama を強制終了
Stop-Process -Name "ollama" -Force -ErrorAction SilentlyContinue

# プロセス終了を少し待つ
Start-Sleep -Seconds 2

# 起動用 PS1 を独立した PowerShell 7 プロセスで起動
$OllamaScript = Join-Path $PSScriptRoot "start-ollama.ps1"

# Start-Process `
    -FilePath "pwsh.exe" `
    -ArgumentList @(
        "-NoLogo"
        "-NoProfile"
        "-NonInteractive"
        "-ExecutionPolicy"
        "Bypass"
        "-File"
        "`"$OllamaScript`""
    ) `
    -WindowStyle Hidden

# Write-Host "Ollama start process launched."

function Test-ServiceHealth {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [int]$TimeoutSec = 2
    )

    try {
        $null = Invoke-RestMethod -Uri $Uri -TimeoutSec $TimeoutSec
        Write-Host "$Name: OK"
        return $true
    }
    catch {
        Write-Warning "$Name: unavailable ($Uri) - $($_.Exception.Message)"
        return $false
    }
}

# API 起動確認
$healthResults = @(
    [pscustomobject]@{ Name = "Ollama API"; Uri = "http://127.0.0.1:11434/api/version" },
    [pscustomobject]@{ Name = "Ollama Models"; Uri = "http://127.0.0.1:11434/api/tags" }
)

$healthyCount = 0
foreach ($health in $healthResults) {
    if (Test-ServiceHealth -Name $health.Name -Uri $health.Uri) {
        $healthyCount++
    }
}

if ($healthyCount -eq 0) {
    Write-Warning "Ollama related health checks failed, but update will continue."
}
else {
    Write-Host "Ollama related health checks: $healthyCount/$($healthResults.Count) OK"
}

Write-Host ""
Write-Host "=== Update Complete ==="

exit 0
