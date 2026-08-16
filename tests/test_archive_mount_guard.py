from pathlib import Path

import pytest
from fastapi import HTTPException

from app import main


def test_archive_storage_requires_the_configured_mount(monkeypatch, tmp_path: Path) -> None:
    mount = tmp_path / "hdd"
    archive = mount / "archive"
    hot = tmp_path / "hot"
    hot_entry = hot / "item"
    hot_entry.mkdir(parents=True)
    (hot_entry / "output.mp4").write_bytes(b"video")
    monkeypatch.setattr(main.settings, "cache_hot_dir", hot)
    monkeypatch.setattr(main.settings, "cache_archive_dir", archive)
    monkeypatch.setattr(main.settings, "cache_archive_mount_point", mount)
    monkeypatch.setattr(main.os.path, "ismount", lambda _path: False)

    assert main.archive_storage_available() is False
    assert main.archive_entry_dir("item") is None
    assert main.archive_cache_entry("item") is False
    assert hot_entry.exists()
    with pytest.raises(HTTPException, match="not mounted"):
        main.archive_all_hot_entries(set())


def test_archive_storage_accepts_only_its_configured_mount(monkeypatch, tmp_path: Path) -> None:
    mount = tmp_path / "hdd"
    archive = mount / "archive"
    monkeypatch.setattr(main.settings, "cache_archive_dir", archive)
    monkeypatch.setattr(main.settings, "cache_archive_mount_point", mount)
    monkeypatch.setattr(main.os.path, "ismount", lambda path: path == mount)

    assert main.archive_storage_available() is True
    assert main.archive_entry_dir("item") == archive / "item"


def test_storage_status_reports_active_jobs(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.settings, "discord_prepare_token", "prepare-token")
    monkeypatch.setattr(main, "current_job_summaries", lambda: [{"job_id": "active", "status": "running"}])
    response = TestClient(main.app).get(
        "/prepare/storage-status",
        headers={"Authorization": "Bearer prepare-token"},
    )
    assert response.status_code == 200
    assert response.json()["active_jobs"] == 1
