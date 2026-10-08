import json
from collections import deque
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


def test_system_metrics_history_reads_only_the_needed_tail(monkeypatch, tmp_path) -> None:
    path = tmp_path / "system-metrics.jsonl"
    path.write_text("".join(json.dumps({"timestamp": value}) + "\n" for value in range(2_000)), encoding="utf-8")
    monkeypatch.setattr(main.settings, "system_metrics_file", path)
    monkeypatch.setattr(main.settings, "system_metrics_history_seconds", 10_000)
    monkeypatch.setattr(main.time, "time", lambda: 2_000)
    monkeypatch.setattr(main, "_system_metrics", deque(maxlen=10))

    main.load_system_metrics_history()

    assert len(main._system_metrics) == main._system_metrics.maxlen
    assert main._system_metrics[0]["timestamp"] == 2_000 - main._system_metrics.maxlen
    assert main._system_metrics[-1]["timestamp"] == 1_999
