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


def test_public_urls_prefer_configured_base_url(monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "youtube_proxy_base_url", "https://lab.usuiensan.dev")
    request = _request()

    assert app_main.prepare_status_url(request, "job-1") == "https://lab.usuiensan.dev/prepare/jobs/job-1"
    assert app_main.prepare_batch_status_url(request, "batch-1") == "https://lab.usuiensan.dev/prepare/batches/batch-1"
    assert app_main.slideshow_public_url(request, "A" * 32) == "https://lab.usuiensan.dev/slideshow/" + "A" * 32 + ".mp4"


def test_public_urls_fall_back_to_request_base_url(monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "youtube_proxy_base_url", "")
    request = _request()

    assert app_main.prepare_status_url(request, "job-1") == "http://internal:8000/prepare/jobs/job-1"
    assert app_main.prepare_batch_status_url(request, "batch-1") == "http://internal:8000/prepare/batches/batch-1"
    assert app_main.slideshow_public_url(request, "A" * 32) == "http://internal:8000/slideshow/" + "A" * 32 + ".mp4"
