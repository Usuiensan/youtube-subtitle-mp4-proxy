[CmdletBinding()]
param(
    [ValidateSet('Status', 'Detach', 'Attach', 'SetProfile')]
    [string]$Action = 'Status',
    [ValidateSet('gemini_2_5_flash_lite', 'gemini_2_5_flash', 'gemini_3_5_flash', 'gpt_5_nano', 'groq_gpt_oss_20b', 'qwen3_4b_instruct', 'qwen3_8b', 'qwen3_14b', 'aya_expanse_8b', 'gemma3_12b', 'translategemma_12b')]
    [string]$Profile,
    [string]$SshTarget = 'furukawa@192.168.68.119'
)

$ErrorActionPreference = 'Stop'
$arguments = switch ($Action) {
    'Status' { @('hdd-status') }
    'Detach' { @('hdd-detach') }
    'Attach' { @('hdd-attach') }
    'SetProfile' {
        if (-not $Profile) { throw 'SetProfile には -Profile を指定してください。' }
        @('translation-profile', $Profile)
    }
}

& ssh.exe -o BatchMode=yes -o ConnectTimeout=10 $SshTarget (('sudo -n /usr/local/sbin/youtube-proxy-operator ' + ($arguments -join ' ')).Trim())
if ($LASTEXITCODE -ne 0) { throw "本番運用操作に失敗しました (exit=$LASTEXITCODE)" }
