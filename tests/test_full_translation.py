from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import srt

from app import translation, translation_worker
from app.translation import TranslationError, TranslationSettings, batch_translation_groups, build_meaning_groups, split_group_translation, translate_srt_with_local_worker, validate_group_translations


def cue(index: int, start: float, end: float, text: str) -> srt.Subtitle:
    return srt.Subtitle(index, timedelta(seconds=start), timedelta(seconds=end), text)


def config(provider: str = "openai_compatible") -> TranslationSettings:
    return TranslationSettings(True, 120, 10, 120, 5, 120, 5, "test-model", "test-engine", "", "", "", "", "", provider, "https://example.invalid", "test-key")


def test_meaning_groups_prefer_sentence_and_gap_boundaries() -> None:
    groups = build_meaning_groups([
        cue(1, 0, 1, "If possible,"), cue(2, 1, 2.5, "remove the hose."),
        cue(3, 3.4, 4.4, "Next,"), cue(4, 4.4, 6.2, "remove the clamp."),
    ])
    assert [[item.index for item in group.subtitles] for group in groups] == [[1, 2], [3, 4]]
    assert [group.group_id for group in groups] == ["g001", "g002"]


def test_meaning_groups_record_forced_boundary_without_splitting_an_id() -> None:
    groups = build_meaning_groups([cue(index, index - 1, index, f"fragment {index}") for index in range(1, 11)])
    assert [item.index for group in groups for item in group.subtitles] == list(range(1, 11))
    assert max(len(group.subtitles) for group in groups) <= 8
    assert any(group.forced_split for group in groups)


def test_api_batches_never_split_a_meaning_group(monkeypatch) -> None:
    groups = build_meaning_groups([cue(1, 0, 1, "One."), cue(2, 1, 2, "Two."), cue(3, 2, 3, "Three.")])
    monkeypatch.setattr(translation, "_chunk_input_token_limit", lambda: 1)
    assert [[group.group_id for group in batch] for batch in batch_translation_groups({"translation_provider": "gemini_api"}, groups)] == [["g001"], ["g002"], ["g003"]]


def test_python_split_preserves_translation_and_weights_by_duration() -> None:
    source = (cue(10, 0, 1, "short"), cue(11, 1, 4, "longer"))
    result = split_group_translation("可能ならラジエーターの下側ホースを外します", source)
    assert "".join(result.segments) == "可能ならラジエーターの下側ホースを外します"
    assert len(result.segments[1]) > len(result.segments[0])


def test_python_split_rejects_translation_too_short_for_non_empty_ids() -> None:
    with pytest.raises(TranslationError, match="too short"):
        split_group_translation("短文", tuple(cue(i, i, i + 1, "x") for i in range(1, 5)))


def test_group_validation_flags_only_bad_group() -> None:
    groups = build_meaning_groups([cue(1, 0, 1, "Route 66 ends here."), cue(2, 2, 3, "Next step.")])
    accepted, invalid = validate_group_translations(groups, {"groups": [
        {"group_id": "g001", "translation": "Route 66はここで終わります。"},
        {"group_id": "g002", "translation": "翻訳なし"},
    ]})
    assert accepted == {"g001": "Route 66はここで終わります。"}
    assert invalid == ["g002"]


def test_group_validation_allows_a_spelled_number_to_become_digits() -> None:
    groups = build_meaning_groups([cue(1, 0, 2, "Seis líquidos mantienen vivo a tu auto.")])
    accepted, invalid = validate_group_translations(groups, {
        "groups": [{"group_id": "g001", "translation": "6種類のフルードが車を動かしています。"}]
    })
    assert accepted == {"g001": "6種類のフルードが車を動かしています。"}
    assert invalid == []


def test_prompt_uses_groups_and_flattens_escape_markers() -> None:
    prompt = translation_worker.build_full_translation_prompt({"source_language": "en", "target_language": "ja", "groups": [
        {"group_id": "g001", "source": [{"id": 1, "text": r"first\nsecond\hthird"}]}
    ]})
    assert r"first\nsecond" not in prompt
    assert '"text":"first second third"' in prompt
    assert "Do not translate individual subtitle ids separately" in prompt


