import json

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
