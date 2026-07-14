"""Monthly Google Cloud Translation character usage tracking."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


FREE_TIER_CHARACTERS = 500_000
_lock = threading.RLock()


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def record(path: Path, characters: int, month: str | None = None) -> dict:
    month = month or current_month()
    characters = max(0, int(characters))
    with _lock:
        data = _read(path)
        months = data.setdefault("months", {})
        months[month] = int(months.get(month, 0)) + characters
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary(path, month=month)


def summary(path: Path, month: str | None = None) -> dict:
    month = month or current_month()
    with _lock:
        data = _read(path)
    used = max(0, int((data.get("months") or {}).get(month, 0)))
    return {
        "month": month,
        "used_characters": used,
        "free_tier_characters": FREE_TIER_CHARACTERS,
        "remaining_characters": max(0, FREE_TIER_CHARACTERS - used),
        "usage_percent": round(used / FREE_TIER_CHARACTERS * 100, 2),
    }
