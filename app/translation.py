from __future__ import annotations

import html
import json
import math
import re
import shutil
import sys
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
    context_before_seconds: int
    context_before_max_events: int
    context_after_seconds: int
    context_after_max_events: int
    model_name: str
    engine: str
    fallback_engine: str
    glossary: str
    topic: str
    prompt_template: str
    google_project: str
    provider_name: str


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
    context_before_seconds: int,
    context_before_max_events: int,
    context_after_seconds: int,
    context_after_max_events: int,
    previous_japanese: list[srt.Subtitle],
) -> dict[str, list[dict[str, Any]]]:
    target_start = target[0].start
    target_end = target[-1].end
    before_start = target_start - timedelta(seconds=context_before_seconds)
    after_end = target_end + timedelta(seconds=context_after_seconds)
    previous_start = target_start - timedelta(seconds=context_before_seconds)

    # Get context before (sorted by start time, latest first, then limited, then sorted chronologically)
    before_events = [
        sub for sub in all_subtitles
        if before_start <= sub.start < target_start
    ]
    before_events = sorted(before_events, key=lambda s: s.start, reverse=True)[:context_before_max_events]
    before_events = sorted(before_events, key=lambda s: s.start)

    # Get context after (sorted by start time, earliest first, then limited)
    after_events = [
        sub for sub in all_subtitles
        if target_end < sub.start <= after_end
    ]
    after_events = sorted(after_events, key=lambda s: s.start)[:context_after_max_events]

    # Get previous japanese (sorted by start time, latest first, then limited, then sorted chronologically)
    prev_ja_events = [
        sub for sub in previous_japanese
        if previous_start <= sub.start < target_start
    ]
    prev_ja_events = sorted(prev_ja_events, key=lambda s: s.start, reverse=True)[:context_before_max_events]
    prev_ja_events = sorted(prev_ja_events, key=lambda s: s.start)

    return {
        "context_before": [event_to_json(sub) for sub in before_events],
        "context_after": [event_to_json(sub) for sub in after_events],
        "previous_japanese": [event_to_json(sub) for sub in prev_ja_events],
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
    channel_name: str,
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
        settings.context_before_seconds,
        settings.context_before_max_events,
        settings.context_after_seconds,
        settings.context_after_max_events,
        previous_japanese,
    )
    return {
        "video_title": video_title,
        "channel_name": channel_name,
        "topic": settings.topic,
        "glossary": settings.glossary,
        "source_language": source_language,
        "target_language": target_language,
        "translation_provider": settings.provider_name,
        "translation_profile": settings.engine,
        "model_name": settings.model_name,
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


def discard_successful_attempt(result: dict[str, Any]) -> None:
    attempt_dir = result.get("_translation_attempt_dir")
    if isinstance(attempt_dir, str) and attempt_dir:
        shutil.rmtree(attempt_dir, ignore_errors=True)


def mark_failed_attempt(result: dict[str, Any], error: Exception) -> None:
    attempt_dir = result.get("_translation_attempt_dir")
    if not isinstance(attempt_dir, str) or not attempt_dir:
        return
    try:
        Path(attempt_dir).mkdir(parents=True, exist_ok=True)
        (Path(attempt_dir) / "validation-error.txt").write_text(
            f"{type(error).__name__}: {error}",
            encoding="utf-8",
        )
    except Exception:
        pass


async def translate_srt_with_local_worker(
    *,
    subtitle_path: Path,
    output_path: Path,
    video_title: str,
    channel_name: str,
    source_language: str,
    target_language: str,
    settings: TranslationSettings,
    run_worker: Callable[[dict[str, Any]], Any],
    on_progress: Callable[[int, int], None] | None = None,
) -> SubtitleTranslationResult:
    subtitles = load_srt(subtitle_path)
    translation_characters = sum(len(sub.content) for sub in subtitles)
    windows = window_subtitles(
        subtitles,
        settings.target_window_seconds,
        settings.target_max_events,
    )
    translated_subtitles: list[srt.Subtitle] = []
    fallback_used = False
    total_windows = len(windows)
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }

    def add_usage(result: dict[str, Any]) -> None:
        usage = result.get("_usage")
        if not isinstance(usage, dict):
            return
        for source_key, target_key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = usage.get(source_key)
            if isinstance(value, (int, float)):
                usage_totals[target_key] += int(value)
        usage_totals["requests"] += 1

    for index, window in enumerate(windows):
        if on_progress:
            on_progress(index, total_windows)
        translated_map: dict[str, str] | None = None
        last_error: Exception | None = None
        for strict in (False, True):
            try:
                payload = build_worker_payload(
                    video_title=video_title,
                    channel_name=channel_name,
                    source_language=source_language,
                    target_language=target_language,
                    target=window,
                    all_subtitles=subtitles,
                    previous_japanese=translated_subtitles,
                    settings=settings,
                    strict=strict,
                )
                result = await run_worker(payload)
                add_usage(result)
                try:
                    translated_map = validate_translations(window, result)
                except Exception as error:
                    mark_failed_attempt(result, error)
                    raise
                discard_successful_attempt(result)
                break
            except Exception as error:
                last_error = error
                print(
                    (
                        "local LLM translation attempt failed: "
                        f"window={index + 1}/{total_windows} strict={strict} "
                        f"source={source_language} target={target_language} "
                        f"error={type(error).__name__}: {error}"
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                translated_map = None

        if translated_map is None and len(window) > 1:
            translated_map = {}
            midpoint = len(window) // 2
            for part_index, part in enumerate((window[:midpoint], window[midpoint:]), start=1):
                payload = build_worker_payload(
                    video_title=video_title,
                    channel_name=channel_name,
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
                    add_usage(result)
                    try:
                        translated_map.update(validate_translations(part, result))
                    except Exception as error:
                        mark_failed_attempt(result, error)
                        raise
                    discard_successful_attempt(result)
                except Exception as error:
                    last_error = error
                    print(
                        (
                            "local LLM translation split retry failed: "
                            f"window={index + 1}/{total_windows} part={part_index}/2 "
                            f"source={source_language} target={target_language} "
                            f"error={type(error).__name__}: {error}"
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    translated_map = None
                    break

        if translated_map is None:
            raise TranslationError(
                "remote LLM translation failed; Google fallback is disabled. "
                f"window={index + 1}/{total_windows} source={source_language} "
                f"target={target_language} last_error={type(last_error).__name__ if last_error else 'unknown'}: {last_error}"
            )

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
            "translation_characters": translation_characters,
            "translation_input_tokens": usage_totals["input_tokens"],
            "translation_output_tokens": usage_totals["output_tokens"],
            "translation_total_tokens": usage_totals["total_tokens"],
            "translation_request_count": usage_totals["requests"],
            "translation_billing_class": "local",
        },
    )


def google_translate_events(
    subtitles: list[srt.Subtitle],
    target_language: str,
    settings: TranslationSettings,
) -> dict[str, str]:
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
