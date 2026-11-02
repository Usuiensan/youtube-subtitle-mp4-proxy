from __future__ import annotations

import asyncio
from datetime import timedelta

import srt

from app import translation, translation_worker
from app.translation import TranslationSettings, translate_srt_with_local_worker


def cue(index: int, start: float, end: float, text: str) -> srt.Subtitle:
    return srt.Subtitle(index, timedelta(seconds=start), timedelta(seconds=end), text)


def config() -> TranslationSettings:
    return TranslationSettings(
        True, 120, 10, 120, 5, 120, 5, "test-model", "test-engine", "", "", "", "", "",
        "openai_compatible", "https://example.invalid", "test-key",
    )


def test_single_split_candidate_falls_back_and_is_recorded(tmp_path, monkeypatch) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source = [cue(1, 0, 1, "One"), cue(2, 1, 2, "two")]
    source_path.write_text(srt.compose(source, reindex=False), encoding="utf-8")
    candidate = translation.TranslationSplit(("自然な", "字幕です。"), 1, True)
    monkeypatch.setattr(translation, "split_group_translation_candidates", lambda *_args: [candidate])

    async def worker(payload: dict) -> dict:
        return {"groups": [{"group_id": "g001", "translation": "自然な字幕です。"}]}

    result = asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=config(), run_worker=worker,
    ))
    translated = list(srt.parse(output.read_text(encoding="utf-8")))
    assert [item.content for item in translated] == ["自然な", "字幕です。"]
    assert [(item.index, item.start, item.end) for item in translated] == [
        (item.index, item.start, item.end) for item in source
    ]
    assert result.metadata["translation_split_fallback_groups"] == ["g001"]


def test_single_subtitle_speed_fallback_does_not_fail(tmp_path) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source = [cue(1, 0, 0.5, "One")]
    source_path.write_text(srt.compose(source), encoding="utf-8")

    async def worker(payload: dict) -> dict:
        return {"groups": [{"group_id": "g001", "translation": "これは表示速度を超える長い訳文です。"}]}

    result = asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=config(), run_worker=worker,
    ))
    translated = list(srt.parse(output.read_text(encoding="utf-8")))
    assert len(translated) == 1
    assert translated[0].content == "これは表示速度を超える長い訳文です。"
    assert result.metadata["translation_split_fallback_groups"] == ["g001"]


def test_normal_request_has_target_length_and_concise_keeps_existing_field(tmp_path) -> None:
    source_path, output = tmp_path / "source.srt", tmp_path / "translated.srt"
    source_path.write_text(srt.compose([cue(1, 0, 1, "One"), cue(2, 1, 3, "two")]), encoding="utf-8")
    calls: list[dict] = []

    async def worker(payload: dict) -> dict:
        calls.append(payload)
        if payload.get("concise"):
            return {"groups": [{"group_id": "g001", "translation": "短い訳。"}]}
        return {"groups": [{"group_id": "g001", "translation": "自然な通常の訳文です。"}]}

    asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=config(), run_worker=worker,
    ))
    normal = calls[0]
    assert normal["target_max_characters"] == {"g001": 42}
    assert "concise_max_characters" not in normal

    async def concise_worker(payload: dict) -> dict:
        calls.append(payload)
        text = "短い訳。" if payload.get("concise") else "これは表示速度を超える長い訳文です。"
        return {"groups": [{"group_id": "g001", "translation": text}]}

    # The concise payload is exercised through the overlong single-cue path.
    source_path.write_text(srt.compose([cue(1, 0, 0.5, "One")]), encoding="utf-8")
    asyncio.run(translate_srt_with_local_worker(
        subtitle_path=source_path, output_path=output, video_title="title", channel_name="channel",
        source_language="en", target_language="ja", settings=config(), run_worker=concise_worker,
    ))
    concise = calls[-1]
    assert concise["concise"] is True
    assert concise["concise_max_characters"] == {"g001": 7}
    assert "target_max_characters" not in concise


def test_worker_prompt_has_optional_target_length_guidance() -> None:
    base = {"source_language": "en", "target_language": "ja", "groups": []}
    prompt = translation_worker.build_full_translation_prompt({**base, "groups": [{"group_id": "g001", "source": []}]})
    assert "target character" not in prompt
    guided = translation_worker.build_full_translation_prompt({
        **base, "groups": [{"group_id": "g001", "source": []}], "target_max_characters": {"g001": 42}
    })
    assert 'target character guideline for each group: {"g001":42}' in guided
    assert "not a strict limit" in guided

