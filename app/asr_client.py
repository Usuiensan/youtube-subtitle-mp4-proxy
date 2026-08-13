"""Client for the shared discord-transcriber media ASR session API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import srt

from app.command_runner import run_command


class AsrServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AsrConfig:
    api_url: str
    token: str
    model: str
    language: str
    chunk_seconds: int
    timeout_seconds: int
    settings_version: str

    @property
    def settings_hash(self) -> str:
        payload = {
            "model": self.model,
            "language": self.language,
            "chunk_seconds": self.chunk_seconds,
            "settings_version": self.settings_version,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


@dataclass(frozen=True)
class AsrTranscript:
    subtitles: list[srt.Subtitle]
    detected_language: str | None
    metadata: dict[str, Any]


def cache_variant(config: AsrConfig) -> str:
    return f"local_asr_{config.settings_hash}"


def _request_json(
    config: AsrConfig,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not config.api_url or not config.token:
        raise AsrServiceError(
            "ASR_NOT_CONFIGURED",
            "共有ASR APIのURLとtokenが設定されていません。",
            503,
        )
    body = None
    headers = {"Authorization": f"Bearer {config.token}"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{config.api_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            response = {}
        detail = response.get("error") if isinstance(response, dict) else None
        if not isinstance(detail, dict):
            detail = {}
        raise AsrServiceError(
            str(detail.get("code") or "ASR_REQUEST_FAILED"),
            str(detail.get("message") or raw or f"HTTP {error.code}"),
            error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AsrServiceError("ASR_WORKER_UNAVAILABLE", str(error), 503) from error


async def _request_json_async(
    config: AsrConfig,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _request_json,
        config,
        path,
        method=method,
        payload=payload,
    )


def _segment_subtitles(segments: list[dict[str, Any]]) -> list[srt.Subtitle]:
    parsed: list[tuple[float, float, str]] = []
    previous_start = -math.inf
    for segment in segments:
        if not isinstance(segment, dict):
            raise AsrServiceError("ASR_INVALID_SEGMENT", "ASR segmentがobjectではありません。", 502)
        try:
            start = float(segment["start"])
            end = float(segment["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise AsrServiceError("ASR_INVALID_SEGMENT", "ASR segmentの時刻が不正です。", 502) from error
        text = str(segment.get("text") or "").strip()
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise AsrServiceError("ASR_INVALID_SEGMENT", "ASR segmentの時刻範囲が不正です。", 502)
        if start < previous_start - 1.0:
            raise AsrServiceError("ASR_INVALID_SEGMENT", "ASR segmentの時刻が大きく逆転しています。", 502)
        previous_start = start
        if text:
            parsed.append((start, end, text))

    parsed.sort(key=lambda item: (item[0], item[1], item[2]))
    subtitles: list[srt.Subtitle] = []
    previous_end = -math.inf
    seen: set[tuple[float, float, str]] = set()
    for index, (start, end, text) in enumerate(parsed, start=1):
        item = (start, end, text)
        if item in seen or start < previous_end - 0.01:
            raise AsrServiceError("ASR_INVALID_SEGMENT", "ASR segmentが重複しています。", 502)
        seen.add(item)
        subtitles.append(
            srt.Subtitle(
                index=index,
                start=timedelta(seconds=start),
                end=timedelta(seconds=end),
                content=text,
            )
        )
        previous_end = end
    if not subtitles:
        raise AsrServiceError("ASR_NO_SEGMENTS", "ASR結果に有効なsegmentがありません。", 422)
    return subtitles


async def _split_audio(media_path: Path, output_dir: Path, config: AsrConfig) -> list[tuple[Path, float]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = output_dir / "chunk-%06d.wav"
    await run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(config.chunk_seconds),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ],
        timeout_seconds=config.timeout_seconds,
        raise_http=False,
    )
    chunks: list[tuple[Path, float]] = []
    offset = 0.0
    for chunk in sorted(output_dir.glob("chunk-*.wav")):
        duration_text = await run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(chunk),
            ],
            timeout_seconds=config.timeout_seconds,
            raise_http=False,
        )
        try:
            duration = float(duration_text.strip())
        except ValueError as error:
            raise AsrServiceError("ASR_AUDIO_INVALID", "音声チャンクの長さを取得できません。", 502) from error
        if not math.isfinite(duration) or duration <= 0:
            raise AsrServiceError("ASR_AUDIO_INVALID", "音声チャンクの長さが不正です。", 502)
        chunks.append((chunk, offset))
        offset += duration
    if not chunks:
        raise AsrServiceError("ASR_AUDIO_EMPTY", "音声チャンクを生成できませんでした。", 422)
    return chunks


async def transcribe_media(
    media_path: Path,
    work_dir: Path,
    config: AsrConfig,
    *,
    priority: str,
) -> AsrTranscript:
    session = await _request_json_async(
        config,
        "/v1/audio-sessions",
        method="POST",
        payload={
            "source": "media_file",
            "priority": priority,
            "timebase": "media_relative",
            "language": config.language,
            "sample_rate": 16000,
            "channels": 1,
            "content_type": "audio/wav",
            "segmentation": {"type": "fixed", "max_chunk_seconds": config.chunk_seconds},
            "retention": {"save_transcript": True, "save_audio": False},
        },
    )
    session_id = str(session.get("session_id") or "")
    if not session_id:
        raise AsrServiceError("ASR_INVALID_RESPONSE", "ASR session_idがありません。", 502)

    results: list[dict[str, Any]] = []
    try:
        for chunk_index, (chunk, offset) in enumerate(
            await _split_audio(media_path, work_dir / "asr-chunks", config)
        ):
            response = await asyncio.to_thread(
                _upload_chunk,
                config,
                session_id,
                chunk_index,
                offset,
                chunk,
            )
            result = response.get("result")
            if isinstance(result, dict):
                results.append(result)
    finally:
        try:
            await _request_json_async(
                config,
                f"/v1/audio-sessions/{session_id}/stop",
                method="POST",
                payload={"reason": "youtube_prepare"},
            )
        except AsrServiceError:
            pass

    segments = [
        segment
        for result in results
        for segment in (result.get("segments") or [])
        if isinstance(segment, dict)
    ]
    subtitles = _segment_subtitles(segments)
    detected = max(
        (result for result in results if result.get("language")),
        key=lambda result: float(result.get("language_probability") or 0),
        default={},
    )
    detected_language = str(detected.get("language") or "") or None
    return AsrTranscript(
        subtitles=subtitles,
        detected_language=detected_language,
        metadata={
            "subtitle_source": "local_asr",
            "asr_model": config.model,
            "requested_language": config.language,
            "detected_language": detected_language,
            "asr_settings_version": config.settings_version,
            "asr_settings_hash": config.settings_hash,
            "asr_priority": priority,
            "asr_segment_count": len(subtitles),
        },
    )


def _upload_chunk(
    config: AsrConfig,
    session_id: str,
    chunk_index: int,
    offset: float,
    chunk: Path,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{config.api_url.rstrip('/')}/v1/audio-sessions/{session_id}/chunks",
        data=chunk.read_bytes(),
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "audio/wav",
            "X-Chunk-Index": str(chunk_index),
            "X-Media-Offset": f"{offset:.6f}",
            "X-Client-Chunk-Id": f"youtube-{session_id}-{chunk_index}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            response = {}
        detail = response.get("error") if isinstance(response, dict) else None
        if not isinstance(detail, dict):
            detail = {}
        raise AsrServiceError(
            str(detail.get("code") or "ASR_REQUEST_FAILED"),
            str(detail.get("message") or raw or f"HTTP {error.code}"),
            error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AsrServiceError("ASR_WORKER_UNAVAILABLE", str(error), 503) from error
