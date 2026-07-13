"""Filesystem layout for hot and archived prepared-video cache entries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheLayout:
    hot_root: Path
    archive_root: Path | None = None

    def entry_dir(self, key: str) -> Path:
        return self.hot_root / key

    def archive_entry_dir(self, key: str) -> Path | None:
        return self.archive_root / key if self.archive_root is not None else None

    def output_path(self, key: str) -> Path:
        return self.entry_dir(key) / "output.mp4"

    def hls_dir(self, key: str) -> Path:
        return self.entry_dir(key) / "hls"

    def hls_playlist_path(self, key: str) -> Path:
        return self.hls_dir(key) / "index.m3u8"

    def meta_path(self, key: str) -> Path:
        return self.entry_dir(key) / "meta.json"

    def source_dir(self, key: str) -> Path:
        return self.entry_dir(key) / "source"

    def source_meta_path(self, key: str) -> Path:
        return self.entry_dir(key) / "source.json"

    def translation_meta_path(self, key: str) -> Path:
        return self.source_dir(key) / "translation.json"
