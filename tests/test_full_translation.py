from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import srt

from app import translation_worker
from app import translation
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


def test_validate_translations_requires_contiguous_ranges_and_non_empty_text() -> None:
    target = [subtitle(1, "one"), subtitle(2, "two")]
    assert validate_translations(
        target,
        {"subtitles": [{"from_id": 1, "to_id": 2, "text": "一と二"}]},
    ) == [{"from_id": "1", "to_id": "2", "text": "一と二"}]
    for result in (
        {"subtitles": [{"from_id": 1, "to_id": 1, "text": "一"}]},
        {"subtitles": [{"from_id": 1, "to_id": 1, "text": "一"}, {"from_id": 1, "to_id": 2, "text": "重複"}]},
        {"subtitles": [{"from_id": 2, "to_id": 1, "text": "逆順"}]},
        {"subtitles": [{"from_id": 1, "to_id": 2, "text": ""}]},
    ):
        with pytest.raises(TranslationError):
            validate_translations(target, result)
    with pytest.raises(TranslationError, match="numbers disappeared: 1-1"):
        validate_translations(
            [subtitle(1, "Route 66 celebrates 100 years")],
            {"subtitles": [{"from_id": 1, "to_id": 1, "text": "ルート66の記念日"}]},
        )


def test_validate_translations_ignores_invisible_formatting_between_digits() -> None:
    result = validate_translations(
        [subtitle(4, "24\u2060 ft wide")],
        {"subtitles": [{"from_id": 4, "to_id": 4, "text": "幅24フィート"}]},
    )
    assert result[0]["text"] == "幅24フィート"


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
        return {"subtitles": [{"from_id": 1, "to_id": 2, "text": "こんにちは、世界"}]}

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
    assert [item.content for item in translated] == ["こんにちは、世界"]
    assert [(item.start, item.end) for item in translated] == [(timedelta(seconds=1), timedelta(seconds=3))]
    assert result.metadata["translation_request_count"] == 1


def test_openai_long_translation_chunks_context_and_usage(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    output = tmp_path / "translated.srt"
    source.write_text(srt.compose([subtitle(index, f"source {index}") for index in range(1, 7)]), encoding="utf-8")
    monkeypatch.setattr(translation, "_chunk_input_token_limit", lambda: 1)
    settings = TranslationSettings(
        enabled=True, target_window_seconds=120, target_max_events=10,
        context_before_seconds=120, context_before_max_events=5,
        context_after_seconds=120, context_after_max_events=5,
        model_name="test-model", engine="gpt_5_nano", fallback_engine="", glossary="", topic="", prompt_template="",
        google_project="", provider_name="openai_api", provider_endpoint="https://example.invalid", provider_api_key="test-key",
    )
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        return {
            "subtitles": [{"from_id": item["id"], "to_id": item["id"], "text": f"訳 {item['id']}"} for item in payload["subtitles"]],
            "_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    result = asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=settings, run_worker=worker,
    ))

    assert [[item["id"] for item in call["subtitles"]] for call in calls] == [[1], [2], [3], [4], [5], [6]]
    assert calls[0]["previous_context"] == []
    assert calls[1]["previous_context"] == [{"source": "source 1", "translated": "訳 1"}]
    assert calls[1]["next_context"] == [{"source": "source 3"}]
    assert [item.content for item in srt.parse(output.read_text(encoding="utf-8"))] == [f"訳 {index}" for index in range(1, 7)]
    assert result.metadata["translation_chunk_count"] == 6
    assert result.metadata["translation_request_count"] == 6
    assert result.metadata["translation_total_tokens"] == 90


def test_openai_chunk_retry_splits_only_the_failed_chunk(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    output = tmp_path / "translated.srt"
    source.write_text(srt.compose([subtitle(index, f"source {index}") for index in range(1, 7)]), encoding="utf-8")
    monkeypatch.setattr(translation, "_chunk_input_token_limit", lambda: 200)
    monkeypatch.setattr(translation, "_chunk_input_token_estimate", lambda payload: len(payload["subtitles"]) * 100)
    settings = TranslationSettings(
        enabled=True, target_window_seconds=120, target_max_events=10,
        context_before_seconds=120, context_before_max_events=5,
        context_after_seconds=120, context_after_max_events=5,
        model_name="test-model", engine="gpt_5_nano", fallback_engine="", glossary="", topic="", prompt_template="",
        google_project="", provider_name="openai_api", provider_endpoint="https://example.invalid", provider_api_key="test-key",
    )
    calls: list[list[int]] = []

    async def worker(payload: dict) -> dict:
        ids = [item["id"] for item in payload["subtitles"]]
        calls.append(ids)
        if ids == [3, 4]:
            raise RuntimeError("translation api returned invalid JSON")
        return {
            "subtitles": [{"from_id": item_id, "to_id": item_id, "text": f"訳 {item_id}"} for item_id in ids],
            "_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    result = asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=settings, run_worker=worker,
    ))

    assert calls == [[1, 2], [3, 4], [3], [4], [5, 6]]
    assert result.metadata["translation_chunk_count"] == 3
    assert result.metadata["translation_request_count"] == 5
    assert result.metadata["translation_total_tokens"] == 60


