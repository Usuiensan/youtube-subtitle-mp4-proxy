"""Defensive JSON file helpers used by cache metadata readers."""

from __future__ import annotations

import json
from pathlib import Path


def read_json_object(path: Path) -> dict:
    """Read a JSON object, returning an empty mapping for invalid input."""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}
