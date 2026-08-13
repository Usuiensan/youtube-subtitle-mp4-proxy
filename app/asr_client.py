"""Small client for discord-transcriber's shared audio session API."""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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
    model: str = "shared-worker"
    language: str = "auto"
    chunk_seconds: int = 30
    timeout_seconds: float = 60
    settings_version: str = "v1"
    status_url: str | None = None

    @property
    def base_url(self) -> str:
        return self.api_url

    @classmethod
    def from_env(cls) -> "AsrConfig":
        return cls(
            api_url=os.getenv("LOCAL_ASR_BASE_URL", "").rstrip("/"),
            token=os.getenv("LOCAL_ASR_TOKEN", ""),
            language=os.getenv("LOCAL_ASR_LANGUAGE", "auto"),
            chunk_seconds=max(1, int(os.getenv("LOCAL_ASR_CHUNK_SECONDS", "30"))),
            timeout_seconds=max(1.0, float(os.getenv("LOCAL_ASR_TIMEOUT_SECONDS", "60"))),
            status_url=os.getenv("LOCAL_ASR_STATUS_URL") or None,
        )


@dataclass(frozen=True)
class AsrSession:
    session_id: str
    status: str
    priority: str
    timebase: str


@dataclass(frozen=True)
class AsrSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class AsrResult:
    result_id: str
    cursor: str
    chunk_index: int
    text: str
    started_at: float | str | None
    ended_at: float | str | None
    language: str | None
    language_probability: float | None
    status: str
    timebase: str
    segments: tuple[AsrSegment, ...]


@dataclass(frozen=True)
class AsrResults:
    session_id: str
    items: tuple[AsrResult, ...]
    next_cursor: str
    session_status: str


@dataclass(frozen=True)
class AsrTranscript:
    session_id: str
    results: tuple[AsrResult, ...]
    session_status: str

    @property
    def segments(self) -> tuple[AsrSegment, ...]:
        return tuple(segment for result in self.results for segment in result.segments)