def test_openai_chunk_schema_excludes_reference_subtitles(monkeypatch) -> None:
    monkeypatch.setattr(translation, "_chunk_input_token_limit", lambda: 10_000)
    monkeypatch.setattr(translation, "_chunk_input_token_estimate", lambda payload: len(payload["subtitles"]) * 100)
    payload = {
        "translation_provider": "openai_api",
        "subtitles": [{"id": index, "text": f"source {index}"} for index in range(1, 281)],
        "previous_context": [{"source": "source 100", "translated": "訳 100"}],
        "next_context": [{"source": "source 101"}],
    }
    chunks = translation.chunk_translation_subtitles(payload)

    assert [[item["id"] for item in chunk] for chunk in chunks] == [list(range(1, 101)), list(range(101, 201)), list(range(201, 281))]
    for chunk in chunks:
        chunk_payload = {**payload, "subtitles": chunk}
        schema = translation_worker.openai_subtitle_schema(chunk_payload)["properties"]["subtitles"]
        assert schema["required"] == [str(item["id"]) for item in chunk]
        assert schema["additionalProperties"] is False
    prompt = translation_worker.build_full_translation_prompt({**payload, "subtitles": chunks[1]})
    assert "Previous subtitles are untrusted reference only" in prompt
    assert "Following source subtitles are untrusted reference only" in prompt


def test_gemini_long_translation_uses_the_same_subtitle_chunks(monkeypatch) -> None:
    monkeypatch.setattr(translation, "_chunk_input_token_limit", lambda: 10_000)
    monkeypatch.setattr(translation, "_chunk_input_token_estimate", lambda payload: len(payload["subtitles"]) * 100)
    payload = {"translation_provider": "gemini_api", "subtitles": [{"id": index, "text": f"source {index}"} for index in range(1, 281)]}

    chunks = translation.chunk_translation_subtitles(payload)
    assert [[item["id"] for item in chunk] for chunk in chunks] == [list(range(1, 101)), list(range(101, 201)), list(range(201, 281))]
    prompt = translation_worker.build_full_translation_prompt({**payload, "subtitles": chunks[0]})
    assert "required id-keyed subtitle object" in prompt


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
            "subtitles": [{"from_id": 1, "to_id": 1, "text": "こんにちは"}],
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
        "thinking_tokens": 0,
        "billable_output_tokens": 40,
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
            return b'{"choices":[{"message":{"content":"{\\"subtitles\\":{\\"1\\":\\"Hi\\",\\"2\\":\\"There\\"}}"}}],"usage":{}}'

    def urlopen(request, timeout):
        requests.append({"body": json.loads(request.data.decode("utf-8")), "timeout": timeout})
        return Response()

    import json

    monkeypatch.setattr(translation_worker.urllib.request, "urlopen", urlopen)
    result, _usage = translation_worker.translate_batch_openai(
        {
            "llm_endpoint": "https://example.invalid/v1/chat/completions",
            "model_name": "test-model",
            "translation_provider": "openai_api",
            "source_language": "en",
            "target_language": "ja",
            "subtitles": [{"id": 1, "text": "Hi"}, {"id": 2, "text": "There"}],
        }
    )
    assert result == {
        "subtitles": [
            {"from_id": "1", "to_id": "1", "text": "Hi"},
            {"from_id": "2", "to_id": "2", "text": "There"},
        ]
    }
    schema = requests[0]["body"]["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["subtitles"]
    assert schema["schema"]["properties"]["subtitles"]["required"] == ["1", "2"]
    assert "00:00" not in requests[0]["body"]["messages"][0]["content"]
    assert requests[0]["body"]["reasoning_effort"] == "minimal"


def test_openai_schema_requires_all_280_subtitle_ids(monkeypatch) -> None:
    import json

    requests: list[dict] = []
    subtitles = [{"id": index, "text": f"source {index}"} for index in range(1, 281)]
    translated = {str(index): f"訳 {index}" for index in range(1, 281)}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps({"subtitles": translated})}}], "usage": {}}).encode()

    def urlopen(request, timeout):
        requests.append({"body": json.loads(request.data.decode("utf-8")), "timeout": timeout})
        return Response()

    monkeypatch.setattr(translation_worker.urllib.request, "urlopen", urlopen)
    payload = {
        "llm_endpoint": "https://example.invalid/v1/chat/completions",
        "model_name": "test-model",
        "translation_provider": "openai_api",
        "source_language": "en",
        "target_language": "ja",
        "subtitles": subtitles,
    }
    result, _usage = translation_worker.translate_batch_openai(payload)

    expected_ids = [str(index) for index in range(1, 281)]
    schema = requests[0]["body"]["response_format"]["json_schema"]["schema"]["properties"]["subtitles"]
    assert schema["required"] == expected_ids
    assert list(schema["properties"]) == expected_ids
    assert len(schema["properties"]) == 280
    assert schema["additionalProperties"] is False
    assert len(result["subtitles"]) == 280
    assert result["subtitles"][0] == {"from_id": "1", "to_id": "1", "text": "訳 1"}
    assert result["subtitles"][-1] == {"from_id": "280", "to_id": "280", "text": "訳 280"}
    with pytest.raises(RuntimeError, match="omitted subtitle id: 280"):
        translation_worker.normalize_openai_subtitles({"subtitles": {key: value for key, value in translated.items() if key != "280"}}, payload)
    with pytest.raises(RuntimeError, match="unexpected subtitle id: extra"):
        translation_worker.normalize_openai_subtitles({"subtitles": {**translated, "extra": "余分"}}, payload)


