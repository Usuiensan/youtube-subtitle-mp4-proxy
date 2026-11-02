import json
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as app_main


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("internal", 8000),
            "path": "/",
            "headers": [],
        }
    )


def _write_prepared_entry(root: Path, key: str, translated: bool = True) -> None:
    entry = root / key
    (entry / "source").mkdir(parents=True)
    (entry / "source" / "video.mp4").write_bytes(b"source")
    (entry / "source" / "original.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nsource\n", encoding="utf-8"
    )
    if translated:
        (entry / "source" / "translated.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\ntranslated\n", encoding="utf-8"
        )
    (entry / "source.json").write_text(
        json.dumps(
            {
                "video_id": "dQw4w9WgXcQ",
                "lang": "ja",
                "title": "Test title",
                "source_video": "source/video.mp4",
                "original_subtitle": "source/original.srt",
                "subtitle": "source/translated.srt" if translated else "source/original.srt",
                "subtitle_meta": {
                    "translated": translated,
                    "source_language": "en",
                    "translation_engine": "google",
                },
            }
        ),
        encoding="utf-8",
    )
    (entry / "output.mp4").write_bytes(b"output")


def test_watch_page_returns_html_for_valid_key_and_rejects_invalid_key() -> None:
    with TestClient(app_main.app) as client:
        response = client.get("/watch/test-key")
        invalid = client.get("/watch/bad$key")
    assert response.status_code == 200
    assert "test-key" in response.text
    assert 'id="watchVideo"' in response.text
    assert 'id="subtitleList"' in response.text
    assert invalid.status_code == 400


def test_subtitle_bundle_includes_watch_state_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "cache_hot_dir", tmp_path / "hot")
    monkeypatch.setattr(app_main.settings, "cache_archive_dir", None)
    _write_prepared_entry(tmp_path / "hot", "test-key")
    body = app_main.prepared_subtitle_bundle_for_key("test-key")
    assert body["source_ready"] is True
    assert body["output_ready"] is True
    assert body["output_url"] is None
    assert body["title"] == "Test title"


def test_job_response_contains_watch_url_only_when_translation_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "cache_hot_dir", tmp_path / "hot")
    monkeypatch.setattr(app_main.settings, "cache_archive_dir", None)
    key = app_main.cache_key("dQw4w9WgXcQ", "ja", "en", "google")
    _write_prepared_entry(tmp_path / "hot", key, translated=True)
    job = {
        "status": "running",
        "video_id": "dQw4w9WgXcQ",
        "lang": "ja",
        "mode": "mp4",
        "subtitle_source_lang": "en",
        "translation_engine": "google",
    }
    body = app_main.job_response_body("job-1", job, _request())
    assert body["watch_url"].endswith(f"/watch/{key}")

    job.pop("subtitle_source_lang")
    job.pop("translation_engine")
    body = app_main.job_response_body("job-2", job, _request())
    assert "watch_url" not in body
