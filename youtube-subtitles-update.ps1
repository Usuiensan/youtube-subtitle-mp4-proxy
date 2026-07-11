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

# API 起動確認
$OllamaReady = $false

for ($i = 1; $i -le 3; $i++) {
    Start-Sleep -Seconds 1

    try {
        $null = Invoke-RestMethod `
            -Uri "http://127.0.0.1:11434/api/version" `
            -TimeoutSec 2

        $OllamaReady = $true
        break
    }
    catch {
        Write-Host "Waiting for Ollama... ($i/15)"
        break
    }
}

if ($OllamaReady) {
    Write-Host "Ollama API is ready."
}
else {
    Write-Warning "Ollama process was launched, but the API did not become ready within 15 seconds."
}

Write-Host ""
Write-Host "=== Update Complete ==="

exit 0