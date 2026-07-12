param(
    [string]$BaseRef = "origin/main",
    [string]$ConfigPath = ".ai-quality.yml",
    [string]$AiQualityPlatformPath = "C:\private\ai-quality-platform-2"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (Test-Path $VenvPython) {
    $python = $VenvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
} else {
    throw "python が見つかりません。Windows マシンに Python を入れて PATH を通してください。"
}

& $python -c "import ai_quality_platform" 2>$null
if ($LASTEXITCODE -ne 0) {
    if (-not (Test-Path $AiQualityPlatformPath)) {
        throw "ai-quality-platform が見つかりません。$AiQualityPlatformPath を確認してください。"
    }
    Write-Host "Installing ai-quality-platform from $AiQualityPlatformPath..."
    & $python -m pip install -e $AiQualityPlatformPath
}

git fetch origin main | Out-Null

$diffFile = Join-Path $env:TEMP "ai-quality-diff.txt"
$diffText = git diff --binary "$BaseRef...HEAD"
if ($LASTEXITCODE -ne 0) {
    throw "git diff の作成に失敗しました。BaseRef=$BaseRef"
}

Set-Content -Path $diffFile -Value $diffText -Encoding utf8

if ([string]::IsNullOrWhiteSpace($diffText)) {
    Write-Host "No diff against $BaseRef. Review skipped."
    exit 0
}

Write-Host "Running AI quality review against $BaseRef..."
& $python -m ai_quality_platform.cli --config $ConfigPath --diff $diffFile
if ($LASTEXITCODE -ne 0) {
    throw "AI quality review failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "修正候補を抽出しています..."
$candidatesJson = Join-Path $env:TEMP "ai-quality-candidates.json"
$candidateScript = @'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ai_quality_platform.config import load_ai_quality_config
from ai_quality_platform.providers.base import create_provider
from ai_quality_platform.review import review_diff


def build_provider(config, role: str):
    provider_name = config.ai.get("provider", "openai")
    api_key = os.environ.get("AI_API_KEY", "")
    base_url = config.ai.get("base_url") or os.environ.get("AI_BASE_URL")
    model = config.ai.get("models", {}).get(role) or config.ai.get("model", "")
    if provider_name in {"openai", "gemini"} and not api_key:
        return None
    if not model:
        return None
    return create_provider(provider_name, model, api_key, base_url)


config = load_ai_quality_config(Path(sys.argv[1]))
diff_text = Path(sys.argv[2]).read_text(encoding="utf-8")
provider = build_provider(config, "review")

result = review_diff(diff_text, provider)
payload = {
    "reviewer": result.reviewer,
    "verdict": result.verdict,
    "summary": result.summary,
    "findings": [
        {
            "id": finding.id,
            "severity": finding.severity,
            "category": finding.category,
            "file": finding.file,
            "line_start": finding.line_start,
            "line_end": finding.line_end,
            "title": finding.title,
            "description": finding.description,
            "recommendation": finding.recommendation,
            "blocking": finding.blocking,
            "confidence": finding.confidence,
        }
        for finding in result.findings
    ],
}
print(json.dumps(payload, ensure_ascii=False))
'@

& $python -c $candidateScript $ConfigPath $diffFile | Set-Content -Path $candidatesJson -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    throw "修正候補の抽出に失敗しました。"
}

$candidateData = Get-Content $candidatesJson -Raw | ConvertFrom-Json
$findings = @()
if ($null -ne $candidateData.findings) {
    $findings = @($candidateData.findings)
}

if ($findings.Count -eq 0) {
    Write-Host "修正候補: なし"
} else {
    Write-Host "修正候補:"
    $selectedCandidates = @()
    foreach ($finding in $findings) {
        $location = if ($finding.file) { [string]$finding.file } else { "-" }
        if ($finding.line_start -and $finding.line_end -and ($finding.line_start -ne $finding.line_end)) {
            $location = "$location`:$($finding.line_start)-$($finding.line_end)"
        } elseif ($finding.line_start) {
            $location = "$location`:$($finding.line_start)"
        }
        Write-Host ""
        Write-Host "- [$($finding.severity)] $($finding.id) $location"
        Write-Host "  $($finding.title)"
        if ($finding.recommendation) {
            Write-Host "  対応: $($finding.recommendation)"
        }

        while ($true) {
            $choice = Read-Host "この候補をそのまま適用しますか？ [a=適用 / s=保留 / q=終了]"
            $normalized = $choice.Trim().ToLowerInvariant()
            if ($normalized -in @("a", "apply")) {
                $selectedCandidates += $finding
                break
            }
            if ($normalized -in @("s", "skip", "hold")) {
                break
            }
            if ($normalized -in @("q", "quit", "exit")) {
                break
            }
            Write-Host "a / s / q のいずれかで答えてください。"
        }

        if ($normalized -in @("q", "quit", "exit")) {
            break
        }
    }

    if ($selectedCandidates.Count -gt 0) {
        Write-Host ""
        Write-Host "選択された候補を適用しています..."
        $applyScript = @'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ai_quality_platform.autofix import run_autofix
from ai_quality_platform.config import load_ai_quality_config
from ai_quality_platform.models import Finding, ReviewResult
from ai_quality_platform.providers.base import create_provider


def build_provider(config, role: str):
    provider_name = config.ai.get("provider", "openai")
    api_key = os.environ.get("AI_API_KEY", "")
    base_url = config.ai.get("base_url") or os.environ.get("AI_BASE_URL")
    model = config.ai.get("models", {}).get(role) or config.ai.get("model", "")
    if provider_name in {"openai", "gemini"} and not api_key:
        return None
    if not model:
        return None
    return create_provider(provider_name, model, api_key, base_url)


config = load_ai_quality_config(Path(sys.argv[1]))
root = Path(sys.argv[2])
finding = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))

provider_autofix = build_provider(config, "autofix")
provider_fallback = build_provider(config, "fallback")
review = ReviewResult(
    reviewer="code",
    verdict="BLOCK",
    summary="selected finding",
    findings=[Finding(**finding)],
)
outcome, _ = run_autofix(
    root,
    [review],
    max_rounds=1,
    provider=provider_autofix,
    fallback_provider=provider_fallback,
)
print(json.dumps({
    "status": outcome.status,
    "rounds": outcome.rounds,
    "changed_files": outcome.changed_files,
    "reason": outcome.reason,
    "repeated_finding_ids": outcome.repeated_finding_ids,
}, ensure_ascii=False))
'@
        foreach ($finding in $selectedCandidates) {
            $findingJson = Join-Path $env:TEMP ("ai-quality-finding-" + [guid]::NewGuid().ToString("N") + ".json")
            $finding | ConvertTo-Json -Depth 20 | Set-Content -Path $findingJson -Encoding utf8
            $applyResultRaw = & $python -c $applyScript $ConfigPath $RepoRoot $findingJson
            if ($LASTEXITCODE -ne 0) {
                throw "選択候補の適用に失敗しました。"
            }
            $applyResult = $applyResultRaw | ConvertFrom-Json
            Write-Host "適用結果: $($applyResult.status) / $($applyResult.reason)"
            if ($applyResult.changed_files) {
                Write-Host "変更ファイル: $($applyResult.changed_files -join ', ')"
            }
        }
    }
}

Write-Host ""
Write-Host "レポートを確認して必要な修正を入れ、もう一度このスクリプトを実行してから push してください。"
