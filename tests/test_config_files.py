import os

from app.config_files import load_env_file, read_text_file


def test_load_env_file_does_not_override_existing_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\nFROM_FILE=value\nEXISTING=file\nQUOTED=\"hello world\"\n", encoding="utf-8")
    monkeypatch.setenv("EXISTING", "process")
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)

    load_env_file(path)

    assert os.environ["FROM_FILE"] == "value"
    assert os.environ["EXISTING"] == "process"
    assert os.environ["QUOTED"] == "hello world"


def test_read_text_file_returns_trimmed_text_or_empty(tmp_path) -> None:
    path = tmp_path / "prompt.txt"
    path.write_text("\n prompt \n", encoding="utf-8")
    assert read_text_file(str(path)) == "prompt"
    assert read_text_file(None) == ""
    assert read_text_file(str(tmp_path / "missing.txt")) == ""