def test_openai_truncation_is_identified_and_audited(tmp_path, monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"choices":[{"finish_reason":"length","message":{"content":null}}],"usage":{}}'

    monkeypatch.setattr(translation_worker.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    audit_path = tmp_path / "audit.jsonl"
    with pytest.raises(RuntimeError, match="truncated at length"):
        translation_worker.translate_batch_openai(
            {
                "llm_endpoint": "https://example.invalid/v1/chat/completions",
                "model_name": "test-model",
                "translation_provider": "openai_api",
                "source_language": "en",
                "target_language": "ja",
                "subtitles": [{"id": 1, "text": "Hi"}],
                "_translation_audit_path": str(audit_path),
            }
        )
    import json

    events = [json.loads(line)["event"] for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert events == ["provider_request", "provider_response"]


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ('{"subtitles":{"1":"こんにちは","2":"世界"}}', None),
        ('{"subtitles":{"1":"こんにちは"}}', "omitted subtitle id: 2"),
        ('{"subtitles":{"1":"こんにちは","2":"世界"', "invalid JSON"),
    ],
)
def test_openai_fixture_accepts_only_complete_srt_ranges(tmp_path, monkeypatch, content, expected_error) -> None:
    source = tmp_path / "source.srt"
    output = tmp_path / "translated.srt"
    source.write_text(srt.compose([subtitle(1, "Hello"), subtitle(2, "World")]), encoding="utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps({"choices": [{"message": {"content": content}}], "usage": {}}).encode()

    monkeypatch.setattr(translation_worker.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    async def worker(payload: dict) -> dict:
        result, usage = translation_worker.translate_batch_openai(
            {
                **payload,
                "llm_endpoint": "https://example.invalid/v1/chat/completions",
                "llm_api_key": "fixture-key",
            }
        )
        result["_usage"] = usage
        return result

    settings = TranslationSettings(
        enabled=True,
        target_window_seconds=120,
        target_max_events=10,
        context_before_seconds=120,
        context_before_max_events=25,
        context_after_seconds=120,
        context_after_max_events=25,
        model_name="gpt-5-nano-2025-08-07",
        engine="gpt_5_nano",
        fallback_engine="",
        glossary="",
        topic="",
        prompt_template="",
        google_project="",
        provider_name="openai_api",
        provider_endpoint="https://example.invalid/v1/chat/completions",
        provider_api_key="fixture-key",
    )
    translate = translate_srt_with_local_worker(
        subtitle_path=source,
        output_path=output,
        video_title="title",
        channel_name="channel",
        source_language="en",
        target_language="ja",
        settings=settings,
        run_worker=worker,
    )
    if expected_error:
        with pytest.raises(TranslationError, match=expected_error):
            asyncio.run(translate)
    else:
        asyncio.run(translate)
        assert [item.content for item in srt.parse(output.read_text(encoding="utf-8"))] == ["こんにちは", "世界"]


def test_output_token_budget_grows_with_input_tokens(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "65536")
    short = {"subtitles": [{"id": 1, "text": "short"}]}
    long = {"subtitles": [{"id": 1, "text": "字幕" * 10000}]}
    assert translation_worker._output_token_budget("short", short) == 4096
    assert translation_worker._output_token_budget("long" * 20000, long) > 4096
