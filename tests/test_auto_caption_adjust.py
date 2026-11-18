from datetime import timedelta

import srt

from app.auto_caption_adjust import adjust_auto_captions


def subtitle(index: int, start: float, end: float, text: str) -> srt.Subtitle:
    return srt.Subtitle(index, timedelta(seconds=start), timedelta(seconds=end), text)


def test_adjusts_sentences_and_maps_boundaries_to_monotonic_times() -> None:
    source = [subtitle(1, 0, 2, "hello world"), subtitle(2, 2, 4, "this works")]

    def llm_call(payload: dict) -> dict:
        assert payload["windows"][0]["cues"] == [
            {"index": 0, "text": "hello world"},
            {"index": 1, "text": "this works"},
        ]
        return {
            "windows": [{"window_id": "w001", "sentences": [
                {"start_cue": 0, "text": "Hello world."},
                {"start_cue": 1, "text": "This works."},
            ]}],
            "_usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        }

    adjusted, report = adjust_auto_captions(
        source, source_language="en", llm_call=llm_call, model_name="test-model"
    )

    assert [item.content for item in adjusted] == ["Hello world.", "This works."]
    assert all(item.start <= item.end for item in adjusted)
    assert all(adjusted[i].end <= adjusted[i + 1].start for i in range(len(adjusted) - 1))
    assert report["windows_adjusted"] == 1
    assert report["usage"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}


def test_falls_back_for_dissimilar_output_and_llm_exception() -> None:
    source = [subtitle(1, 0, 2, "hello world")]

    adjusted, report = adjust_auto_captions(
        source,
        source_language="en",
        llm_call=lambda _payload: {"windows": [{"window_id": "w001", "sentences": [{"start_cue": 0, "text": "completely unrelated"}]}]},
    )
    assert [item.content for item in adjusted] == ["hello world"]
    assert report["windows_fallback"] == 1

    adjusted, report = adjust_auto_captions(
        source,
        source_language="en",
        llm_call=lambda _payload: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert [item.content for item in adjusted] == ["hello world"]
    assert report["windows_fallback"] == 1


def test_splits_windows_by_subtitle_end_time() -> None:
    source = [subtitle(index, index * 30, index * 30 + 30, f"cue {index}") for index in range(4)]
    calls: list[dict] = []

    def llm_call(payload: dict) -> dict:
        calls.append(payload)
        return {"windows": [
            {"window_id": window["window_id"], "sentences": [{"start_cue": window["cues"][0]["index"], "text": window["cues"][0]["text"]}]}
            for window in payload["windows"]
        ], "_usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}

    _adjusted, report = adjust_auto_captions(source, source_language="en", llm_call=llm_call)
    assert report["windows_total"] == 2
    # 2窓は1リクエストにまとめて送る（窓ごとの連射でAPIの分あたり上限を踏まない）
    assert len(calls) == 1
    assert [len(payload["windows"]) for payload in calls] == [2]
    assert [len(window["cues"]) for window in calls[0]["windows"]] == [2, 2]
    assert report["usage"] == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}


def test_batches_windows_up_to_four_per_request() -> None:
    source = [subtitle(index, index * 30, index * 30 + 30, f"cue {index}") for index in range(10)]
    calls: list[dict] = []

    def llm_call(payload: dict) -> dict:
        calls.append(payload)
        return {"windows": [
            {"window_id": window["window_id"], "sentences": [{"start_cue": window["cues"][0]["index"], "text": window["cues"][0]["text"]}]}
            for window in payload["windows"]
        ]}

    _adjusted, report = adjust_auto_captions(source, source_language="en", llm_call=llm_call)
    assert report["windows_total"] == 5
    assert [len(payload["windows"]) for payload in calls] == [4, 1]
    assert report["windows_adjusted"] == 5


def test_renumbers_adjusted_and_fallback_cues_across_windows() -> None:
    source = [subtitle(index, index * 30, index * 30 + 30, f"cue {index}") for index in range(4)]

    def llm_call(payload: dict) -> dict:
        # w002 は応答から欠落させる。その窓だけがフォールバックする
        return {"windows": [{
            "window_id": "w001",
            "sentences": [{"start_cue": 0, "text": "cue 0 adjusted"}, {"start_cue": 1, "text": "cue 1"}],
        }]}

    adjusted, report = adjust_auto_captions(source, source_language="en", llm_call=llm_call)

    assert [item.index for item in adjusted] == [1, 2, 3, 4]
    assert [item.content for item in adjusted] == ["cue 0 adjusted", "cue 1", "cue 2", "cue 3"]
    assert report["windows_adjusted"] == 1
    assert report["windows_fallback"] == 1


def test_sentences_may_share_a_start_cue() -> None:
    # 実例: 1キューに2文入っていると LLM は同じ start_cue で2文返す。
    # 重複を理由に窓ごと捨てると、正しく再構成された文が全部失われる。
    source = [
        subtitle(1, 0, 2, 'said, "Fine, you abolished the law, but'),
        subtitle(2, 2, 4, 'we will continue the policy anyway,"'),
        subtitle(3, 4, 6, "Answer me. Go read the stance"),
        subtitle(4, 6, 8, "on nuclear energy."),
    ]

    def llm_call(payload: dict) -> dict:
        return {"windows": [{"window_id": "w001", "sentences": [
            {"start_cue": 0, "text": 'Said, "Fine, you abolished the law, but we will continue the policy anyway,"'},
            {"start_cue": 2, "text": "Answer me."},
            {"start_cue": 2, "text": "Go read the stance on nuclear energy."},
        ]}]}

    adjusted, report = adjust_auto_captions(source, source_language="en", llm_call=llm_call)

    assert report["windows_adjusted"] == 1
    assert report["windows_fallback"] == 0
    assert [item.content for item in adjusted] == [
        'Said, "Fine, you abolished the law, but we will continue the policy anyway,"',
        "Answer me.",
        "Go read the stance on nuclear energy.",
    ]
    assert all(item.start < item.end for item in adjusted)
    assert all(adjusted[i].end <= adjusted[i + 1].start for i in range(len(adjusted) - 1))
