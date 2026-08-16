[CmdletBinding()]
param(
    [switch]$Get,
    [switch]$Set,
    [switch]$Validate,
    [switch]$Restart,
    [switch]$Status,
    [string]$Key,
    [string]$Value,
    [string]$ExpectedRevision,
    [string]$SshTarget = 'furukawa@192.168.68.119'
)

$ErrorActionPreference = 'Stop'
$operations = @($Get, $Set, $Validate, $Restart, $Status) | Where-Object { $_ }
if ($operations.Count -gt 1) { throw '操作は1つだけ指定してください。' }
if ($operations.Count -eq 0) { $Get = $true }

function Invoke-ConfigWrapper([string[]]$Arguments) {
    & ssh.exe -o BatchMode=yes -o ConnectTimeout=10 $SshTarget (('sudo -n /usr/local/sbin/youtube-proxy-config ' + ($Arguments -join ' ')).Trim())
    if ($LASTEXITCODE -ne 0) { throw "本番コンフィグ操作に失敗しました (exit=$LASTEXITCODE)" }
}

if ($Set) {
    if ($Key -notmatch '^(LOCAL_LLM_MAX_OUTPUT_TOKENS|TRANSLATION_CHUNK_INPUT_TOKENS|TRANSLATION_DEFAULT_PROFILE|TRANSLATION_API_RETRY_MAX_ATTEMPTS|TRANSLATION_API_RETRY_BASE_SECONDS|GEMINI_THINKING_LEVEL|SYSTEM_METRICS_INTERVAL_SECONDS|SYSTEM_METRICS_HISTORY_SECONDS|DISCORD_OPERATOR_USER_ID|CACHE_ARCHIVE_MOUNT_POINT)$') { throw 'Key はallowlist内で指定してください。' }
    if ($Key -eq 'CACHE_ARCHIVE_MOUNT_POINT') {
        if ($Value -notmatch '^/([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$') { throw 'Value が不正です。' }
    } elseif ($Value -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Value が不正です。' }
    if ($ExpectedRevision -notmatch '^[0-9a-f]{64}$') { throw 'ExpectedRevision が不正です。' }
    Invoke-ConfigWrapper @('set', '--key', $Key, '--value', $Value, '--expected-revision', $ExpectedRevision)
    Invoke-ConfigWrapper @('restart')
} elseif ($Validate) {
    Invoke-ConfigWrapper @('validate')
} elseif ($Restart) {
    Invoke-ConfigWrapper @('restart')
} elseif ($Status) {
    Invoke-ConfigWrapper @('status')
} else {
    Invoke-ConfigWrapper @('get')
}
