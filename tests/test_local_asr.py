import asyncio
import json
import socket
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import patch

import pytest

from app.asr_client import AsrConfig, AsrServiceError, LocalAsrClient


class FakeResponse:
    def __init__(self, payload: object, status: int = 200, content_type: str = "application/json") -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_media_session_uploads_chunks_and_reads_timed_results() -> None:
    responses = [
        FakeResponse({"session_id": "ses_1", "status": "created", "priority": "batch", "timebase": "media_relative"}),
        FakeResponse({"accepted": True, "chunk_index": 0}),
        FakeResponse({"accepted": True, "chunk_index": 1}),
        FakeResponse({"session_id": "ses_1", "status": "completed"}),
        FakeResponse({
            "session_id": "ses_1",
            "items": [{
                "result_id": "res_1",
                "cursor": "1",
                "chunk_index": 0,
                "text": "字幕です",
                "language": "ja",
                "language_probability": 0.99,
                "status": "final",
                "timebase": "media_relative",
                "segments": [
                    {"start": 10.25, "end": 11.5, "text": "字幕です"},
                ],
            }],
            "next_cursor": "1",
            "session_status": "completed",
        }),
    ]

    def urlopen(_request: urllib.request.Request, timeout: float):
        assert timeout == 5
        return responses.pop(0)

    client = LocalAsrClient(AsrConfig("http://127.0.0.1:8080", "token", timeout_seconds=5))
    with patch("urllib.request.urlopen", side_effect=urlopen) as mocked:
        transcript = asyncio.run(client.transcribe_chunks([(b"wav-0", 0), (b"wav-1", 10)] , priority="batch"))

    assert not responses
    assert transcript.session_id == "ses_1"
    assert transcript.segments[0].start == 10.25
    assert transcript.segments[0].end == 11.5
    assert transcript.segments[0].text == "字幕です"
    requests = [call.args[0] for call in mocked.call_args_list]
    assert requests[0].full_url == "http://127.0.0.1:8080/v1/audio-sessions"
    assert json.loads(requests[0].data) == {
        "source": "media_file",
        "priority": "batch",
        "timebase": "media_relative",
        "language": "auto",
        "sample_rate": 16000,
        "channels": 1,
        "content_type": "audio/wav",
        "segmentation": {"type": "fixed", "max_chunk_seconds": 30},
        "retention": {"save_transcript": True, "save_audio": False},
    }
    assert requests[1].headers["X-media-offset"] == "0.000000"
    assert requests[2].headers["X-media-offset"] == "10.000000"
    assert requests[4].full_url.endswith("/results?after=0&limit=50")


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "ASR_QUEUE_FULL"), (429, "ASR_QUEUE_EVICTED"), (503, "ASR_WORKER_FAILED"), (503, "ASR_WORKER_UNAVAILABLE")],
)
def test_scheduler_errors_are_preserved(status: int, code: str) -> None:
    error = urllib.error.HTTPError(
        "http://asr.test/v1/audio-sessions",
        status,
        "error",
        {},
        None,
    )
    error.read = lambda: json.dumps({"error": {"code": code, "message": "scheduler"}}).encode()  # type: ignore[method-assign]
    client = LocalAsrClient(AsrConfig("http://asr.test", "token"))

    with patch("urllib.request.urlopen", side_effect=error), pytest.raises(AsrServiceError) as raised:
        asyncio.run(client.create_media_session())

    assert (raised.value.code, raised.value.status_code) == (code, status)


def test_timeout_is_distinguished_from_worker_failure() -> None:
    client = LocalAsrClient(AsrConfig("http://asr.test", "token"))
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(socket.timeout("timed out"))):
        with pytest.raises(AsrServiceError) as raised:
            asyncio.run(client.create_media_session())
    assert (raised.value.code, raised.value.status_code) == ("ASR_TIMEOUT", 504)


def test_health_and_scheduler_status_use_existing_endpoints() -> None:
    responses = [FakeResponse(b"<html>voice input</html>", content_type="text/html"), FakeResponse({"state": "ready", "queue_length": 0})]
    client = LocalAsrClient(AsrConfig("http://asr.test", "token", status_url="http://asr.test:8081/internal/asr/status"))
    with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: responses.pop(0)):
        health = asyncio.run(client.health())
        status = asyncio.run(client.status())
    assert health["ok"] is True
    assert health["content_type"] == "text/html"
    assert status == {"state": "ready", "queue_length": 0}


def test_config_reads_local_asr_environment(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_ASR_BASE_URL", "http://127.0.0.1:8080/")
    monkeypatch.setenv("LOCAL_ASR_TOKEN", "secret")
    monkeypatch.setenv("LOCAL_ASR_STATUS_URL", "http://127.0.0.1:8081/internal/asr/status")
    config = AsrConfig.from_env()
    assert (config.api_url, config.token, config.status_url) == (
        "http://127.0.0.1:8080",
        "secret",
        "http://127.0.0.1:8081/internal/asr/status",
    )