class LocalAsrClient:
    def __init__(self, config: AsrConfig) -> None:
        self.config = config

    async def create_media_session(self, *, priority: str | None = None) -> AsrSession:
        if priority not in {"media", "batch", None}:
            raise ValueError("media_file priority must be media or batch")
        payload = await self._json(
            "/v1/audio-sessions",
            method="POST",
            payload={
                "source": "media_file",
                "priority": priority or "media",
                "timebase": "media_relative",
                "language": self.config.language,
                "sample_rate": 16000,
                "channels": 1,
                "content_type": "audio/wav",
                "segmentation": {
                    "type": "fixed",
                    "max_chunk_seconds": self.config.chunk_seconds,
                },
                "retention": {"save_transcript": True, "save_audio": False},
            },
        )
        try:
            return AsrSession(
                session_id=str(payload["session_id"]),
                status=str(payload.get("status", "created")),
                priority=str(payload["priority"]),
                timebase=str(payload["timebase"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AsrServiceError("ASR_INVALID_RESPONSE", "ASR session response is invalid.", 502) from error

    async def send_chunk(
        self,
        session_id: str,
        chunk_index: int,
        chunk: bytes | Path,
        media_offset: float,
        *,
        client_chunk_id: str | None = None,
        content_type: str = "audio/wav",
    ) -> dict[str, Any]:
        if media_offset < 0 or not math.isfinite(media_offset):
            raise ValueError("media_offset must be a finite non-negative number")
        body = await asyncio.to_thread(chunk.read_bytes) if isinstance(chunk, Path) else chunk
        headers = {
            "Content-Type": content_type,
            "X-Chunk-Index": str(chunk_index),
            "X-Media-Offset": f"{media_offset:.6f}",
            "X-Client-Chunk-Id": client_chunk_id or f"youtube-{session_id}-{chunk_index}",
        }
        return await self._json(f"/v1/audio-sessions/{session_id}/chunks", method="POST", body=body, headers=headers)

    async def get_results(self, session_id: str, *, after: int = 0, limit: int = 50) -> AsrResults:
        if after < 0 or limit < 1:
            raise ValueError("after must be >= 0 and limit must be > 0")
        query = urllib.parse.urlencode({"after": after, "limit": min(limit, 100)})
        payload = await self._json(f"/v1/audio-sessions/{session_id}/results?{query}", method="GET")
        try:
            items = tuple(self._result(item) for item in payload.get("items", []))
            return AsrResults(
                session_id=str(payload["session_id"]),
                items=items,
                next_cursor=str(payload.get("next_cursor", after)),
                session_status=str(payload["session_status"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AsrServiceError("ASR_INVALID_RESPONSE", "ASR results response is invalid.", 502) from error

    async def stop_session(self, session_id: str, *, reason: str = "client") -> dict[str, Any]:
        return await self._json(
            f"/v1/audio-sessions/{session_id}/stop",
            method="POST",
            payload={"reason": reason},
        )

    async def transcribe_chunks(
        self,
        chunks: Iterable[tuple[bytes | Path, float]],
        *,
        priority: str | None = None,
    ) -> AsrTranscript:
        session = await self.create_media_session(priority=priority)
        stopped = False
        try:
            for index, (chunk, offset) in enumerate(chunks):
                await self.send_chunk(session.session_id, index, chunk, offset)
            await self.stop_session(session.session_id, reason="client")
            stopped = True
            results: list[AsrResult] = []
            after = 0
            while True:
                page = await self.get_results(session.session_id, after=after)
                results.extend(page.items)
                next_cursor = int(page.next_cursor)
                if len(page.items) < 50 or not page.items or next_cursor <= after:
                    return AsrTranscript(session.session_id, tuple(results), page.session_status)
                after = next_cursor
        finally:
            if not stopped:
                try:
                    await self.stop_session(session.session_id, reason="client_cleanup")
                except AsrServiceError:
                    pass

    async def health(self) -> dict[str, Any]:
        status, headers, body = await self._raw("/", method="GET")
        if status >= 400:
            raise AsrServiceError("ASR_HEALTH_FAILED", f"HTTP {status}", status)
        return {
            "ok": True,
            "status_code": status,
            "content_type": headers.get_content_type(),
            "body": body.decode("utf-8", errors="replace"),
        }

    async def status(self) -> dict[str, Any]:
        if not self.config.status_url:
            raise AsrServiceError("ASR_STATUS_NOT_CONFIGURED", "LOCAL_ASR_STATUS_URL is not configured.", 503)
        return await self._json_url(self.config.status_url, method="GET")

    async def _json(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._json_url(
            f"{self.config.api_url.rstrip('/')}{path}",
            method=method,
            payload=payload,
            body=body,
            headers=headers,
        )

    async def _json_url(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        _status, _headers, raw = await self._raw(url, method=method, payload=payload, body=body, headers=headers)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AsrServiceError("ASR_INVALID_RESPONSE", "ASR response is not valid JSON.", 502) from error
        if not isinstance(value, dict):
            raise AsrServiceError("ASR_INVALID_RESPONSE", "ASR response must be a JSON object.", 502)
        return value

    async def _raw(
        self,
        url_or_path: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, bytes]:
        if not self.config.api_url or not self.config.token:
            raise AsrServiceError("ASR_NOT_CONFIGURED", "LOCAL_ASR_BASE_URL and LOCAL_ASR_TOKEN are required.", 503)
        url = url_or_path if "://" in url_or_path else f"{self.config.api_url.rstrip('/')}{url_or_path}"
        request_headers = {"Authorization": f"Bearer {self.config.token}", **(headers or {})}
        request_body = body
        if payload is not None:
            request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=request_body, headers=request_headers, method=method)
        try:
            response = await asyncio.to_thread(urllib.request.urlopen, request, timeout=self.config.timeout_seconds)
            with response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("error", {})
            except (json.JSONDecodeError, AttributeError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            raise AsrServiceError(
                str(detail.get("code") or "ASR_REQUEST_FAILED"),
                str(detail.get("message") or raw or f"HTTP {error.code}"),
                error.code,
            ) from error
        except (socket.timeout, TimeoutError) as error:
            raise AsrServiceError("ASR_TIMEOUT", str(error) or "ASR request timed out.", 504) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise AsrServiceError("ASR_TIMEOUT", str(error.reason), 504) from error
            raise AsrServiceError("ASR_WORKER_UNAVAILABLE", str(error.reason), 503) from error

    @staticmethod
    def _result(value: Any) -> AsrResult:
        if not isinstance(value, dict):
            raise ValueError("result is not an object")
        segments: list[AsrSegment] = []
        for raw in value.get("segments", []):
            if not isinstance(raw, dict):
                raise ValueError("segment is not an object")
            start = float(raw["start"])
            end = float(raw["end"])
            text = str(raw["text"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError("segment range is invalid")
            segments.append(AsrSegment(start, end, text))
        probability = value.get("language_probability")
        return AsrResult(
            result_id=str(value["result_id"]),
            cursor=str(value["cursor"]),
            chunk_index=int(value["chunk_index"]),
            text=str(value.get("text", "")),
            started_at=value.get("started_at"),
            ended_at=value.get("ended_at"),
            language=str(value["language"]) if value.get("language") is not None else None,
            language_probability=float(probability) if probability is not None else None,
            status=str(value.get("status", "final")),
            timebase=str(value["timebase"]),
            segments=tuple(segments),
        )


async def _split_audio(media_path: Path, output_dir: Path, config: AsrConfig) -> list[tuple[Path, float]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "chunk-%06d.wav"
    await run_command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(media_path), "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "segment",
            "-segment_time", str(config.chunk_seconds), "-reset_timestamps", "1", str(pattern),
        ],
        timeout_seconds=config.timeout_seconds,
        raise_http=False,
    )
    chunks: list[tuple[Path, float]] = []
    offset = 0.0
    for chunk in sorted(output_dir.glob("chunk-*.wav")):
        duration = float((await run_command(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(chunk)],
            timeout_seconds=config.timeout_seconds,
            raise_http=False,
        )).strip())
        if not math.isfinite(duration) or duration <= 0:
            raise AsrServiceError("ASR_AUDIO_INVALID", "Audio chunk duration is invalid.", 502)
        chunks.append((chunk, offset))
        offset += duration
    if not chunks:
        raise AsrServiceError("ASR_AUDIO_EMPTY", "No audio chunks were generated.", 422)
    return chunks


async def transcribe_media(media_path: Path, work_dir: Path, config: AsrConfig, *, priority: str) -> AsrTranscript:
    client = LocalAsrClient(config)
    chunks = await _split_audio(media_path, work_dir / "asr-chunks", config)
    return await client.transcribe_chunks(chunks, priority=priority)
