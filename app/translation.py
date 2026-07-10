from __future__ import annotations

import html
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import srt


@dataclass
class TranslationSettings:
    enabled: bool
    target_window_seconds: int
    target_max_events: int
    context_seconds: int
    model_name: str
    engine: str
    fallback_engine: str
    glossary: str
    topic: str
    google_project: str


@dataclass
class SubtitleTranslationResult:
    subtitle_path: Path
    metadata: dict[str, Any]


class TranslationError(Exception):
    pass


def load_srt(path: Path) -> list[srt.Subtitle]:
    return list(srt.parse(path.read_text(encoding="utf-8-sig")))


def save_srt(path: Path, subtitles: list[srt.Subtitle]) -> None:
    path.write_text(srt.compose(subtitles), encoding="utf-8")


def seconds(value: timedelta) -> float:
    return value.total_seconds()


def window_subtitles(
    subtitles: list[srt.Subtitle],
    max_seconds: int,
    max_events: int,
) -> list[list[srt.Subtitle]]:
    windows: list[list[srt.Subtitle]] = []
    current: list[srt.Subtitle] = []
    start_second: float | None = None

    for subtitle in subtitles:
        subtitle_start = seconds(subtitle.start)
        if start_second is None:
            start_second = subtitle_start
        should_split = (
            current
            and (
                len(current) >= max_events
                or subtitle_start - start_second >= max_seconds
            )
        )
        if should_split:
            windows.append(current)
            current = []
            start_second = subtitle_start
        current.append(subtitle)

    if current:
        windows.append(current)
    return windows


def context_for_window(
    all_subtitles: list[srt.Subtitle],
    target: list[srt.Subtitle],
    context_seconds: int,
    previous_japanese: list[srt.Subtitle],
) -> dict[str, list[dict[str, Any]]]:
    target_start = target[0].start
    target_end = target[-1].end
    before_start = target_start - timedelta(seconds=context_seconds)
    after_end = target_end + timedelta(seconds=context_seconds)
    previous_start = target_start - timedelta(seconds=context_seconds)

    return {
        "context_before": [
            event_to_json(sub)
            for sub in all_subtitles
            if before_start <= sub.start < target_start
        ],
        "context_after": [
            event_to_json(sub)
            for sub in all_subtitles
            if target_end < sub.start <= after_end
        ],
        "previous_japanese": [
            event_to_json(sub)
            for sub in previous_japanese
            if previous_start <= sub.start < target_start
        ],
    }


def event_to_json(subtitle: srt.Subtitle) -> dict[str, Any]:
    return {
        "id": str(subtitle.index),
        "start": str(subtitle.start),
        "end": str(subtitle.end),
        "text": subtitle.content,
    }


def build_worker_payload(
    *,
    video_title: str,
    source_language: str,
    target_language: str,
    target: list[srt.Subtitle],
    all_subtitles: list[srt.Subtitle],
    previous_japanese: list[srt.Subtitle],
    settings: TranslationSettings,
    strict: bool = False,
) -> dict[str, Any]:
    context = context_for_window(
        all_subtitles,
        target,
        settings.context_seconds,
        previous_japanese,
    )
    return {
        "video_title": video_title,
        "topic": settings.topic,
        "glossary": settings.glossary,
        "source_language": source_language,
        "target_language": target_language,
        "strict": strict,
        "context_before": context["context_before"],
        "target": [event_to_json(sub) for sub in target],
        "context_after": context["context_after"],
        "previous_japanese": context["previous_japanese"],
    }


def validate_translations(
    target: list[srt.Subtitle],
    result: dict[str, Any],
) -> dict[str, str]:
    translations = result.get("translations")
    if not isinstance(translations, list):
        raise TranslationError("translations must be an array")

    expected_ids = [str(sub.index) for sub in target]
    expected_set = set(expected_ids)
    seen: set[str] = set()
    output: dict[str, str] = {}

    for item in translations:
        if not isinstance(item, dict):
            raise TranslationError("translation item must be an object")
        item_id = str(item.get("id", ""))
        text = str(item.get("text", "")).strip()
        if item_id not in expected_set:
            raise TranslationError(f"unexpected subtitle id: {item_id}")
        if item_id in seen:
            raise TranslationError(f"duplicate subtitle id: {item_id}")
        if not text:
            raise TranslationError(f"empty translation: {item_id}")
        if len(text) > max(400, len(target[expected_ids.index(item_id)].content) * 8):
            raise TranslationError(f"translation too long: {item_id}")
        seen.add(item_id)
        output[item_id] = text

    if seen != expected_set:
        raise TranslationError("missing subtitle ids")

    for sub in target:
        original = sub.content
        translated = output[str(sub.index)]
        for url in re.findall(r"https?://\S+", original):
            if url not in translated:
                raise TranslationError(f"url disappeared: {sub.index}")
        original_numbers = re.findall(r"\d+", original)
        if len(original_numbers) >= 2:
            translated_numbers = re.findall(r"\d+", translated)
            if len(translated_numbers) < math.floor(len(original_numbers) / 2):
                raise TranslationError(f"numbers disappeared: {sub.index}")

    return output


