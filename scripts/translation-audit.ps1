[CmdletBinding()]
param(
    [switch]$List,
    [string]$DetailName,
    [ValidateRange(1, 1000)][int]$Limit = 200,
    [string]$VideoId,
    [string]$Model,
    [string]$Provider,
    [string]$SshTarget = 'furukawa@192.168.68.119'
)

$ErrorActionPreference = 'Stop'
if (-not $List -and -not $DetailName) { $List = $true }
if ($List -and $DetailName) { throw 'List と DetailName は同時に指定できません。' }
if ($DetailName -and $DetailName -notmatch '^[A-Za-z0-9_.-]+\.jsonl$') { throw 'DetailName が不正です。' }
foreach ($value in @($VideoId, $Model, $Provider)) {
    if ($value -and $value -notmatch '^[A-Za-z0-9_.:-]{1,128}$') { throw 'フィルタ値が不正です。' }
}

$remote = @('sudo -n /usr/local/sbin/youtube-proxy-audit')
if ($DetailName) {
    $remote += "--detail-name $DetailName"
} else {
    $remote += "--list --limit $Limit"
    if ($VideoId) { $remote += "--video-id $VideoId" }
    if ($Model) { $remote += "--model $Model" }
    if ($Provider) { $remote += "--provider $Provider" }
}

& ssh.exe -o BatchMode=yes -o ConnectTimeout=10 $SshTarget ($remote -join ' ')
if ($LASTEXITCODE -ne 0) { throw "監査API操作に失敗しました (exit=$LASTEXITCODE)" }
