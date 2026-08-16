from __future__ import annotations

import html
import json
import math
import os
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


def normalize_subtitle_text(text: str, *, compact: bool = False) -> str:
    normalized = re.sub(r"\\+[nNhH]", " ", str(text))
    normalized = re.sub(r"\\+\r?\n", "\n", normalized)
    normalized = re.sub(r"\\+(?=\s)", " ", normalized)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normalized.split()) if compact else normalized


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
    video_id: str = "",
) -> dict[str, Any]:
    return {
        "video_id": video_id,
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


def _chunk_input_token_limit() -> int:
    return max(512, int(os.getenv("TRANSLATION_CHUNK_INPUT_TOKENS", "3500")))


def _chunk_input_token_estimate(payload: dict[str, Any]) -> int:
    # Keep a conservative fixed allowance for the prompt instructions and OpenAI schema.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return math.ceil((len(encoded) + 3072) / 3)


def chunk_translation_subtitles(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    subtitles = payload.get("subtitles")
    if str(payload.get("translation_provider") or "") not in {"openai_api", "gemini_api"} or not isinstance(subtitles, list):
        return [subtitles] if isinstance(subtitles, list) else []
    limit = _chunk_input_token_limit()
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in subtitles:
        if not isinstance(item, dict):
            raise TranslationError("subtitles must be an array of objects")
        candidate = [*current, item]
        if current and _chunk_input_token_estimate({**payload, "subtitles": candidate}) > limit:
            chunks.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _recent_translation_pairs(
    subtitles: list[srt.Subtitle],
    segments: list[dict[str, Any]],
) -> list[dict[str, str]]:
    positions = {str(sub.index): position for position, sub in enumerate(subtitles)}
    return [
        {
            "source": " ".join(
                sub.content
                for sub in subtitles[positions[segment["from_id"]] : positions[segment["to_id"]] + 1]
            ),
            "translated": segment["text"],
        }
        for segment in segments
    ]


def _chunk_error_can_be_split(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "invalid json",
            "no json content",
            "truncated",
            "omitted subtitle id",
            "missing subtitle range",
            "subtitle range must continue",
        )
    )


def validate_translations(
    target: list[srt.Subtitle],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    translations = result.get("subtitles")
    if not isinstance(translations, list):
        raise TranslationError("subtitles must be an array")

    expected_ids = [str(sub.index) for sub in target]
    expected_positions = {item_id: position for position, item_id in enumerate(expected_ids)}
    next_position = 0
    output: list[dict[str, Any]] = []

    for item in translations:
        if not isinstance(item, dict):
            raise TranslationError("translation item must be an object")
        from_id = str(item.get("from_id", ""))
        to_id = str(item.get("to_id", ""))
        text = str(item.get("text", "")).strip()
        if from_id not in expected_positions or to_id not in expected_positions:
            raise TranslationError(f"unexpected subtitle range: {from_id}-{to_id}")
        from_position = expected_positions[from_id]
        to_position = expected_positions[to_id]
        if from_position != next_position or to_position < from_position:
            expected_from_id = expected_ids[next_position] if next_position < len(expected_ids) else "end"
            raise TranslationError(
                f"subtitle range must continue at {expected_from_id}: got {from_id}-{to_id}"
            )
        if not text:
            raise TranslationError(f"empty translation: {from_id}-{to_id}")
        output.append({"from_id": from_id, "to_id": to_id, "text": text})
        next_position = to_position + 1

    if next_position != len(target):
        missing_id = expected_ids[next_position] if next_position < len(target) else "end"
        raise TranslationError(f"missing subtitle range starting at: {missing_id}")

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
    video_id: str = "",
) -> SubtitleTranslationResult:
    subtitles = load_srt(subtitle_path)
    translation_characters = sum(len(sub.content) for sub in subtitles)
    translated_subtitles: list[srt.Subtitle] = []
    recent_pairs: list[dict[str, str]] = []
    total_subtitles = len(subtitles)
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "billable_output_tokens": 0,
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
            ("thinking_tokens", "thinking_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = usage.get(source_key)
            if isinstance(value, (int, float)):
                usage_totals[target_key] += int(value)
        billable_output_tokens = usage.get("billable_output_tokens")
        if not isinstance(billable_output_tokens, (int, float)):
            billable_output_tokens = max(
                int(usage.get("output_tokens") or 0) + int(usage.get("thinking_tokens") or 0),
                int(usage.get("total_tokens") or 0) - int(usage.get("input_tokens") or 0),
            )
        usage_totals["billable_output_tokens"] += max(0, int(billable_output_tokens))

    base_payload = build_worker_payload(
        video_title=video_title,
        channel_name=channel_name,
        source_language=source_language,
        target_language=target_language,
        all_subtitles=subtitles,
        settings=settings,
        video_id=video_id,
    )
    chunks = chunk_translation_subtitles(base_payload)
    context_limit = min(5, max(0, settings.context_before_max_events, settings.context_after_max_events))
    subtitle_by_id = {str(sub.index): sub for sub in subtitles}

    def source_subtitles(chunk: list[dict[str, Any]]) -> list[srt.Subtitle]:
        return [subtitle_by_id[str(item["id"])] for item in chunk]

    def limited_context(pairs: list[dict[str, str]]) -> list[dict[str, str]]:
        return pairs[-context_limit:] if context_limit else []

    async def translate_chunk(
        chunk: list[dict[str, Any]],
        previous_context: list[dict[str, str]],
        next_context: list[dict[str, str]],
        chunk_index: int,
        chunk_count: int,
    ) -> list[dict[str, Any]]:
        payload = {
            **base_payload,
            "subtitles": chunk,
            "previous_context": previous_context,
            "next_context": next_context,
            "translation_chunk_index": chunk_index,
            "translation_chunk_count": chunk_count,
            "translation_chunk_from_id": chunk[0]["id"],
            "translation_chunk_to_id": chunk[-1]["id"],
        }
        usage_totals["requests"] += 1
        result = await run_worker(payload)
        add_usage(result)
        translated = validate_translations(source_subtitles(chunk), result)
        discard_successful_attempt(result)
        return translated

    async def translate_chunk_once_with_split(
        chunk: list[dict[str, Any]],
        previous_context: list[dict[str, str]],
        next_context: list[dict[str, str]],
        chunk_index: int,
        chunk_count: int,
    ) -> list[dict[str, Any]]:
        try:
            return await translate_chunk(chunk, previous_context, next_context, chunk_index, chunk_count)
        except Exception as error:
            if (
                settings.provider_name not in {"openai_api", "gemini_api"}
                or len(chunks) <= 1
                or len(chunk) < 2
                or not _chunk_error_can_be_split(error)
            ):
                raise
            midpoint = len(chunk) // 2
            left, right = chunk[:midpoint], chunk[midpoint:]
            left_next = [{"source": str(item["text"])} for item in right[:context_limit]]
            left_translated = await translate_chunk(left, previous_context, left_next, chunk_index, chunk_count)
            right_context = limited_context(previous_context + _recent_translation_pairs(source_subtitles(left), left_translated))
            right_translated = await translate_chunk(right, right_context, next_context, chunk_index, chunk_count)
            return [*left_translated, *right_translated]

    if on_progress:
        on_progress(0, total_subtitles, recent_pairs)
    try:
        translated_segments: list[dict[str, Any]] = []
        completed = 0
        for chunk_index, chunk in enumerate(chunks, start=1):
            following = chunks[chunk_index] if chunk_index < len(chunks) else []
            next_context = [{"source": str(item["text"])} for item in following[:context_limit]]
            translated = await translate_chunk_once_with_split(
                chunk,
                limited_context(recent_pairs),
                next_context,
                chunk_index,
                len(chunks),
            )
            translated_segments.extend(translated)
            recent_pairs = limited_context(recent_pairs + _recent_translation_pairs(source_subtitles(chunk), translated))
            completed += len(chunk)
            if on_progress:
                on_progress(completed, total_subtitles, recent_pairs)
        translated_segments = validate_translations(subtitles, {"subtitles": translated_segments})
    except Exception as error:
        if isinstance(error, TranslationError):
            error.translation_usage = {"engine": settings.engine, "model": settings.model_name, **usage_totals}
            error.translation_chunk_failed = len(chunks) > 1
            raise
        translation_error = TranslationError(
            "remote LLM translation failed: "
            f"source={source_language} target={target_language} "
            f"error={type(error).__name__}: {error}"
        )
        translation_error.translation_usage = {"engine": settings.engine, "model": settings.model_name, **usage_totals}
        translation_error.translation_chunk_failed = len(chunks) > 1
        raise translation_error from error

    subtitle_positions = {str(sub.index): position for position, sub in enumerate(subtitles)}
    for segment_index, segment in enumerate(translated_segments, start=1):
        start_subtitle = subtitles[subtitle_positions[segment["from_id"]]]
        end_subtitle = subtitles[subtitle_positions[segment["to_id"]]]
        translated_text = segment["text"]
        translated_subtitles.append(
            srt.Subtitle(
                index=segment_index,
                start=start_subtitle.start,
                end=end_subtitle.end,
                content=translated_text,
                proprietary=start_subtitle.proprietary,
            )
        )
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
            "translation_thinking_tokens": usage_totals["thinking_tokens"],
            "translation_billable_output_tokens": usage_totals["billable_output_tokens"],
            "translation_total_tokens": usage_totals["total_tokens"],
            "translation_request_count": usage_totals["requests"],
            "translation_chunk_count": len(chunks),
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