def test_pipeline_keeps_every_original_id_and_time(tmp_path) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source = [cue(10, 0, 1, "If possible,"), cue(11, 1, 3, "remove the hose.")]
    source_path.write_text(srt.compose(source, reindex=False), encoding="utf-8")
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        return {"groups": [{"group_id": "g001", "translation": "可能なら、下側のホースを取り外します。"}]}

    result = asyncio.run(translate_srt_with_local_worker(subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel", source_language="en", target_language="ja", settings=config(), run_worker=worker))
    translated = list(srt.parse(output.read_text(encoding="utf-8")))
    assert [item.index for item in translated] == [10, 11]
    assert [(item.start, item.end) for item in translated] == [(item.start, item.end) for item in source]
    assert "".join(item.content for item in translated) == "可能なら、下側のホースを取り外します。"
    assert "subtitles" not in calls[0]
    assert result.metadata["translation_group_count"] == 1


def test_invalid_group_is_retried_without_retranslating_valid_group(tmp_path) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source_path.write_text(srt.compose([cue(1, 0, 2, "First sentence."), cue(2, 3, 5, "Second sentence.")]), encoding="utf-8")
    calls: list[list[str]] = []

    async def worker(payload: dict) -> dict:
        ids = [item["group_id"] for item in payload["groups"]]
        calls.append(ids)
        if len(ids) == 2:
            return {"groups": [{"group_id": "g001", "translation": "最初の文です。"}, {"group_id": "g002", "translation": "翻訳なし"}]}
        return {"groups": [{"group_id": ids[0], "translation": "次の文です。"}]}

    asyncio.run(translate_srt_with_local_worker(subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel", source_language="en", target_language="ja", settings=config(), run_worker=worker))
    assert calls == [["g001", "g002"], ["g002"]]


def test_difficult_split_asks_llm_to_choose_without_retranslation(tmp_path, monkeypatch) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source_path.write_text(srt.compose([cue(1, 0, 1, "One"), cue(2, 1, 2, "two")]), encoding="utf-8")
    first = translation.TranslationSplit(("自然な", "字幕です。"), 1, True)
    second = translation.TranslationSplit(("自然な字幕", "です。"), 2, True)
    monkeypatch.setattr(translation, "split_group_translation_candidates", lambda *_args: [first, second])
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        return {"candidate_index": 1} if payload.get("task") else {"groups": [{"group_id": "g001", "translation": "自然な字幕です。"}]}

    asyncio.run(translate_srt_with_local_worker(subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel", source_language="en", target_language="ja", settings=config(), run_worker=worker))
    assert calls[1]["translation"] == "自然な字幕です。"
    assert calls[1]["candidates"] == [["自然な", "字幕です。"], ["自然な字幕", "です。"]]
    assert [item.content for item in srt.parse(output.read_text(encoding="utf-8"))] == ["自然な字幕", "です。"]


def test_split_does_not_overprefer_early_punctuation() -> None:
    subtitles = (
        cue(63, 166.020, 168.242, "Bien. Mientras espero que eso gotee,"),
        cue(64, 168.724, 169.554, "voy a hacer"),
        cue(65, 170.043, 173.071, "un pequeño prellenado del filtro."),
    )
    result = split_group_translation("さて、滴り落ちるのを待つ間に、フィルターを少し予備充填しておきます。", subtitles)

    assert not result.difficult
    assert "".join(result.segments) == "さて、滴り落ちるのを待つ間に、フィルターを少し予備充填しておきます。"


def test_overlong_single_id_is_retranslated_concisely(tmp_path) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source_path.write_text(srt.compose([cue(1, 0, 0.8, "I like it a lot.")]), encoding="utf-8")
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        text = "お気に入りです。" if payload.get("concise_max_characters") == {"g001": 11} else "私はこの道具をとても気に入っています。"
        return {"groups": [{"group_id": "g001", "translation": text}]}

    asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=config(), run_worker=worker,
    ))
    assert [call.get("concise") for call in calls] == [False, True]
    assert calls[1]["concise_max_characters"] == {"g001": 11}
    assert 'must not exceed this character limit: {"g001":11}' in translation_worker.build_full_translation_prompt(calls[1])
    assert [item.content for item in srt.parse(output.read_text(encoding="utf-8"))] == ["お気に入りです。"]


def test_openai_request_uses_group_array_schema(monkeypatch) -> None:
    requests: list[dict] = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return json.dumps({"choices": [{"message": {"content": '{"groups":[{"group_id":"g001","translation":"こんにちは。"}]}'}}], "usage": {}}).encode()

    def urlopen(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(translation_worker.urllib.request, "urlopen", urlopen)
    result, _usage = translation_worker.translate_batch_openai({"llm_endpoint": "https://example.invalid", "model_name": "test", "translation_provider": "openai_api", "source_language": "en", "target_language": "ja", "groups": [{"group_id": "g001", "source": [{"id": 1, "text": "Hello."}]}]})
    schema = requests[0]["response_format"]["json_schema"]
    assert result["groups"][0]["group_id"] == "g001"
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["groups"]
    assert "subtitles" not in schema["schema"]["properties"]


def test_incident_range_keeps_ids_and_uses_groups(tmp_path) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    cues = [cue(index, index - 331, index - 330, "fragment") for index in range(331, 440)]
    source_path.write_text(srt.compose(cues, reindex=False), encoding="utf-8")
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        if payload.get("task"):
            return {"candidate_index": 0}
        return {"groups": [{"group_id": group["group_id"], "translation": "時間対応を検証する完成字幕" + chr(12354 + position) + "です。"} for position, group in enumerate(payload["groups"])]}

    asyncio.run(translate_srt_with_local_worker(subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel", source_language="es", target_language="ja", settings=config("gemini_api"), run_worker=worker))
    translation_calls = [call for call in calls if not call.get("task")]
    assert all("groups" in call and "subtitles" not in call for call in translation_calls)
    assert max(len(call["groups"]) for call in translation_calls) < 109
    translated = list(srt.parse(output.read_text(encoding="utf-8")))
    assert [item.index for item in translated] == list(range(331, 440))
    assert [(item.start, item.end) for item in translated] == [(item.start, item.end) for item in cues]


def test_output_token_budget_grows_with_input_tokens(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "65536")
    groups = [{"group_id": "g001", "source": [{"id": 1, "text": "short"}]}]
    assert translation_worker._output_token_budget("short", {"groups": groups}) == 4096
    assert translation_worker._output_token_budget("long" * 20000, {"groups": groups}) > 4096
