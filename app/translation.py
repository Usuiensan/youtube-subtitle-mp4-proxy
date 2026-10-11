from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from decimal import Decimal
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


@dataclass(frozen=True)
class MeaningGroup:
    group_id: str
    subtitles: tuple[srt.Subtitle, ...]
    forced_split: bool = False

    @property
    def duration_seconds(self) -> float:
        return max(0.001, (self.subtitles[-1].end - self.subtitles[0].start).total_seconds())

    def payload(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "source": [
                {
                    "id": subtitle.index,
                    "text": normalize_subtitle_text(subtitle.content, compact=True),
                    "start": round(subtitle.start.total_seconds(), 3),
                    "end": round(subtitle.end.total_seconds(), 3),
                }
                for subtitle in self.subtitles
            ],
            "forced_split": self.forced_split,
        }


@dataclass(frozen=True)
class TranslationSplit:
    segments: tuple[str, ...]
    score: float
    difficult: bool


def load_srt(path: Path) -> list[srt.Subtitle]:
    return list(srt.parse(path.read_text(encoding="utf-8-sig")))


def normalize_subtitle_text(text: str, *, compact: bool = False) -> str:
    normalized = re.sub(r"\\+[nNhH]", " ", str(text))
    normalized = re.sub(r"\\+\r?\n", "\n", normalized)
    normalized = re.sub(r"\\+(?=\s)", " ", normalized)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normalized.split()) if compact else normalized


def save_srt(path: Path, subtitles: list[srt.Subtitle]) -> None:
    path.write_text(srt.compose(subtitles, reindex=False), encoding="utf-8")


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
    groups = build_meaning_groups(all_subtitles)
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
        "groups": [group.payload() for group in groups],
    }


def _chunk_input_token_limit() -> int:
    return max(512, int(os.getenv("TRANSLATION_CHUNK_INPUT_TOKENS", "3500")))


def _chunk_input_token_estimate(payload: dict[str, Any]) -> int:
    # Keep a conservative fixed allowance for the prompt instructions and OpenAI schema.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return math.ceil((len(encoded) + 3072) / 3)


_SENTENCE_END_RE = re.compile(r"[.!?。！？…][\"'”’）】」』]*$")
_CONTINUATION_END_RE = re.compile(r"[,，、:：;；\-—][\"'”’）】」』]*$")
_SPEAKER_RE = re.compile(r"^(?:[-–—]\s*|[\[（(][^\]）)]{1,24}[\]）)])")


def _meaning_boundary_score(subtitles: list[srt.Subtitle], position: int) -> float:
    current = subtitles[position]
    following = subtitles[position + 1] if position + 1 < len(subtitles) else None
    text = normalize_subtitle_text(current.content, compact=True)
    if following is None:
        return 100.0
    gap = (following.start - current.end).total_seconds()
    score = 0.0
    if _SENTENCE_END_RE.search(text):
        score += 80.0
    if gap >= 0.7:
        score += 70.0 + min(gap, 3.0) * 5.0
    if _SPEAKER_RE.search(normalize_subtitle_text(following.content, compact=True)):
        score += 50.0
    if _CONTINUATION_END_RE.search(text):
        score -= 45.0
    if text and text[-1:].isalnum() and normalize_subtitle_text(following.content, compact=True)[:1].islower():
        score -= 20.0
    return score


def build_meaning_groups(subtitles: list[srt.Subtitle]) -> list[MeaningGroup]:
    if not subtitles:
        return []
    count = len(subtitles)
    boundary_scores = [_meaning_boundary_score(subtitles, index) for index in range(count)]
    best_cost = [math.inf] * (count + 1)
    previous = [-1] * (count + 1)
    best_cost[0] = 0.0

    for end in range(1, count + 1):
        for start in range(max(0, end - 8), end):
            group = subtitles[start:end]
            duration = (group[-1].end - group[0].start).total_seconds()
            if duration > 12.0 and len(group) > 1:
                continue
            events = len(group)
            cost = abs(events - 4) * 2.5 + abs(duration - 6.0) * 0.45
            boundary_score = boundary_scores[end - 1]
            cost -= boundary_score * 0.12
            for internal in range(start, end - 1):
                if boundary_scores[internal] >= 70.0:
                    cost += 18.0
            if events == 1 and boundary_score < 70.0:
                cost += 10.0
            candidate = best_cost[start] + cost
            if candidate < best_cost[end]:
                best_cost[end] = candidate
                previous[end] = start

    if previous[count] < 0:
        raise TranslationError("meaning groups could not be constructed")
    ranges: list[tuple[int, int]] = []
    end = count
    while end:
        start = previous[end]
        ranges.append((start, end))
        end = start
    ranges.reverse()
    return [
        MeaningGroup(
            group_id=f"g{number:03d}",
            subtitles=tuple(subtitles[start:end]),
            forced_split=end < count and boundary_scores[end - 1] < 0.0,
        )
        for number, (start, end) in enumerate(ranges, start=1)
    ]


