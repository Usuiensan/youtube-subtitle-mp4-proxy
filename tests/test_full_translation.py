from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import srt

from app import translation_worker
from app.translation import TranslationError, TranslationSettings, translate_srt_with_local_worker, validate_translations


def subtitle(index: int, text: str) -> srt.Subtitle:
    return srt.Subtitle(index, timedelta(seconds=index), timedelta(seconds=index + 1), text)


def test_translation_prompt_flattens_subtitle_escape_markers() -> None:
    prompt = translation_worker.build_full_translation_prompt(
        {
            "source_language": "en",
            "target_language": "ja",
            "subtitles": [{"id": 1, "text": r"first\nsecond\hthird"}],
        }
    )
    assert r"first\nsecond" not in prompt
    assert '"text":"first second third"' in prompt


def test_validate_translations_requires_exact_ids_count_and_non_empty_text() -> None:
    target = [subtitle(1, "one"), subtitle(2, "two")]
    assert validate_translations(target, {"subtitles": [{"id": 1, "text": "一"}, {"id": 2, "text": "二"}]}) == {"1": "一", "2": "二"}
    for result in (
        {"subtitles": [{"id": 1, "text": "一"}]},
        {"subtitles": [{"id": 1, "text": "一"}, {"id": 1, "text": "重複"}]},
        {"subtitles": [{"id": 1, "text": "一"}, {"id": 2, "text": ""}]},
    ):
        with pytest.raises(TranslationError):
            validate_translations(target, result)


def test_llm_translation_calls_worker_once_for_the_whole_srt(tmp_path) -> None:
    source = tmp_path / "source.srt"
    output = tmp_path / "translated.srt"
    source.write_text(srt.compose([subtitle(1, "Hello"), subtitle(2, "World")]), encoding="utf-8")
    settings = TranslationSettings(
        enabled=True,
        target_window_seconds=120,
        target_max_events=10,
        context_before_seconds=120,
        context_before_max_events=25,
        context_after_seconds=120,
        context_after_max_events=25,
        model_name="test-model",
        engine="test_engine",
        fallback_engine="",
        glossary="",
        topic="",
        prompt_template="",
        google_project="",
        provider_name="openai_compatible",
        provider_endpoint="http://example.invalid/v1/chat/completions",
        provider_api_key="",
    )
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        return {"subtitles": [{"id": 1, "text": "こんにちは"}, {"id": 2, "text": "世界"}]}

    result = asyncio.run(
        translate_srt_with_local_worker(
            subtitle_path=source,
            output_path=output,
            video_title="title",
            channel_name="channel",
            source_language="en",
            target_language="ja",
            settings=settings,
            run_worker=worker,
        )
    )
    assert len(calls) == 1
    assert calls[0]["subtitles"] == [{"id": 1, "text": "Hello"}, {"id": 2, "text": "World"}]
    translated = list(srt.parse(output.read_text(encoding="utf-8")))
    assert [item.content for item in translated] == ["こんにちは", "世界"]
    assert result.metadata["translation_request_count"] == 1


def test_llm_validation_failure_keeps_provider_usage(tmp_path) -> None:
    source = tmp_path / "source.srt"
    output = tmp_path / "translated.srt"
    source.write_text(srt.compose([subtitle(1, "Hello"), subtitle(2, "World")]), encoding="utf-8")
    settings = TranslationSettings(
        enabled=True,
        target_window_seconds=120,
        target_max_events=10,
        context_before_seconds=120,
        context_before_max_events=25,
        context_after_seconds=120,
        context_after_max_events=25,
        model_name="gemini-3.1-flash-lite",
        engine="gemini_2_5_flash_lite",
        fallback_engine="",
        glossary="",
        topic="",
        prompt_template="",
        google_project="",
        provider_name="gemini_api",
        provider_endpoint="",
        provider_api_key="test-key",
    )

    async def worker(_payload: dict) -> dict:
        return {
            "subtitles": [{"id": 1, "text": "こんにちは"}],
            "_usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
        }

    with pytest.raises(TranslationError) as raised:
        asyncio.run(
            translate_srt_with_local_worker(
                subtitle_path=source,
                output_path=output,
                video_title="title",
                channel_name="channel",
                source_language="en",
                target_language="ja",
                settings=settings,
                run_worker=worker,
            )
        )

    assert raised.value.translation_usage == {
        "engine": "gemini_2_5_flash_lite",
        "model": "gemini-3.1-flash-lite",
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "requests": 1,
    }


def test_openai_request_uses_strict_json_schema(monkeypatch) -> None:
    requests: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"subtitles\\":[{\\"id\\":1,\\"text\\":\\"Hi\\"}]}"}}],"usage":{}}'

    def urlopen(request, timeout):
        requests.append({"body": json.loads(request.data.decode("utf-8")), "timeout": timeout})
        return Response()

    import json

    monkeypatch.setattr(translation_worker.urllib.request, "urlopen", urlopen)
    result, _usage = translation_worker.translate_batch_openai(
        {
            "llm_endpoint": "https://example.invalid/v1/chat/completions",
            "model_name": "test-model",
            "source_language": "en",
            "target_language": "ja",
            "subtitles": [{"id": 1, "text": "Hi"}],
        }
    )
    assert result == {"subtitles": [{"id": 1, "text": "Hi"}]}
    schema = requests[0]["body"]["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["subtitles"]
    assert "00:00" not in requests[0]["body"]["messages"][0]["content"]


def test_output_token_budget_grows_with_input_tokens(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "65536")
    short = {"subtitles": [{"id": 1, "text": "short"}]}
    long = {"subtitles": [{"id": 1, "text": "字幕" * 10000}]}
    assert translation_worker._output_token_budget("short", short) == 4096
    assert translation_worker._output_token_budget("long" * 20000, long) > 4096
