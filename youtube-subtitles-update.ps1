# これをPowerShellで実行してからSSHコマンドを打ってください
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8


ssh masato@192.168.68.117 "sudo /usr/local/sbin/youtube-proxy-update"