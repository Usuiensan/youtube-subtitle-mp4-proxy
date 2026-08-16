"""Monthly token usage tracking for paid/free-tier LLM calls."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()

TOKEN_PRICES = {
    "gemini_2_5_flash": (0.30, 2.50),
    "gemini_2_5_flash_lite": (0.25, 1.50),
    "gemini_3_5_flash_lite": (0.30, 2.50),
    "gemini_3_5_flash": (1.50, 9.00),
    "gpt_5_nano": (0.05, 0.40),
    "groq_gpt_oss_20b": (0.075, 0.30),
}


def estimate_usd(engine: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = TOKEN_PRICES.get(engine, (0.0, 0.0))
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000.0


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def record(
    path: Path,
    engine: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_usd: float,
    charged_usd: float,
    month: str | None = None,
) -> dict:
    month = month or current_month()
    with _lock:
        data = _read(path)
        month_data = data.setdefault("months", {}).setdefault(month, {})
        engines = month_data.setdefault("engines", {})
        item = engines.setdefault(
            engine,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_usd": 0.0,
                "charged_usd": 0.0,
            },
        )
        item["input_tokens"] += max(0, int(input_tokens))
        item["output_tokens"] += max(0, int(output_tokens))
        item["total_tokens"] += max(0, int(total_tokens))
        item["estimated_usd"] = estimate_usd(
            engine,
            item["input_tokens"],
            item["output_tokens"],
        )
        item["charged_usd"] += max(0.0, float(charged_usd))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary(path, engine=engine, month=month)


def summary(path: Path, engine: str | None = None, month: str | None = None) -> dict:
    month = month or current_month()
    with _lock:
        data = _read(path)
    engines = (data.get("months") or {}).get(month, {}).get("engines") or {}
    if engine:
        engines = {engine: engines.get(engine, {})}
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_usd": 0.0,
        "charged_usd": 0.0,
    }
    for engine_name, item in engines.items():
        for key in totals:
            if key != "estimated_usd":
                totals[key] += item.get(key, 0)
        totals["estimated_usd"] += estimate_usd(
            engine_name,
            int(item.get("input_tokens") or 0),
            int(item.get("output_tokens") or 0),
        )
    return {"month": month, "engine": engine, **totals, "engines": engines}
