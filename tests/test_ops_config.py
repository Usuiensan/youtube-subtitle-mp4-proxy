import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as app_main
from app import ops_config


def test_ops_config_requires_separate_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_main.settings, "translation_config_api_token", "config-token")
    monkeypatch.setattr(ops_config, "config_file", lambda: tmp_path / "config.env")
    client = TestClient(app_main.app)
    assert client.get("/ops/config").status_code == 401
    assert client.get("/ops/config", headers={"X-Translation-Config-Token": "wrong"}).status_code == 401


def test_ops_config_allowlist_revision_atomic_write_and_audit(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.env"
    config.write_text("# keep\nLOCAL_LLM_MAX_OUTPUT_TOKENS=100\n", encoding="utf-8")
    audit = tmp_path / "config-audit.jsonl"
    monkeypatch.setattr(app_main.settings, "translation_config_api_token", "config-token")
    monkeypatch.setattr(ops_config, "config_file", lambda: config)
    monkeypatch.setattr(ops_config, "config_audit_file", lambda: audit)
    client = TestClient(app_main.app)
    headers = {"X-Translation-Config-Token": "config-token", "X-Operator": "agent"}
    current = client.get("/ops/config", headers=headers).json()
    assert current["values"]["LOCAL_LLM_MAX_OUTPUT_TOKENS"] == 100
    body = {"expected_revision": current["revision"], "values": {"LOCAL_LLM_MAX_OUTPUT_TOKENS": 200}}
    response = client.put("/ops/config", headers=headers, json=body)
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert "LOCAL_LLM_MAX_OUTPUT_TOKENS=200" in config.read_text(encoding="utf-8")
    record = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert record == {
        "timestamp": record["timestamp"],
        "key": "LOCAL_LLM_MAX_OUTPUT_TOKENS",
        "old_value": 100,
        "new_value": 200,
        "operator": "agent",
        "result": "updated",
    }
    assert client.put("/ops/config", headers=headers, json=body).status_code == 409
    assert client.put("/ops/config", headers=headers, json={"expected_revision": response.json()["revision"], "values": {"GEMINI_API_KEY": "secret"}}).status_code == 400


def test_ops_config_atomic_write_failure_preserves_file(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.env"
    config.write_text("LOCAL_LLM_MAX_OUTPUT_TOKENS=100\n", encoding="utf-8")
    monkeypatch.setattr(app_main.settings, "translation_config_api_token", "config-token")
    monkeypatch.setattr(ops_config, "config_file", lambda: config)
    client = TestClient(app_main.app)
    headers = {"X-Translation-Config-Token": "config-token"}
    revision = client.get("/ops/config", headers=headers).json()["revision"]
    with patch.object(ops_config.os, "replace", side_effect=OSError("test")):
        response = client.put("/ops/config", headers=headers, json={"expected_revision": revision, "values": {"LOCAL_LLM_MAX_OUTPUT_TOKENS": 200}})
    assert response.status_code == 500
    assert config.read_text(encoding="utf-8") == "LOCAL_LLM_MAX_OUTPUT_TOKENS=100\n"


def test_ops_config_allows_openai_chunk_token_limit(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.env"
    monkeypatch.setattr(app_main.settings, "translation_config_api_token", "config-token")
    monkeypatch.setattr(ops_config, "config_file", lambda: config)
    client = TestClient(app_main.app)
    headers = {"X-Translation-Config-Token": "config-token"}
    response = client.put(
        "/ops/config",
        headers=headers,
        json={"expected_revision": ops_config.revision(), "values": {"TRANSLATION_CHUNK_INPUT_TOKENS": 3500}},
    )
    assert response.status_code == 200
    assert "TRANSLATION_CHUNK_INPUT_TOKENS=3500" in config.read_text(encoding="utf-8")


def test_ops_config_revision_is_sha256(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.env"
    config.write_bytes(b"A=1\n")
    monkeypatch.setattr(ops_config, "config_file", lambda: config)
    assert ops_config.revision() == hashlib.sha256(b"A=1\n").hexdigest()
