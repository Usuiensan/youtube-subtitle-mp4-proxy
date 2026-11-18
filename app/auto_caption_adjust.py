from __future__ import annotations

import asyncio
import difflib
import inspect
import re
from datetime import timedelta
from typing import Any, Callable

import srt

from app.translation import normalize_subtitle_text


WINDOW_SECONDS = 75
SIMILARITY_THRESHOLD = 0.6
WINDOWS_PER_BATCH = 4
MAX_CUES_PER_BATCH = 150


def _batches(
    windows: list[list[tuple[int, srt.Subtitle]]], payload_windows: list[dict]
) -> list[list[tuple[list[tuple[int, srt.Subtitle]], dict]]]:
    result: list[list[tuple[list[tuple[int, srt.Subtitle]], dict]]] = []
    current: list[tuple[list[tuple[int, srt.Subtitle]], dict]] = []
    cue_count = 0
    for window, expected in zip(windows, payload_windows):
        if current and (len(current) >= WINDOWS_PER_BATCH or cue_count + len(window) > MAX_CUES_PER_BATCH):
            result.append(current)
            current = []
            cue_count = 0
        current.append((window, expected))
        cue_count += len(window)
    if current:
        result.append(current)
    return result


def _normalized_for_similarity(text: str) -> str:
    return re.sub(r"[\s\W_]", "", text, flags=re.UNICODE)


def _windows(subtitles: list[srt.Subtitle]) -> list[list[tuple[int, srt.Subtitle]]]:
    result: list[list[tuple[int, srt.Subtitle]]] = []
    current: list[tuple[int, srt.Subtitle]] = []
    window_start = 0.0
    for index, subtitle in enumerate(subtitles):
        end = subtitle.end.total_seconds()
        if current and end - window_start > WINDOW_SECONDS:
            result.append(current)
            current = []
        if not current:
            window_start = subtitle.start.total_seconds()
        current.append((index, subtitle))
    if current:
        result.append(current)
    return result


def _usage(result: dict[str, Any]) -> dict[str, int]:
    raw = result.get("_usage") if isinstance(result.get("_usage"), dict) else {}
    return {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "total_tokens": int(raw.get("total_tokens") or 0),
    }


def _call(llm_call: Callable[[dict], Any], payload: dict) -> dict:
    result = llm_call(payload)
    if inspect.isawaitable(result):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(result)
        else:
            raise RuntimeError("async llm_call must be invoked outside an active event loop")
    if not isinstance(result, dict):
        raise ValueError("LLM response must be an object")
    return result


def _rebuild(window: list[tuple[int, srt.Subtitle]], sentences: Any) -> list[srt.Subtitle]:
    if not isinstance(sentences, list) or not sentences:
        raise ValueError("sentences must be a non-empty array")
    indexes = [index for index, _ in window]
    starts: list[int] = []
    texts: list[str] = []
    for sentence in sentences:
        if not isinstance(sentence, dict) or not isinstance(sentence.get("start_cue"), int):
            raise ValueError("invalid sentence")
        start_cue = sentence["start_cue"]
        if start_cue not in indexes or (starts and start_cue < starts[-1]):
            raise ValueError("invalid sentence start_cue")
        text = str(sentence.get("text") or "").strip()
        if not text:
            raise ValueError("empty sentence")
        starts.append(start_cue)
        texts.append(text)
    if starts[0] != indexes[0]:
        raise ValueError("sentences do not cover the window")

    source_text = "".join(normalize_subtitle_text(item.content, compact=True) for _, item in window)
    spans: dict[int, tuple[int, int, srt.Subtitle]] = {}
    position = 0
    for index, item in window:
        text = normalize_subtitle_text(item.content, compact=True)
        spans[index] = (position, position + len(text), item)
        position += len(text)

    def time_at(char_position: int) -> timedelta:
        for start, end, item in spans.values():
            if char_position <= end:
                fraction = (char_position - start) / max(1, end - start)
                duration = (item.end - item.start).total_seconds()
                return item.start + timedelta(seconds=duration * max(0.0, min(1.0, fraction)))
        return window[-1][1].end

    result: list[srt.Subtitle] = []
    position = 0
    while position < len(starts):
        group_end = position
        while group_end + 1 < len(starts) and starts[group_end + 1] == starts[position]:
            group_end += 1
        span_start = spans[starts[position]][0]
        span_end = spans[starts[group_end + 1]][0] if group_end + 1 < len(starts) else len(source_text)
        group_texts = texts[position:group_end + 1]
        total_length = sum(len(text) for text in group_texts) or 1
        cursor = float(span_start)
        for text in group_texts:
            next_cursor = cursor + (span_end - span_start) * len(text) / total_length
            result.append(
                srt.Subtitle(len(result) + 1, time_at(round(cursor)), time_at(round(next_cursor)), text)
            )
            cursor = next_cursor
        position = group_end + 1
    return result


def adjust_auto_captions(
    subtitles: list[srt.Subtitle], *, source_language: str, llm_call: Callable[[dict], Any],
    model_name: str = "", video_title: str = "", channel_name: str = ""
) -> tuple[list[srt.Subtitle], dict]:
    windows = _windows(subtitles)
    payload_windows = [
        {"window_id": f"w{number:03d}", "cues": [
            {"index": index, "text": normalize_subtitle_text(item.content, compact=True)}
            for index, item in window
        ]}
        for number, window in enumerate(windows, start=1)
    ]
    report = {
        "windows_total": len(windows), "windows_adjusted": 0, "windows_fallback": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model_name": model_name,
    }
    if not windows:
        return [], report
    adjusted: list[srt.Subtitle] = []
    for batch in _batches(windows, payload_windows):
        batch_windows = [window for window, _ in batch]
        batch_expected = [expected for _, expected in batch]
        items_by_id: dict[str, Any] = {}
        try:
            response = _call(llm_call, {
                "task": "adjust_auto_captions", "source_language": source_language,
                "video_title": video_title, "channel_name": channel_name, "windows": batch_expected,
            })
            raw_windows = response.get("windows")
            if not isinstance(raw_windows, list):
                raise ValueError("invalid windows response")
            usage = _usage(response)
            for key in report["usage"]:
                report["usage"][key] += usage[key]
            items_by_id = {
                item["window_id"]: item
                for item in raw_windows
                if isinstance(item, dict) and isinstance(item.get("window_id"), str)
            }
        except Exception:
            items_by_id = {}
        for window, expected in zip(batch_windows, batch_expected):
            try:
                item = items_by_id.get(expected["window_id"])
                if item is None:
                    raise ValueError("window missing from response")
                candidate = _rebuild(window, item.get("sentences"))
                original = "".join(cue["text"] for cue in expected["cues"])
                adjusted_text = "".join(subtitle.content for subtitle in candidate)
                similarity = difflib.SequenceMatcher(
                    None, _normalized_for_similarity(original), _normalized_for_similarity(adjusted_text)
                ).ratio()
                if similarity < SIMILARITY_THRESHOLD:
                    raise ValueError("adjusted text is too dissimilar")
                adjusted.extend(candidate)
                report["windows_adjusted"] += 1
            except Exception:
                adjusted.extend(item for _, item in window)
                report["windows_fallback"] += 1
    if report["windows_adjusted"] == 0:
        return list(subtitles), report
    for index, item in enumerate(adjusted, start=1):
        item.index = index
    return adjusted, report
