import json

from app.json_files import read_json_object


def test_read_json_object_accepts_only_json_objects(tmp_path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"key": "value"}), encoding="utf-8")
    assert read_json_object(path) == {"key": "value"}

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_json_object(path) == {}


def test_read_json_object_handles_missing_and_invalid_files(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    assert read_json_object(missing) == {}
    missing.write_text("not-json", encoding="utf-8")
    assert read_json_object(missing) == {}