def batch_translation_groups(payload: dict[str, Any], groups: list[MeaningGroup]) -> list[list[MeaningGroup]]:
    if str(payload.get("translation_provider") or "") not in {"openai_api", "gemini_api"}:
        return [groups] if groups else []
    limit = _chunk_input_token_limit()
    batches: list[list[MeaningGroup]] = []
    current: list[MeaningGroup] = []
    for group in groups:
        candidate = [*current, group]
        estimate_payload = {**payload, "groups": [item.payload() for item in candidate]}
        estimate_payload.pop("subtitles", None)
        if current and _chunk_input_token_estimate(estimate_payload) > limit:
            batches.append(current)
            current = [group]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


_BAD_SPLIT_BEFORE = set("、。！？!?)]}）］｝」』】〉》,.;:：；")
_BAD_SPLIT_AFTER = set("([{（［｛「『【〈《")
_GOOD_SPLIT_AFTER = set("、。！？!?：；")


def _char_weight(text: str) -> float:
    return sum(0.5 if ord(character) < 128 else 1.0 for character in text)


def _split_boundary_penalty(text: str, position: int) -> float:
    before = text[position - 1]
    after = text[position]
    if before in _BAD_SPLIT_AFTER or after in _BAD_SPLIT_BEFORE:
        return 50.0
    if before in _GOOD_SPLIT_AFTER:
        return -2.0
    if before.isspace() or after.isspace():
        return -4.0
    if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
        return 30.0
    if before in "てでにはをがとのもへばからのでし" or after in "しかしそしてまた":
        return -1.5
    return 1.0


def split_group_translation(translation: str, subtitles: tuple[srt.Subtitle, ...]) -> TranslationSplit:
    text = str(translation).strip()
    count = len(subtitles)
    if not text:
        raise TranslationError("empty group translation")
    if len(text) < count:
        raise TranslationError("group translation is too short for non-empty subtitle ids")
    if count == 1:
        duration = max(0.001, (subtitles[0].end - subtitles[0].start).total_seconds())
        difficult = _char_weight(text) / duration > 14.0
        return TranslationSplit((text,), 0.0, difficult)

    durations = [max(0.1, (item.end - item.start).total_seconds()) for item in subtitles]
    total_duration = sum(durations)
    cumulative_targets: list[float] = []
    running = 0.0
    for duration in durations[:-1]:
        running += duration
        cumulative_targets.append(len(text) * running / total_duration)

    states: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {(0, 0): (0.0, ())}
    for segment_index in range(1, count):
        for position in range(segment_index, len(text) - (count - segment_index) + 1):
            best: tuple[float, tuple[int, ...]] | None = None
            target = cumulative_targets[segment_index - 1]
            for previous_position in range(segment_index - 1, position):
                prior = states.get((segment_index - 1, previous_position))
                if prior is None:
                    continue
                cost = prior[0] + ((position - target) ** 2) / max(1.0, len(text)) + _split_boundary_penalty(text, position)
                if best is None or cost < best[0]:
                    best = (cost, (*prior[1], position))
            if best is not None:
                states[(segment_index, position)] = best

    best = min(
        (value for (segment_index, _position), value in states.items() if segment_index == count - 1),
        key=lambda item: item[0],
    )
    cuts = (0, *best[1], len(text))
    segments = tuple(text[cuts[index] : cuts[index + 1]] for index in range(count))
    if "".join(segments) != text or any(not segment for segment in segments):
        raise TranslationError("translation split did not preserve the completed translation")
    rates = [_char_weight(segment) / duration for segment, duration in zip(segments, durations)]
    bad_boundaries = sum(
        _split_boundary_penalty(text, position) >= 30.0
        for position in best[1]
    )
    difficult = bad_boundaries > 0 or max(rates) > 14.0
    return TranslationSplit(segments, best[0], difficult)


def split_group_translation_candidates(
    translation: str,
    subtitles: tuple[srt.Subtitle, ...],
) -> list[TranslationSplit]:
    best = split_group_translation(translation, subtitles)
    if len(subtitles) == 1:
        return [best]

    text = str(translation).strip()
    equal_subtitles = tuple(
        srt.Subtitle(index=item.index, start=item.start, end=item.start + (subtitles[0].end - subtitles[0].start), content=item.content)
        for item in subtitles
    )
    alternatives = [best, split_group_translation(text, equal_subtitles)]
    unique: list[TranslationSplit] = []
    for candidate in alternatives:
        if candidate.segments not in {item.segments for item in unique}:
            unique.append(candidate)
    return unique


