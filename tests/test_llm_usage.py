from app.llm_usage import record, summary


def test_llm_usage_accumulates_tokens_by_month(tmp_path) -> None:
    path = tmp_path / "llm-usage.json"

    record(path, "gemini_2_5_flash_lite", 10, 4, 14, 0.000002, 0.0, month="2026-08")
    usage = record(path, "gemini_2_5_flash_lite", 3, 2, 5, 0.000001, 0.0, month="2026-08")

    assert usage["input_tokens"] == 13
    assert usage["output_tokens"] == 6
    assert usage["total_tokens"] == 19
    assert summary(path, month="2026-09")["total_tokens"] == 0
