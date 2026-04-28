param(
    [string]$OutputPath = "docs/windows-font-families.txt"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$ResolvedOutputPath = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    [System.IO.Path]::GetFullPath($OutputPath)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $OutputPath))
}

Add-Type -AssemblyName System.Drawing

$InstalledFonts = New-Object System.Drawing.Text.InstalledFontCollection
$Families = $InstalledFonts.Families |
    ForEach-Object { $_.Name } |
    Sort-Object -Unique

$OutputDir = Split-Path -Parent $ResolvedOutputPath
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Header = @(
    "# Windows font family names",
    "# Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
    "# Use one of these values as SUBTITLE_FONT.",
    ""
)

$Header + $Families | Set-Content -LiteralPath $ResolvedOutputPath -Encoding UTF8

Write-Host "Wrote $($Families.Count) font family names to:"
Write-Host $ResolvedOutputPath