async def translate_srt_with_local_worker(
    *,
    subtitle_path: Path,
    output_path: Path,
    video_title: str,
    source_language: str,
    target_language: str,
    settings: TranslationSettings,
    run_worker: Callable[[dict[str, Any]], Any],
) -> SubtitleTranslationResult:
    subtitles = load_srt(subtitle_path)
    windows = window_subtitles(
        subtitles,
        settings.target_window_seconds,
        settings.target_max_events,
    )
    translated_subtitles: list[srt.Subtitle] = []
    fallback_used = False

    for window in windows:
        translated_map: dict[str, str] | None = None
        for strict in (False, True):
            try:
                payload = build_worker_payload(
                    video_title=video_title,
                    source_language=source_language,
                    target_language=target_language,
                    target=window,
                    all_subtitles=subtitles,
                    previous_japanese=translated_subtitles,
                    settings=settings,
                    strict=strict,
                )
                result = await run_worker(payload)
                translated_map = validate_translations(window, result)
                break
            except Exception:
                translated_map = None

        if translated_map is None and len(window) > 1:
            translated_map = {}
            midpoint = len(window) // 2
            for part in (window[:midpoint], window[midpoint:]):
                payload = build_worker_payload(
                    video_title=video_title,
                    source_language=source_language,
                    target_language=target_language,
                    target=part,
                    all_subtitles=subtitles,
                    previous_japanese=translated_subtitles,
                    settings=settings,
                    strict=True,
                )
                try:
                    result = await run_worker(payload)
                    translated_map.update(validate_translations(part, result))
                except Exception:
                    translated_map = None
                    break

        if translated_map is None:
            fallback_used = True
            translated_map = google_translate_events(window, target_language, settings)

        for sub in window:
            translated_subtitles.append(
                srt.Subtitle(
                    index=sub.index,
                    start=sub.start,
                    end=sub.end,
                    content=translated_map[str(sub.index)],
                    proprietary=sub.proprietary,
                )
            )

    save_srt(output_path, translated_subtitles)
    engine = "google_cloud" if fallback_used else settings.engine
    if translated_subtitles:
        notice = f"{source_language}>>{target_language} ({engine})"
        translated_subtitles[0] = srt.Subtitle(
            index=translated_subtitles[0].index,
            start=translated_subtitles[0].start,
            end=translated_subtitles[0].end,
            content=f"{notice}\n{translated_subtitles[0].content}",
            proprietary=translated_subtitles[0].proprietary,
        )
        save_srt(output_path, translated_subtitles)
    return SubtitleTranslationResult(
        subtitle_path=output_path,
        metadata={
            "requested_language": target_language,
            "source_language": source_language,
            "translated": True,
            "translation_engine": engine,
            "translation_model": settings.model_name if engine == settings.engine else None,
            "translation_fallback_used": fallback_used,
            "translation_created_at": int(time.time()),
        },
    )


def google_translate_events(
    subtitles: list[srt.Subtitle],
    target_language: str,
    settings: TranslationSettings,
) -> dict[str, str]:
    if settings.fallback_engine != "google_cloud":
        raise TranslationError("Google Cloud fallback is disabled")
    if not settings.google_project:
        raise TranslationError("GOOGLE_CLOUD_PROJECT is not configured")

    from google.cloud import translate_v3 as translate

    client = translate.TranslationServiceClient()
    parent = f"projects/{settings.google_project}/locations/global"
    output: dict[str, str] = {}
    for sub in subtitles:
        text = html.unescape(sub.content)
        response = client.translate_text(
            request={
                "parent": parent,
                "contents": [text],
                "mime_type": "text/plain",
                "target_language_code": target_language,
            }
        )
        output[str(sub.index)] = html.unescape(response.translations[0].translated_text)
    return output
