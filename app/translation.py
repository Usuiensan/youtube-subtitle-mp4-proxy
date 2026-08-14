from __future__ import annotations

import html
import math
import re
import shutil
import time
from dataclasses import dataclass
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
    provider_endpoint: str
    provider_api_key: str


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


def build_worker_payload(
    *,
    video_title: str,
    channel_name: str,
    source_language: str,
    target_language: str,
    all_subtitles: list[srt.Subtitle],
    settings: TranslationSettings,
) -> dict[str, Any]:
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
        "subtitles": [{"id": sub.index, "text": sub.content} for sub in all_subtitles],
    }


def validate_translations(
    target: list[srt.Subtitle],
    result: dict[str, Any],
) -> dict[str, str]:
    translations = result.get("subtitles")
    if not isinstance(translations, list):
        raise TranslationError("subtitles must be an array")

    expected_ids = [str(sub.index) for sub in target]
    expected_set = set(expected_ids)
    if len(translations) != len(expected_ids):
        raise TranslationError(
            f"subtitle count mismatch: expected {len(expected_ids)}, got {len(translations)}"
        )
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
    on_progress: Callable[[int, int, list[dict[str, str]] | None], None] | None = None,
) -> SubtitleTranslationResult:
    subtitles = load_srt(subtitle_path)
    translation_characters = sum(len(sub.content) for sub in subtitles)
    translated_subtitles: list[srt.Subtitle] = []
    recent_pairs: list[dict[str, str]] = []
    total_subtitles = len(subtitles)
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 1,
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

    if on_progress:
        on_progress(0, total_subtitles, recent_pairs)

    payload = build_worker_payload(
        video_title=video_title,
        channel_name=channel_name,
        source_language=source_language,
        target_language=target_language,
        all_subtitles=subtitles,
        settings=settings,
    )
    try:
        result = await run_worker(payload)
        add_usage(result)
        translated_map = validate_translations(subtitles, result)
    except Exception as error:
        if isinstance(error, TranslationError):
            raise
        raise TranslationError(
            "remote LLM translation failed: "
            f"source={source_language} target={target_language} "
            f"error={type(error).__name__}: {error}"
        ) from error
    discard_successful_attempt(result)

    for sub in subtitles:
        translated_text = translated_map[str(sub.index)]
        translated_subtitles.append(
            srt.Subtitle(
                index=sub.index,
                start=sub.start,
                end=sub.end,
                content=translated_text,
                proprietary=sub.proprietary,
            )
        )
        recent_pairs.append({"source": sub.content, "translated": translated_text})
        recent_pairs = recent_pairs[-5:]
    if on_progress:
        on_progress(total_subtitles, total_subtitles, recent_pairs)

    save_srt(output_path, translated_subtitles)
    return SubtitleTranslationResult(
        subtitle_path=output_path,
        metadata={
            "requested_language": target_language,
            "source_language": source_language,
            "translated": True,
            "translation_engine": settings.engine,
            "translation_model": settings.model_name,
            "translation_fallback_used": False,
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
