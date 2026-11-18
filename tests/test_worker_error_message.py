from app.main import worker_error_message


def test_short_message_is_unchanged() -> None:
    assert worker_error_message("boom") == "boom"


def test_long_message_keeps_head_marker_and_tail_retry_info() -> None:
    # 末尾だけ切り取ると先頭の "translation api http error 429:" が消え、
    # 429 の待機・再試行と短文表示がどちらも効かなくなる（2026-11-18 の実障害）。
    message = "translation api http error 429: " + ("x" * 4000) + '"retryDelay": "46s"'
    result = worker_error_message(message)
    assert result.startswith("translation api http error 429:")
    assert '"retryDelay": "46s"' in result
    assert len(result) < len(message)
