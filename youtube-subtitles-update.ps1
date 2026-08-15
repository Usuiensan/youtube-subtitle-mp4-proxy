# ローカルのmainをアーカイブ化して本番へ転送し、サーバー側の更新処理を実行します。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$sha = (git -C $repo rev-parse HEAD).Trim()
$archive = Join-Path $env:TEMP "youtube-proxy-$sha.tar.gz"
$tarArchive = Join-Path $env:TEMP "youtube-proxy-$sha.tar"
$emptyDir = Join-Path $env:TEMP "youtube-proxy-empty-$sha"

try {
    & git -C $repo archive --format=tar --output=$tarArchive HEAD -- ':(exclude).env'
    if ($LASTEXITCODE -ne 0) { throw "git archiveに失敗しました (exit=$LASTEXITCODE)" }
    New-Item -ItemType Directory -Path $emptyDir -Force | Out-Null
    & tar.exe -rf $tarArchive --no-recursion -C $emptyDir .
    if ($LASTEXITCODE -ne 0) { throw "tarへのrootディレクトリ追加に失敗しました (exit=$LASTEXITCODE)" }
    $source = [System.IO.File]::OpenRead($tarArchive)
    $target = [System.IO.File]::Create($archive)
    $gzip = [System.IO.Compression.GZipStream]::new($target, [System.IO.Compression.CompressionMode]::Compress)
    $source.CopyTo($gzip)
    $gzip.Dispose()
    $source.Dispose()
    $target.Dispose()
    & scp.exe $archive furukawa@192.168.68.119:/tmp/
    if ($LASTEXITCODE -ne 0) { throw "scpに失敗しました (exit=$LASTEXITCODE)" }
    & ssh.exe furukawa@192.168.68.119 "sudo -n /usr/local/sbin/youtube-proxy-update --archive /tmp/youtube-proxy-$sha.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "本番更新に失敗しました (exit=$LASTEXITCODE)" }
} finally {
    Remove-Item -LiteralPath $tarArchive, $archive, $emptyDir -Recurse -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 10
