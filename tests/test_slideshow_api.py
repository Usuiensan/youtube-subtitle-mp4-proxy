import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as app_main


def _fake_image(name: str = "slide.png", body: bytes = b"image") -> tuple[str, tuple[str, bytes, str]]:
    return ("images", (name, b"\x89PNG\r\n\x1a\n" + body, "image/png"))


def test_index_contains_slideshow_form() -> None:
    with TestClient(app_main.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert 'id="slideshowForm"' in response.text
    assert 'id="slideshowPdf"' in response.text
    assert 'id="slideshowImages"' in response.text
    assert 'id="slideshowDuration"' in response.text
    assert 'min="1" max="300" step="1" value="3"' in response.text
    assert "VRChatに貼るURL" in response.text


def test_slideshow_upload_job_public_range_and_ttl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    monkeypatch.setattr(app_main.settings, "slideshow_ttl_seconds", 86400)
    app_main._prepare_jobs.clear()

    async def fake_convert(source, output_path, **kwargs):
        output_path.write_bytes(b"0123456789")
        assert [path.read_bytes()[-1:] for path in source] == [b"1", b"2", b"0"]
        return output_path

    monkeypatch.setattr(app_main, "convert_slideshow", fake_convert)
    with TestClient(app_main.app) as client:
        response = client.post(
            "/slideshow",
            files=[_fake_image("10.png", b"0"), _fake_image("2.png", b"2"), _fake_image("1.png", b"1")],
            data={"slide_duration": "2"},
            headers={"Authorization": "Bearer token"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert len(body["slideshow_id"]) == 32
        assert body["slideshow_id"] != body["job_id"]

        for _ in range(20):
            status = client.get(body["status_url"], headers={"Authorization": "Bearer token"})
            if status.json()["status"] == "ready":
                break
            time.sleep(0.01)
        assert status.json()["status"] == "ready"
        url = status.json()["url"]
        public = client.get(url)
        assert public.status_code == 200
        assert public.headers["content-type"] == "video/mp4"
        assert public.headers["accept-ranges"] == "bytes"
        assert client.get("/slideshow/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.mp4").status_code == 404
        ranged = client.get(url, headers={"Range": "bytes=2-5"})
        assert ranged.status_code == 206
        assert ranged.content == b"2345"
        assert ranged.headers["content-range"] == "bytes 2-5/10"
        second = client.post(
            "/slideshow",
            files=[_fake_image("another.png")],
            headers={"Authorization": "Bearer token"},
        )
        assert second.status_code == 202
        assert second.json()["slide_duration"] == 3.0
        assert second.json()["slideshow_id"] != body["slideshow_id"]

        stale = app_main.settings.slideshow_dir / f"{body['slideshow_id']}.mp4"
        stale.touch()
        old = time.time() - 10
        import os

        os.utime(stale, (old, old))
        monkeypatch.setattr(app_main.settings, "slideshow_ttl_seconds", 1)
        assert client.get(url).status_code == 404
        assert not stale.exists()
        assert client.get(f"/slideshow/{body['slideshow_id']}.mp4?download=1").status_code == 404
        assert client.get("/slideshow/../escape.mp4").status_code == 404


def test_slideshow_accepts_a_pdf_upload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    app_main._prepare_jobs.clear()

    async def fake_convert(source, output_path, **kwargs):
        assert source.suffix == ".pdf"
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(app_main, "convert_slideshow", fake_convert)
    with TestClient(app_main.app) as client:
        response = client.post(
            "/slideshow",
            files=[("pdf", ("source.pdf", b"%PDF-1.7 fake", "application/octet-stream"))],
            headers={"Authorization": "Bearer token"},
        )
        assert response.status_code == 202
        assert response.json()["input_type"] == "pdf"


def test_slideshow_rejects_auth_ambiguous_and_invalid_inputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    app_main._prepare_jobs.clear()
    with TestClient(app_main.app) as client:
        assert client.post("/slideshow", files=[_fake_image()], data={"slide_duration": "2"}).status_code == 401
        headers = {"Authorization": "Bearer token"}
        assert client.post("/slideshow", headers=headers).status_code == 400
        assert client.post("/slideshow", files=[_fake_image()], data={"slide_duration": "0"}, headers=headers).status_code == 400
        assert client.post("/slideshow", files=[_fake_image()], data={"slide_duration": "1.5"}, headers=headers).status_code == 400
        assert client.post(
            "/slideshow",
            files=[("pdf", ("source.pdf", b"%PDF-1.7", "application/pdf")), _fake_image()],
            headers=headers,
        ).status_code == 400
        assert client.post(
            "/slideshow",
            files=[("images", ("bad.png", b"not-an-image", "image/png"))],
            headers=headers,
        ).status_code == 400
        assert client.post(
            "/slideshow",
            files=[("images", ("bad.gif", b"\x89PNG\r\n\x1a\nimage", "image/gif"))],
            headers=headers,
        ).status_code == 400


def test_slideshow_rejects_too_many_images(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    monkeypatch.setattr(app_main.settings, "slideshow_max_files", 1)
    with TestClient(app_main.app) as client:
        response = client.post(
            "/slideshow",
            files=[_fake_image("1.png"), _fake_image("2.png")],
            headers={"Authorization": "Bearer token"},
        )
    assert response.status_code == 400


def test_slideshow_rejects_upload_over_limit_and_cleans_workdir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    monkeypatch.setattr(app_main.settings, "slideshow_max_input_bytes", 10)
    with TestClient(app_main.app) as client:
        response = client.post(
            "/slideshow",
            files=[_fake_image("1.png", b"0123456789")],
            headers={"Authorization": "Bearer token"},
        )
    assert response.status_code == 413
    assert not list((tmp_path / "slideshows").glob(".work-*"))


def test_slideshow_rejects_total_duration_for_images_and_pdf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    monkeypatch.setattr(app_main.settings, "slideshow_max_total_duration_seconds", 4)
    app_main._prepare_jobs.clear()

    async def fake_convert(source, output_path, **kwargs):
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(app_main, "convert_slideshow", fake_convert)
    with TestClient(app_main.app) as client:
        image_response = client.post(
            "/slideshow",
            files=[_fake_image("1.png"), _fake_image("2.png")],
            data={"slide_duration": "3"},
            headers={"Authorization": "Bearer token"},
        )
        assert image_response.status_code == 400
        assert "Total slideshow duration" in image_response.json()["detail"]

        monkeypatch.setattr(app_main, "pdf_page_count", lambda _path: _async_value(2))
        pdf_response = client.post(
            "/slideshow",
            files=[("pdf", ("source.pdf", b"%PDF-1.7 fake", "application/pdf"))],
            data={"slide_duration": "3"},
            headers={"Authorization": "Bearer token"},
        )
    assert pdf_response.status_code == 400
    assert "Total slideshow duration" in pdf_response.json()["detail"]


async def _async_value(value):
    return value


def test_slideshow_capacity_check_precedes_conversion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    app_main._prepare_jobs.clear()
    required: list[int] = []

    async def fake_capacity(value: int) -> None:
        required.append(value)

    async def fake_convert(source, output_path, **kwargs):
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(app_main, "ensure_prepare_workspace_capacity", fake_capacity)
    monkeypatch.setattr(app_main, "convert_slideshow", fake_convert)
    with TestClient(app_main.app) as client:
        response = client.post(
            "/slideshow",
            files=[_fake_image("1.png", b"0123456789")],
            data={"slide_duration": "2"},
            headers={"Authorization": "Bearer token"},
        )
        assert response.status_code == 202
        for _ in range(20):
            if client.get(response.json()["status_url"], headers={"Authorization": "Bearer token"}).json()["status"] == "ready":
                break
            time.sleep(0.01)
    assert required
    assert required[0] > 10


def test_slideshow_cache_control_uses_remaining_ttl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    monkeypatch.setattr(app_main.settings, "slideshow_ttl_seconds", 86400)
    slideshow_id = "A" * 32
    path = app_main.settings.slideshow_dir / f"{slideshow_id}.mp4"
    path.parent.mkdir(parents=True)

    for age, upper_bound in ((0, 86400), (43200, 43200), (86399, 1)):
        path.write_bytes(b"mp4")
        os.utime(path, (time.time() - age, time.time() - age))
        with TestClient(app_main.app) as client:
            response = client.get(f"/slideshow/{slideshow_id}.mp4")
        assert response.status_code == 200
        max_age = int(response.headers["cache-control"].split("=")[-1])
        assert 0 <= max_age <= upper_bound

    path.write_bytes(b"mp4")
    expired = time.time() - 86401
    os.utime(path, (expired, expired))
    with TestClient(app_main.app) as client:
        response = client.get(f"/slideshow/{slideshow_id}.mp4")
    assert response.status_code == 404
    assert not path.exists()
