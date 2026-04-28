param(
    [string]$CacheDir = ".cache",
    [string]$DefaultLang = "ja",
    [int]$MaxDurationSeconds = 3600,
    [int]$MaxHeight = 720,
    [int]$CacheTtlSeconds = 604800,
    [int]$JobTimeoutSeconds = 7200,
    [int]$HlsSegmentSeconds = 6,
    [int]$HlsReadyTimeoutSeconds = 1800,
    [string]$SubtitleFont = "BIZ UDGothic",
    [int]$SubtitleFontSize = 20,
    [int]$SubtitleMarginV = 34,
    [int]$SubtitleMarginL = 24,
    [int]$SubtitleMarginR = 24,
    [string]$SubtitlePrimaryColour = "&H00FFFFFF",
    [string]$SubtitleBackColour = "&H40000000",
    [string]$YtdlpExtraArgs = "--js-runtimes deno --remote-components ejs:npm",
    [string]$YtdlpCookiesFile = "",
    [string]$ApiKey = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
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

$env:CACHE_DIR = $ResolvedCacheDir
$env:DEFAULT_LANG = $DefaultLang
$env:MAX_DURATION_SECONDS = [string]$MaxDurationSeconds
$env:MAX_HEIGHT = [string]$MaxHeight
$env:CACHE_TTL_SECONDS = [string]$CacheTtlSeconds
$env:JOB_TIMEOUT_SECONDS = [string]$JobTimeoutSeconds
$env:HLS_SEGMENT_SECONDS = [string]$HlsSegmentSeconds
$env:HLS_READY_TIMEOUT_SECONDS = [string]$HlsReadyTimeoutSeconds
$env:SUBTITLE_FONT = $SubtitleFont
$env:SUBTITLE_FONT_SIZE = [string]$SubtitleFontSize
$env:SUBTITLE_MARGIN_V = [string]$SubtitleMarginV
$env:SUBTITLE_MARGIN_L = [string]$SubtitleMarginL
$env:SUBTITLE_MARGIN_R = [string]$SubtitleMarginR
$env:SUBTITLE_PRIMARY_COLOUR = $SubtitlePrimaryColour
$env:SUBTITLE_BACK_COLOUR = $SubtitleBackColour
$env:YTDLP_EXTRA_ARGS = $YtdlpExtraArgs

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

Write-Host "Cache cleared and local environment variables set."
Write-Host "CACHE_DIR=$env:CACHE_DIR"
Write-Host "SUBTITLE_FONT=$env:SUBTITLE_FONT"
Write-Host "SUBTITLE_FONT_SIZE=$env:SUBTITLE_FONT_SIZE"
Write-Host "SUBTITLE_BACK_COLOUR=$env:SUBTITLE_BACK_COLOUR"
Write-Host ""
Write-Host "Start the app with:"
Write-Host "uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers"
