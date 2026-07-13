# ローカルのmainをアーカイブ化して本番へ転送し、サーバー側の更新処理を実行します。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$sha = (git -C $repo rev-parse HEAD).Trim()
$archive = Join-Path $env:TEMP "youtube-proxy-$sha.tar.gz"

git -C $repo archive --format=tar.gz --output=$archive HEAD
scp $archive masato@192.168.68.117:/tmp/
ssh masato@192.168.68.117 "sudo /usr/local/sbin/youtube-proxy-update --archive /tmp/youtube-proxy-$sha.tar.gz"
