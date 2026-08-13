import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import srt

from app import main
from app.asr_client import AsrConfig, AsrServiceError, AsrTranscript, _segment_subtitles, cache_variant


def test_asr_segments_become_sorted_srt_without_losing_unicode() -> None:
    subtitles = _segment_subtitles(
        [
            {"start": 3.0, "end": 4.5, "text": "世界"},
            {"start": 2.5, "end": 2.8, "text": "こんにちは"},
        ]
    )

    assert [(item.start, item.end, item.content) for item in subtitles] == [
        (timedelta(seconds=2.5), timedelta(seconds=2.8), "こんにちは"),
        (timedelta(seconds=3), timedelta(seconds=4.5), "世界"),
    ]
    assert "こんにちは" in srt.compose(subtitles)


@pytest.mark.parametrize(
    "segments",
    [
        [{"start": -1, "end": 1, "text": "bad"}],
        [{"start": 2, "end": 1, "text": "bad"}],
        [
            {"start": 0, "end": 2, "text": "first"},
            {"start": 0, "end": 2, "text": "first"},
        ],
        [
            {"start": 5, "end": 6, "text": "late"},
            {"start": 1, "end": 2, "text": "reversed"},
        ],
    ],
)
def test_invalid_asr_segments_are_rejected(segments: list[dict]) -> None:
    with pytest.raises(AsrServiceError):
        _segment_subtitles(segments)


def test_local_asr_japanese_does_not_enter_translation(monkeypatch, tmp_path: Path) -> None:
    transcript = AsrTranscript(
        [_subtitle("日本語の字幕", 0, 2)],
        "ja",
        {"subtitle_source": "local_asr", "asr_model": "medium"},
    )
    transcribe = AsyncMock(return_value=transcript)
    monkeypatch.setattr(main, "transcribe_media", transcribe)
    translate = AsyncMock()
    monkeypatch.setattr(main, "translate_subtitle_if_needed", translate)
    monkeypatch.setattr(main.settings, "translation_enabled", True)

    source, translated, metadata = asyncio.run(
        main.transcribe_local_asr_subtitle(
            video=tmp_path / "video.mkv",
            lang="ja",
            selection={"requested_language": "ja", "translation_engine_requested": "google_cloud"},
            work_dir=tmp_path,
            key="key",
            priority="media",
            info={},
        )
    )

    assert source == translated
    assert "日本語の字幕" in source.read_text(encoding="utf-8")
    assert metadata["subtitle_source"] == "local_asr"
    transcribe.assert_awaited_once()
    assert transcribe.await_args.kwargs["priority"] == "media"
    translate.assert_not_awaited()


def test_local_asr_english_reuses_translation_pipeline(monkeypatch, tmp_path: Path) -> None:
    transcript = AsrTranscript(
        [_subtitle("English speech", 0, 2)],
        "en",
        {"subtitle_source": "local_asr", "asr_model": "medium"},
    )
    monkeypatch.setattr(main, "transcribe_media", AsyncMock(return_value=transcript))
    translated_path = tmp_path / "translated.srt"
    translate = AsyncMock(
        return_value=(translated_path, {"translated": True, "translation_engine": "google_cloud"})
    )
    monkeypatch.setattr(main, "translate_subtitle_if_needed", translate)
    monkeypatch.setattr(main.settings, "translation_enabled", True)

    source, translated, metadata = asyncio.run(
        main.transcribe_local_asr_subtitle(
            video=tmp_path / "video.mkv",
            lang="ja",
            selection={"requested_language": "ja", "translation_engine_requested": "google_cloud"},
            work_dir=tmp_path,
            key="key",
            priority="batch",
            info={},
        )
    )

    assert source.name.endswith(".source.srt")
    assert translated == translated_path
    assert metadata["detected_language"] == "en"
    assert translate.await_args.kwargs["selection"]["source_language"] == "en"
    assert translate.await_args.kwargs["selection"]["translated"] is True
    assert translate.await_args.kwargs["selection"]["source_kind"] == "local_asr"


def test_youtube_subtitle_wins_over_local_asr(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "asr_api_url", "https://asr.example")
    monkeypatch.setattr(main.settings, "asr_token", "token")

    selection = main.select_subtitle_language(
        {"language": "ja", "subtitles": {"ja": [{}]}, "automatic_captions": {}},
        "ja",
    )

    assert selection["source_kind"] == "manual"
    assert main.resolved_cache_key("video", "ja", {"language": "ja", "subtitles": {"ja": [{}]}}).startswith("video_ja_")
    assert "local_asr" not in main.resolved_cache_key("video", "ja", {"language": "ja", "subtitles": {"ja": [{}]}})


def test_download_with_youtube_subtitle_does_not_call_asr(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "video.ja.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n既存字幕\n", encoding="utf-8")
    monkeypatch.setattr(main, "run_command", AsyncMock(return_value=""))
    monkeypatch.setattr(main, "find_downloaded_video", lambda _work_dir: video)
    transcribe = AsyncMock(side_effect=AssertionError("YouTube subtitle should win"))
    monkeypatch.setattr(main, "transcribe_media", transcribe)
    monkeypatch.setattr(main.settings, "asr_api_url", "https://asr.example")
    monkeypatch.setattr(main.settings, "asr_token", "token")

    _video, original, translated, metadata = asyncio.run(
        main.download_sources(
            "video",
            "ja",
            tmp_path,
            {"id": "video", "language": "ja", "subtitles": {"ja": [{}]}},
        )
    )

    assert original == subtitle
    assert translated == subtitle
    assert metadata["subtitle_source"] == "youtube_manual"
    transcribe.assert_not_awaited()


def test_asr_retry_policy_keeps_queue_full_and_fails_worker_error() -> None:
    assert main.is_retryable_prepare_error(AsrServiceError("ASR_QUEUE_FULL", "full", 429))
    assert not main.is_retryable_prepare_error(AsrServiceError("ASR_WORKER_FAILED", "down", 503))
    assert not main.is_retryable_prepare_error(AsrServiceError("ASR_WORKER_UNAVAILABLE", "down", 503))


def test_asr_cache_variant_changes_with_settings() -> None:
    base = AsrConfig("https://asr.example", "token", "medium", "auto", 30, 60, "v1")
    changed = AsrConfig("https://asr.example", "token", "large-v3", "auto", 30, 60, "v1")
    assert cache_variant(base) != cache_variant(changed)


def _subtitle(text: str, start: float, end: float) -> srt.Subtitle:
    return srt.Subtitle(
        index=1,
        start=timedelta(seconds=start),
        end=timedelta(seconds=end),
        content=text,
    )