_ANCHOR_RE = re.compile(r"https?://\S+|[A-Za-z]*\d+(?:[.,]\d+)?[A-Za-z]*")
_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|万|億)?", re.IGNORECASE)
_FILLER_TRANSLATIONS = {"完了", "終了", "翻訳なし", "翻訳完了", "translation complete", "done"}


def _translation_anchors(text: str) -> list[str]:
    return [match.group(0) for match in _ANCHOR_RE.finditer(text)]


def _values_preserved(source: list[Any], translated: list[Any]) -> bool:
    remaining = iter(source)
    expected = next(remaining, None)
    for value in translated:
        if value == expected:
            expected = next(remaining, None)
    return expected is None


def _numeric_values(text: str) -> list[Decimal]:
    scales = {"": 1, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000, "万": 10_000, "億": 100_000_000}
    return [Decimal(number.replace(",", "")) * scales[scale.lower()] for number, scale in _NUMBER_RE.findall(text)]


def _group_translation_issue(group: MeaningGroup, translation: str) -> str | None:
    text = str(translation).strip()
    if not text:
        return "empty translation"
    if text.lower().strip("。.!！ ") in _FILLER_TRANSLATIONS:
        return "filler translation"
    source = " ".join(normalize_subtitle_text(item.content, compact=True) for item in group.subtitles)
    source_anchors = _translation_anchors(source)
    source_named = [anchor for anchor in source_anchors if not anchor[0].isdigit()]
    translated_named = [anchor for anchor in _translation_anchors(text) if not anchor[0].isdigit()]
    if source_anchors and (
        not _values_preserved(source_named, translated_named)
        or not _values_preserved(_numeric_values(source), _numeric_values(text))
    ):
        return "number, model, or URL anchors changed"
    return None


