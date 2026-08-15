# ローカルのmainをアーカイブ化して本番へ転送し、サーバー側の更新処理を実行します。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$sha = (git -C $repo rev-parse HEAD).Trim()
$archive = Join-Path $env:TEMP "youtube-proxy-$sha.tar.gz"
$stage = Join-Path $env:TEMP "youtube-proxy-archive-$sha"

try {
    git -C $repo archive --format=tar.gz --output=$archive HEAD
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    tar -xzf $archive -C $stage
    tar -czf $archive -C $stage .
    & scp.exe $archive furukawa@192.168.68.119:/tmp/
    if ($LASTEXITCODE -ne 0) { throw "scpに失敗しました (exit=$LASTEXITCODE)" }
    & ssh.exe furukawa@192.168.68.119 "sudo -n /usr/local/sbin/youtube-proxy-update --archive /tmp/youtube-proxy-$sha.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "本番更新に失敗しました (exit=$LASTEXITCODE)" }
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 10
