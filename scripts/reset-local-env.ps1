param(
    [string]$CacheDir = ".cache",
    [string]$DefaultLang = "ja",
    [int]$MaxDurationSeconds = 3600,
    [int]$MaxHeight = 720,
    [int]$CacheTtlSeconds = 604800,
    [int]$JobTimeoutSeconds = 7200,
    [int]$HlsSegmentSeconds = 6,
    [int]$HlsReadyTimeoutSeconds = 1800,
    [switch]$GpuEncode,
    [string]$FfmpegVideoEncoder = "",
    [string]$FfmpegVideoPreset = "",
    [int]$FfmpegVideoCrf = 23,
    [int]$FfmpegVideoCq = 23,
    [string]$SubtitleFont = "BIZ UDPGothic",
    [int]$SubtitleFontSize = 20,
    [int]$SubtitleMarginV = 34,
    [int]$SubtitleMarginL = 4,
    [int]$SubtitleMarginR = 4,
    [string]$SubtitlePrimaryColour = "&H00FFFFFF",
    [string]$SubtitleBackColour = "&H40000000",
    [string]$YtdlpExtraArgs = "--js-runtimes deno --remote-components ejs:npm",
    [string]$YtdlpProxy = "",
    [string]$YtdlpCookiesFile = "",
    [string]$YoutubeDataApiKey = "",
    [string]$DiscordBotToken = "",
    [string]$DiscordPrepareToken = "",
    [string]$YoutubeProxyBaseUrl = "http://127.0.0.1:8000",
    [int]$DiscordPreparePollSeconds = 10,
    [int]$DiscordPreparePollTimeoutSeconds = 7200,
    [string]$ApiKey = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$EnvFilePath = Join-Path $RepoRoot ".env.local"

function Invoke-FileOperationWithRetry {
    param(
        [scriptblock]$Operation,
        [string]$Path,
        [string]$Action,
        [int]$Attempts = 20,
        [int]$DelayMilliseconds = 250
    )

    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        try {
            return & $Operation
        } catch [System.IO.IOException] {
            if ($Attempt -eq $Attempts) {
                throw "Could not $Action '$Path' after $Attempts attempts. Close any process that is using it and try again. Last error: $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        } catch [System.UnauthorizedAccessException] {
            if ($Attempt -eq $Attempts) {
                throw "Could not $Action '$Path' after $Attempts attempts. Close any process that is using it and try again. Last error: $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

if (
    (-not $YoutubeDataApiKey -or -not $DiscordBotToken -or -not $DiscordPrepareToken) -and
    (Test-Path -LiteralPath $EnvFilePath)
) {
    $ExistingEnvLines = Invoke-FileOperationWithRetry `
        -Path $EnvFilePath `
        -Action "read" `
        -Operation { [System.IO.File]::ReadAllLines($EnvFilePath) }

    foreach ($Line in $ExistingEnvLines) {
        if (-not $YoutubeDataApiKey -and $Line -match '^\s*YOUTUBE_DATA_API_KEY\s*=\s*(.+?)\s*$') {
            $YoutubeDataApiKey = $Matches[1].Trim().Trim('"').Trim("'")
        } elseif (-not $DiscordBotToken -and $Line -match '^\s*DISCORD_BOT_TOKEN\s*=\s*(.+?)\s*$') {
            $DiscordBotToken = $Matches[1].Trim().Trim('"').Trim("'")
        } elseif (-not $DiscordPrepareToken -and $Line -match '^\s*DISCORD_PREPARE_TOKEN\s*=\s*(.+?)\s*$') {
            $DiscordPrepareToken = $Matches[1].Trim().Trim('"').Trim("'")
        } elseif ($Line -match '^\s*YOUTUBE_PROXY_BASE_URL\s*=\s*(.+?)\s*$') {
            $YoutubeProxyBaseUrl = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}

$ResolvedCacheDir = if ([System.IO.Path]::IsPathRooted($CacheDir)) {
    [System.IO.Path]::GetFullPath($CacheDir)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $CacheDir))
}

$RepoRootWithSlash = $RepoRoot.Path.TrimEnd('\') + '\'
$CacheDirWithSlash = $ResolvedCacheDir.TrimEnd('\') + '\'
if (-not $CacheDirWithSlash.StartsWith($RepoRootWithSlash, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to delete cache outside the repository: $ResolvedCacheDir"
}

if (Test-Path -LiteralPath $ResolvedCacheDir) {
    Remove-Item -LiteralPath $ResolvedCacheDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $ResolvedCacheDir | Out-Null

if ($GpuEncode) {
    $FfmpegVideoEncoder = "h264_nvenc"
    if (-not $FfmpegVideoPreset) {
        $FfmpegVideoPreset = "fast"
    }
}
if (-not $FfmpegVideoEncoder) {
    $FfmpegVideoEncoder = "libx264"
}

$env:CACHE_DIR = $ResolvedCacheDir
$env:DEFAULT_LANG = $DefaultLang
$env:MAX_DURATION_SECONDS = [string]$MaxDurationSeconds
$env:MAX_HEIGHT = [string]$MaxHeight
$env:CACHE_TTL_SECONDS = [string]$CacheTtlSeconds
$env:JOB_TIMEOUT_SECONDS = [string]$JobTimeoutSeconds
$env:HLS_SEGMENT_SECONDS = [string]$HlsSegmentSeconds
$env:HLS_READY_TIMEOUT_SECONDS = [string]$HlsReadyTimeoutSeconds
$env:FFMPEG_VIDEO_ENCODER = $FfmpegVideoEncoder
$env:FFMPEG_VIDEO_PRESET = $FfmpegVideoPreset
$env:FFMPEG_VIDEO_CRF = [string]$FfmpegVideoCrf
$env:FFMPEG_VIDEO_CQ = [string]$FfmpegVideoCq
$env:SUBTITLE_FONT = $SubtitleFont
$env:SUBTITLE_FONT_SIZE = [string]$SubtitleFontSize
$env:SUBTITLE_MARGIN_V = [string]$SubtitleMarginV
$env:SUBTITLE_MARGIN_L = [string]$SubtitleMarginL
$env:SUBTITLE_MARGIN_R = [string]$SubtitleMarginR
$env:SUBTITLE_PRIMARY_COLOUR = $SubtitlePrimaryColour
$env:SUBTITLE_BACK_COLOUR = $SubtitleBackColour
$env:YTDLP_EXTRA_ARGS = $YtdlpExtraArgs

if ($YoutubeDataApiKey) {
    $env:YOUTUBE_DATA_API_KEY = $YoutubeDataApiKey
} else {
    Remove-Item Env:\YOUTUBE_DATA_API_KEY -ErrorAction SilentlyContinue
}

if ($DiscordBotToken) {
    $env:DISCORD_BOT_TOKEN = $DiscordBotToken
} else {
    Remove-Item Env:\DISCORD_BOT_TOKEN -ErrorAction SilentlyContinue
}

if ($DiscordPrepareToken) {
    $env:DISCORD_PREPARE_TOKEN = $DiscordPrepareToken
} else {
    Remove-Item Env:\DISCORD_PREPARE_TOKEN -ErrorAction SilentlyContinue
}

$env:YOUTUBE_PROXY_BASE_URL = $YoutubeProxyBaseUrl
$env:DISCORD_PREPARE_POLL_SECONDS = [string]$DiscordPreparePollSeconds
$env:DISCORD_PREPARE_POLL_TIMEOUT_SECONDS = [string]$DiscordPreparePollTimeoutSeconds

if ($YtdlpProxy) {
    $env:YTDLP_PROXY = $YtdlpProxy
} else {
    Remove-Item Env:\YTDLP_PROXY -ErrorAction SilentlyContinue
}

if ($YtdlpCookiesFile) {
    $env:YTDLP_COOKIES_FILE = $YtdlpCookiesFile
} else {
    Remove-Item Env:\YTDLP_COOKIES_FILE -ErrorAction SilentlyContinue
}

if ($ApiKey) {
    $env:API_KEY = $ApiKey
} else {
    Remove-Item Env:\API_KEY -ErrorAction SilentlyContinue
}

$EnvLines = @(
    "CACHE_DIR=$ResolvedCacheDir",
    "DEFAULT_LANG=$DefaultLang",
    "MAX_DURATION_SECONDS=$MaxDurationSeconds",
    "MAX_HEIGHT=$MaxHeight",
    "CACHE_TTL_SECONDS=$CacheTtlSeconds",
    "JOB_TIMEOUT_SECONDS=$JobTimeoutSeconds",
    "HLS_SEGMENT_SECONDS=$HlsSegmentSeconds",
    "HLS_READY_TIMEOUT_SECONDS=$HlsReadyTimeoutSeconds",
    "FFMPEG_VIDEO_ENCODER=$FfmpegVideoEncoder",
    "FFMPEG_VIDEO_PRESET=$FfmpegVideoPreset",
    "FFMPEG_VIDEO_CRF=$FfmpegVideoCrf",
    "FFMPEG_VIDEO_CQ=$FfmpegVideoCq",
    "SUBTITLE_FONT=$SubtitleFont",
    "SUBTITLE_FONT_SIZE=$SubtitleFontSize",
    "SUBTITLE_MARGIN_V=$SubtitleMarginV",
    "SUBTITLE_MARGIN_L=$SubtitleMarginL",
    "SUBTITLE_MARGIN_R=$SubtitleMarginR",
    "SUBTITLE_PRIMARY_COLOUR=$SubtitlePrimaryColour",
    "SUBTITLE_BACK_COLOUR=$SubtitleBackColour",
    "YTDLP_EXTRA_ARGS=$YtdlpExtraArgs"
)

if ($YtdlpProxy) {
    $EnvLines += "YTDLP_PROXY=$YtdlpProxy"
}
if ($YtdlpCookiesFile) {
    $EnvLines += "YTDLP_COOKIES_FILE=$YtdlpCookiesFile"
}
if ($YoutubeDataApiKey) {
    $EnvLines += "YOUTUBE_DATA_API_KEY=$YoutubeDataApiKey"
}
if ($DiscordBotToken) {
    $EnvLines += "DISCORD_BOT_TOKEN=$DiscordBotToken"
}
if ($DiscordPrepareToken) {
    $EnvLines += "DISCORD_PREPARE_TOKEN=$DiscordPrepareToken"
}
$EnvLines += "YOUTUBE_PROXY_BASE_URL=$YoutubeProxyBaseUrl"
$EnvLines += "DISCORD_PREPARE_POLL_SECONDS=$DiscordPreparePollSeconds"
$EnvLines += "DISCORD_PREPARE_POLL_TIMEOUT_SECONDS=$DiscordPreparePollTimeoutSeconds"
if ($ApiKey) {
    $EnvLines += "API_KEY=$ApiKey"
}

Invoke-FileOperationWithRetry `
    -Path $EnvFilePath `
    -Action "write" `
    -Operation {
        [System.IO.File]::WriteAllLines(
            $EnvFilePath,
            $EnvLines,
            [System.Text.UTF8Encoding]::new($false)
        )
    } | Out-Null

Write-Host "Cache cleared and local settings written."
Write-Host ".env.local=$EnvFilePath"
Write-Host "CACHE_DIR=$env:CACHE_DIR"
Write-Host "SUBTITLE_FONT=$env:SUBTITLE_FONT"
Write-Host "SUBTITLE_FONT_SIZE=$env:SUBTITLE_FONT_SIZE"
Write-Host "SUBTITLE_BACK_COLOUR=$env:SUBTITLE_BACK_COLOUR"
Write-Host "FFMPEG_VIDEO_ENCODER=$env:FFMPEG_VIDEO_ENCODER"
Write-Host "FFMPEG_VIDEO_PRESET=$env:FFMPEG_VIDEO_PRESET"
Write-Host "FFMPEG_VIDEO_CQ=$env:FFMPEG_VIDEO_CQ"
if ($env:YOUTUBE_DATA_API_KEY) {
    Write-Host "YOUTUBE_DATA_API_KEY=(set)"
}
if ($env:DISCORD_BOT_TOKEN) {
    Write-Host "DISCORD_BOT_TOKEN=(set)"
}
if ($env:DISCORD_PREPARE_TOKEN) {
    Write-Host "DISCORD_PREPARE_TOKEN=(set)"
}
Write-Host "YOUTUBE_PROXY_BASE_URL=$env:YOUTUBE_PROXY_BASE_URL"
Write-Host ""
Write-Host "Start the app with:"
Write-Host "uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers"
Write-Host "Start the Discord bot with:"
Write-Host "python -m bot.main"
