"""Persistent conversion metrics used for ETA estimates.

The metrics file is intentionally best-effort: a corrupt or unavailable
metrics file must never make video preparation fail.
"""

from __future__ import annotations

import json
from pathlib import Path


_EMPTY_DATA = {
    "download_speed": [],
    "encode_speed_ratio": [],
    "translate_speed": [],
    "archive_speed": [],
}


class MetricsManager:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.data = {key: list(values) for key, values in _EMPTY_DATA.items()}
        self.load()

    def load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            loaded = json.loads(self.file_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(
                    {
                        key: value
                        for key, value in loaded.items()
                        if key in _EMPTY_DATA and isinstance(value, list)
                    }
                )
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _record(self, key: str, value: float, time_seconds: float) -> None:
        if time_seconds > 0:
            self.data.setdefault(key, []).append(value / time_seconds)
            self.save()

    def record_download(self, size_bytes: float, time_seconds: float) -> None:
        self._record("download_speed", size_bytes, time_seconds)

    def record_encode(self, duration: float, time_seconds: float) -> None:
        self._record("encode_speed_ratio", duration, time_seconds)

    def record_translate(self, events_count: int, time_seconds: float) -> None:
        self._record("translate_speed", events_count, time_seconds)

    def record_archive(self, size_bytes: float, time_seconds: float) -> None:
        self._record("archive_speed", size_bytes, time_seconds)

    def get_avg(self, key: str, fallback: float) -> float:
        values = self.data.get(key)
        if not values:
            return fallback
        recent = values[-50:]
        return sum(recent) / len(recent)

    def reset(self) -> None:
        self.data = {key: list(values) for key, values in _EMPTY_DATA.items()}
        self.save()
