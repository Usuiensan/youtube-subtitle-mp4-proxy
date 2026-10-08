import json
from io import StringIO

from app import main
from app.metrics import MetricsManager


def test_metrics_manager_records_and_averages_recent_values(tmp_path) -> None:
    path = tmp_path / "metrics.json"
    metrics = MetricsManager(path)

    metrics.record_download(100, 10)
    metrics.record_download(300, 10)

    assert metrics.get_avg("download_speed", 99) == 20
    assert json.loads(path.read_text(encoding="utf-8"))["download_speed"] == [10.0, 30.0]


def test_metrics_manager_ignores_invalid_file_and_can_reset(tmp_path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text("not-json", encoding="utf-8")
    metrics = MetricsManager(path)

    assert metrics.get_avg("encode_speed_ratio", 7) == 7
    metrics.record_encode(60, 20)
    metrics.reset()
    assert metrics.get_avg("encode_speed_ratio", 7) == 7


def test_system_metrics_history_is_streamed_instead_of_read_whole(monkeypatch) -> None:
    class MetricsFile:
        def exists(self) -> bool:
            return True

        def open(self, **_kwargs):
            return StringIO('{"timestamp":100,"cpu":1}\n{"timestamp":200,"cpu":2}\n')

        def read_text(self, **_kwargs):
            raise AssertionError("large metrics history must not be read into memory at once")

    monkeypatch.setattr(main.settings, "system_metrics_file", MetricsFile())
    monkeypatch.setattr(main.settings, "system_metrics_history_seconds", 150)
    monkeypatch.setattr(main.time, "time", lambda: 250)
    main._system_metrics.clear()

    main.load_system_metrics_history()

    assert [sample["timestamp"] for sample in main._system_metrics] == [100, 200]
