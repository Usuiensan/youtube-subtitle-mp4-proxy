from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_FILE = ROOT / "prompts" / "translation-prompt.txt"
ENV_FILE = ROOT / ".env.local"

KEYS = [
    "TRANSLATION_PROMPT_TEMPLATE_FILE",
    "TRANSLATION_PROMPT_TEMPLATE",
    "TRANSLATION_TOPIC",
    "TRANSLATION_GLOSSARY",
    "TRANSLATION_ENABLED",
    "TRANSLATION_SOURCE_LANGS",
    "TRANSLATION_DEFAULT_PROFILE",
    "TRANSLATION_PROVIDER",
    "TRANSLATION_FALLBACK_ENGINE",
    "REMOTE_LLM_ENDPOINT",
    "REMOTE_LLM_HEALTH_URL",
    "REMOTE_LLM_MODEL",
    "REMOTE_LLM_API_KEY",
    "LOCAL_LLM_TIMEOUT_SECONDS",
    "LOCAL_LLM_TARGET_WINDOW_SECONDS",
    "LOCAL_LLM_TARGET_MAX_EVENTS",
    "LOCAL_LLM_CONTEXT_BEFORE_SECONDS",
    "LOCAL_LLM_CONTEXT_BEFORE_MAX_EVENTS",
    "LOCAL_LLM_CONTEXT_AFTER_SECONDS",
    "LOCAL_LLM_CONTEXT_AFTER_MAX_EVENTS",
    "LOCAL_LLM_MAX_OUTPUT_TOKENS",
    "LOCAL_LLM_TEMPERATURE",
    "DISCORD_BOT_TOKEN",
    "DISCORD_PREPARE_TOKEN",
    "WEBUI_TEMP_KEY_SECRET",
    "DISCORD_URL_INTAKE_CHANNEL_ID",
    "YOUTUBE_DATA_API_KEY",
    "YOUTUBE_PROXY_BASE_URL",
    "YOUTUBE_PROXY_INTERNAL_BASE_URL",
]

SECRET_KEY_HINTS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "API_KEY", "CREDENTIAL")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def mask_text(value: str, *, visible_prefix: int = 4, visible_suffix: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible_prefix + visible_suffix + 4:
        return "*" * max(8, len(value))
    return f"{value[:visible_prefix]}...{value[-visible_suffix:]}"


def is_secret_like(key: str, value: str) -> bool:
    upper_key = key.upper()
    if any(hint in upper_key for hint in SECRET_KEY_HINTS):
        return True
    if re.fullmatch(r"[A-Za-z0-9_-]{24,}", value):
        return True
    if re.fullmatch(r"[A-Za-z0-9+/=]{32,}", value):
        return True
    return False


def display_value(key: str, value: str) -> str:
    if not value:
        return "(empty)"
    if key == "TRANSLATION_PROMPT_TEMPLATE":
        lines = value.splitlines()
        preview = " | ".join(line.strip() for line in lines[:3] if line.strip())
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"inline template (sha256:{digest}) {preview[:120]}"
    if key == "TRANSLATION_PROMPT_TEMPLATE_FILE":
        path = Path(value)
        exists = path.exists()
        if exists:
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            first_nonempty = next((line.strip() for line in text.splitlines() if line.strip()), "")
            return f"{path} (exists, sha256:{digest}) {first_nonempty[:120]}"
        return f"{path} (missing)"
    if is_secret_like(key, value):
        return mask_text(value)
    return value


def effective_prompt_source() -> tuple[str, str]:
    template_file = os.getenv("TRANSLATION_PROMPT_TEMPLATE_FILE", "").strip()
    if template_file:
        path = Path(template_file)
        if path.exists():
            return "TRANSLATION_PROMPT_TEMPLATE_FILE", str(path)
    if DEFAULT_PROMPT_FILE.exists():
        return "default repo prompt", str(DEFAULT_PROMPT_FILE)
    inline = os.getenv("TRANSLATION_PROMPT_TEMPLATE", "").strip()
    if inline:
        return "TRANSLATION_PROMPT_TEMPLATE", inline
    return "none", ""


def main() -> int:
    load_env_file(ENV_FILE)
    print("=== translation config check ===")
    source_kind, source_value = effective_prompt_source()
    print(f"prompt source: {source_kind}")
    if source_value:
        if source_kind == "default repo prompt":
            path = Path(source_value)
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            print(f"prompt file: {path} (exists, sha256:{digest})")
        else:
            print(f"prompt ref: {display_value('TRANSLATION_PROMPT_TEMPLATE_FILE' if source_kind.endswith('FILE') else 'TRANSLATION_PROMPT_TEMPLATE', source_value)}")
    print()
    print("=== env ===")
    for key in KEYS:
        value = os.getenv(key, "")
        if not value:
            continue
        print(f"{key}={display_value(key, value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
