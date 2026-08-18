import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as app_main


def _fake_image(name: str = "slide.png") -> tuple[str, tuple[str, bytes, str]]:
    return ("images", (name, b"\x89PNG\r\n\x1a\nimage", "image/png"))


def test_slideshow_upload_job_public_range_and_ttl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(app_main.settings, "slideshow_dir", tmp_path / "slideshows")
    monkeypatch.setattr(app_main.settings, "slideshow_ttl_seconds", 86400)
    app_main._prepare_jobs.clear()

    async def fake_convert(source, output_path, **kwargs):
        output_path.write_bytes(b"0123456789")
        return output_path

    monkeypatch.setattr(app_main, "convert_slideshow", fake_convert)
    with TestClient(app_main.app) as client:
        response = client.post("/slideshow", files=[_fake_image("2.png"), _fake_image("10.png")], data={"slide_duration": "2"}, headers={"Authorization": "Bearer token"})
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
        assert second.json()["slideshow_id"] != body["slideshow_id"]

        stale = app_main.settings.slideshow_dir / f"{body['slideshow_id']}.mp4"
        stale.touch()
        old = time.time() - 10
        import os

        os.utime(stale, (old, old))
        monkeypatch.setattr(app_main.settings, "slideshow_ttl_seconds", 1)
        assert client.get(url).status_code == 404
        assert not stale.exists()


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