def validate_group_translations(
    groups: list[MeaningGroup],
    result: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    raw = result.get("groups")
    if not isinstance(raw, list):
        raise TranslationError("translation groups must be an array")
    expected = {group.group_id: group for group in groups}
    accepted: dict[str, str] = {}
    invalid: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "")
        if group_id not in expected:
            raise TranslationError(f"unexpected translation group: {group_id}")
        if group_id in accepted:
            invalid.add(group_id)
            accepted.pop(group_id, None)
            continue
        text = str(item.get("translation") or "").strip()
        issue = _group_translation_issue(expected[group_id], text)
        if issue:
            invalid.add(group_id)
            continue
        accepted[group_id] = text
    invalid.update(set(expected) - set(accepted))
    return accepted, [group.group_id for group in groups if group.group_id in invalid]


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
    meaning_groups = build_meaning_groups(subtitles)
    chunks = batch_translation_groups(base_payload, meaning_groups)
    context_limit = min(5, max(0, settings.context_before_max_events, settings.context_after_max_events))

    def limited_context(pairs: list[dict[str, str]]) -> list[dict[str, str]]:
        return pairs[-context_limit:] if context_limit else []

    async def run_group_request(
        chunk: list[MeaningGroup],
        previous_context: list[dict[str, str]],
        next_context: list[dict[str, str]],
        chunk_index: int,
        chunk_count: int,
        *,
        concise: bool = False,
    ) -> dict:
        payload = {
            **base_payload,
            "groups": [group.payload() for group in chunk],
            "previous_context": previous_context,
            "next_context": next_context,
            "translation_chunk_index": chunk_index,
            "translation_chunk_count": chunk_count,
            "translation_chunk_from_id": chunk[0].subtitles[0].index,
            "translation_chunk_to_id": chunk[-1].subtitles[-1].index,
            "concise": concise,
        }
        if concise:
            payload["concise_max_characters"] = {
                group.group_id: max(
                    1,
                    math.floor((group.subtitles[-1].end - group.subtitles[0].start).total_seconds() * 14.0),
                )
                for group in chunk
            }
        payload.pop("subtitles", None)
        usage_totals["requests"] += 1
        result = await run_worker(payload)
        add_usage(result)
        discard_successful_attempt(result)
        return result

    async def translate_chunk(
        chunk: list[MeaningGroup],
        previous_context: list[dict[str, str]],
        next_context: list[dict[str, str]],
        chunk_index: int,
        chunk_count: int,
    ) -> dict[str, str]:
        result = await run_group_request(chunk, previous_context, next_context, chunk_index, chunk_count)
        accepted, invalid = validate_group_translations(chunk, result)
        for group_id in invalid:
            group = next(item for item in chunk if item.group_id == group_id)
            retry = await run_group_request([group], previous_context, next_context, chunk_index, chunk_count)
            retried, still_invalid = validate_group_translations([group], retry)
            if still_invalid:
                raise TranslationError(f"invalid translation group after retry: {group_id}")
            accepted.update(retried)
        ordered = {group.group_id: accepted[group.group_id] for group in chunk}
        duplicate_run: list[str] = []
        previous_text = None
        for group_id, text in ordered.items():
            duplicate_run = [*duplicate_run, group_id] if text == previous_text else [group_id]
            if len(duplicate_run) >= 3:
                for retry_group_id in duplicate_run:
                    group = next(item for item in chunk if item.group_id == retry_group_id)
                    retry = await run_group_request([group], previous_context, next_context, chunk_index, chunk_count)
                    retried, still_invalid = validate_group_translations([group], retry)
                    if still_invalid or retried[group.group_id] == text:
                        raise TranslationError(f"abnormal repeated translation: {retry_group_id}")
                    ordered.update(retried)
            previous_text = text
        return ordered

    async def select_split_candidate(group: MeaningGroup, translation: str, candidates: list[TranslationSplit]) -> TranslationSplit:
        payload = {
            **base_payload,
            "task": "select_split_candidate",
            "group": group.payload(),
            "translation": translation,
            "candidates": [list(candidate.segments) for candidate in candidates],
        }
        payload.pop("groups", None)
        usage_totals["requests"] += 1
        result = await run_worker(payload)
        add_usage(result)
        discard_successful_attempt(result)
        index = result.get("candidate_index") if isinstance(result, dict) else None
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(candidates):
            raise TranslationError(f"invalid split candidate selection: {group.group_id}")
        selected = candidates[index]
        if "".join(selected.segments) != translation:
            raise TranslationError(f"selected split changed translation: {group.group_id}")
        return selected

    if on_progress:
        on_progress(0, total_subtitles, recent_pairs)
    try:
        translated_groups: dict[str, str] = {}
        completed = 0
        for chunk_index, chunk in enumerate(chunks, start=1):
            following = chunks[chunk_index] if chunk_index < len(chunks) else []
            next_context = [
                {"source": " ".join(normalize_subtitle_text(item.content, compact=True) for item in group.subtitles)}
                for group in following[:context_limit]
            ]
            translated = await translate_chunk(
                chunk,
                limited_context(recent_pairs),
                next_context,
                chunk_index,
                len(chunks),
            )
            translated_groups.update(translated)
            recent_pairs = limited_context(
                recent_pairs
                + [
                    {
                        "source": " ".join(normalize_subtitle_text(item.content, compact=True) for item in group.subtitles),
                        "translated": translated[group.group_id],
                    }
                    for group in chunk
                ]
            )
            completed += sum(len(group.subtitles) for group in chunk)
            if on_progress:
                on_progress(completed, total_subtitles, recent_pairs)
        for group in meaning_groups:
            translation = translated_groups[group.group_id]
            candidates = split_group_translation_candidates(translation, group.subtitles)
            split = candidates[0]
            if split.difficult and len(candidates) < 2:
                concise_result = await run_group_request(
                    [group], limited_context(recent_pairs), [], 1, 1, concise=True
                )
                concise, invalid = validate_group_translations([group], concise_result)
                if invalid:
                    raise TranslationError(f"invalid concise translation: {group.group_id}")
                translation = concise[group.group_id]
                translated_groups[group.group_id] = translation
                candidates = split_group_translation_candidates(translation, group.subtitles)
                split = candidates[0]
            if split.difficult:
                if len(candidates) < 2:
                    raise TranslationError(f"translation cannot be split naturally: {group.group_id}")
                split = await select_split_candidate(group, translation, candidates)
            for source_subtitle, translated_text in zip(group.subtitles, split.segments):
                translated_subtitles.append(
                    srt.Subtitle(
                        index=source_subtitle.index,
                        start=source_subtitle.start,
                        end=source_subtitle.end,
                        content=translated_text,
                        proprietary=source_subtitle.proprietary,
                    )
                )
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

    if [item.index for item in translated_subtitles] != [item.index for item in subtitles] or any(
        translated.start != source.start or translated.end != source.end
        for source, translated in zip(subtitles, translated_subtitles)
    ):
        raise TranslationError("translated subtitle ids or timings changed")
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
            "translation_group_count": len(meaning_groups),
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
