"""Configuration file loading helpers."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding process environment."""
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


def read_text_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        file_path = Path(path)
        if not file_path.exists():
            return ""
        return file_path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_translation_prompt_file() -> Path:
    return repository_root() / "prompts" / "translation-prompt.txt"


def default_translategemma_prompt_file() -> Path:
    return repository_root() / "prompts" / "translategemma-prompt.txt"
