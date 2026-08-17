from __future__ import annotations

import shutil
from pathlib import Path
import srt

from app.translation import normalize_subtitle_text


_TRANSIENT_CUE_SECONDS = 0.2
_MIN_TRANSIENT_RATIO = 0.2
_MIN_TRANSIENT_RELATION_RATIO = 0.7


def raw_subtitle_sidecar(path: Path) -> Path:
    return path.with_name(f"{path.stem}.raw{path.suffix}")


def _compact(subtitle: srt.Subtitle) -> str:
    return normalize_subtitle_text(subtitle.content, compact=True)


def _is_transient(subtitle: srt.Subtitle) -> bool:
    return (subtitle.end - subtitle.start).total_seconds() < _TRANSIENT_CUE_SECONDS


def _transient_is_related(subtitles: list[srt.Subtitle], position: int) -> bool:
    text = _compact(subtitles[position])
    if not text:
        return True
    for neighbor_position in (position - 1, position + 1):
        if not 0 <= neighbor_position < len(subtitles):
            continue
        neighbor = _compact(subtitles[neighbor_position])
        if text in neighbor or neighbor in text:
            return True
    return False


def is_word_incremental_subtitles(subtitles: list[srt.Subtitle]) -> bool:
    if len(subtitles) < 4:
        return False
    transient_positions = [
        position for position, subtitle in enumerate(subtitles) if _is_transient(subtitle)
    ]
    if len(transient_positions) / len(subtitles) < _MIN_TRANSIENT_RATIO:
        return False
    related = sum(_transient_is_related(subtitles, position) for position in transient_positions)
    return related / len(transient_positions) >= _MIN_TRANSIENT_RELATION_RATIO


def _words(text: str) -> list[str]:
    return text.split()


def _remove_rolling_overlap(previous: str, current: str) -> str:
    previous_words = _words(previous)
    current_words = _words(current)
    for size in range(min(len(previous_words), len(current_words)), 1, -1):
        if previous_words[-size:] == current_words[:size]:
            return " ".join(current_words[size:])
    return current


def normalize_word_incremental_subtitles(subtitles: list[srt.Subtitle]) -> list[srt.Subtitle] | None:
    if not is_word_incremental_subtitles(subtitles):
        return None
    kept = [subtitle for position, subtitle in enumerate(subtitles) if not _is_transient(subtitle)]
    normalized: list[srt.Subtitle] = []
    previous_text = ""
    for subtitle in kept:
        text = _compact(subtitle)
        if not text:
            continue
        delta = _remove_rolling_overlap(previous_text, text) if previous_text else text
        if delta:
            normalized.append(
                srt.Subtitle(
                    index=len(normalized) + 1,
                    start=subtitle.start,
                    end=subtitle.end,
                    content=delta,
                    proprietary=subtitle.proprietary,
                )
            )
        previous_text = text
    return normalized


def preprocess_downloaded_subtitle(path: Path) -> Path:
    subtitles = list(srt.parse(path.read_text(encoding="utf-8-sig")))
    normalized = normalize_word_incremental_subtitles(subtitles)
    if normalized is None:
        return path
    shutil.copy2(path, raw_subtitle_sidecar(path))
    path.write_text(srt.compose(normalized), encoding="utf-8")
    return path
