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
        if start_cue not in indexes or (starts and start_cue <= starts[-1]):
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
    for number, (start_cue, text) in enumerate(zip(starts, texts), start=1):
        start_position = spans[start_cue][0]
        end_position = spans[starts[number]][0] if number < len(starts) else len(source_text)
        result.append(srt.Subtitle(number, time_at(start_position), time_at(end_position), text))
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
    payload = {
        "task": "adjust_auto_captions", "source_language": source_language,
        "video_title": video_title, "channel_name": channel_name, "windows": payload_windows,
    }
    adjusted: list[srt.Subtitle] = []
    for number, (window, expected) in enumerate(zip(windows, payload_windows), start=1):
        try:
            response = _call(llm_call, {
                "task": "adjust_auto_captions", "source_language": source_language,
                "video_title": video_title, "channel_name": channel_name, "windows": [expected],
            })
            raw_windows = response.get("windows")
            if not isinstance(raw_windows, list) or len(raw_windows) != 1:
                raise ValueError("invalid windows response")
            item = raw_windows[0]
            usage = _usage(response)
            for key in report["usage"]:
                report["usage"][key] += usage[key]
            if not isinstance(item, dict) or item.get("window_id") != expected["window_id"]:
                raise ValueError("window_id mismatch")
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
