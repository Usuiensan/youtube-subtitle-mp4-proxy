# これをPowerShellで実行してからSSHコマンドを打ってください
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8


Write-Host "=== Remote working tree guard ==="
ssh -t masato@192.168.68.117 "sudo git -C /opt/youtube-mp4-proxy stash push -m auto-stash-before-youtube-proxy-update"

ssh -t masato@192.168.68.117 "sudo /usr/local/sbin/youtube-proxy-update"

