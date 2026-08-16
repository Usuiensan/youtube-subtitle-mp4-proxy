from app import main
import srt
import pytest
from datetime import timedelta


def test_explicit_translation_source_wins_over_target_auto_caption() -> None:
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {
            "en": [{"ext": "vtt"}],
            "en-orig": [{"ext": "vtt"}],
            "ja": [{"ext": "vtt"}],
        },
    }

    selection = main.select_subtitle_language(
        info,
        "ja",
        source_lang="en",
    )

    assert selection["source_language"] == "en"
    assert selection["source_kind"] == "manual"
    assert selection["translated"] is True
    assert selection["translation_engine"] == main.configured_translation_engine()


def test_automatic_captions_are_always_excluded() -> None:
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]},
    }

    selection = main.select_subtitle_language(info, "ja")

    assert selection["source_language"] == "en"
    assert selection["source_kind"] == "manual"

    with pytest.raises(main.HTTPException) as error:
        main.select_subtitle_language({"automatic_captions": {"en": [{}]}}, "ja")
    assert error.value.status_code == 422


def test_translation_api_503_is_retryable_but_other_errors_are_not() -> None:
    assert main.is_retryable_translation_api_503(RuntimeError("translation api http error 503: unavailable"))
    assert not main.is_retryable_translation_api_503(RuntimeError("translation api http error 429: quota"))


def test_subtitle_candidates_ignore_automatic_captions() -> None:
    info = {
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {"ja": [{"ext": "vtt"}]},
    }

    assert [item["language"] for item in main.subtitle_candidates(info, "ja")] == ["en"]


def test_variant_japanese_track_is_recognized_as_requested_language(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "translation_enabled", True)
    body = main.subtitle_choice_body(
        {
            "id": "video",
            "subtitles": {"ja-p4xb9ptA1GQ": [{"ext": "vtt"}]},
        },
        "ja",
    )

    assert body["requires_choice"] is False
    assert "error" not in body


def test_ass_builder_preserves_srt_line_breaks(tmp_path) -> None:
    source = tmp_path / "subtitle.srt"
    output = tmp_path / "subtitle.ass"
    source.write_text(
        srt.compose([
            srt.Subtitle(
                index=1,
                start=timedelta(seconds=1),
                end=timedelta(seconds=3),
                content="English line\n　\n日本語の行",
            )
        ]),
        encoding="utf-8",
    )

    main.build_ass_from_srt(
        source,
        output,
        align=1,
        margin_l=20,
        margin_r=20,
        margin_v=20,
        font_size=32,
        keep_source_line_breaks=True,
    )

    assert r"English line\N　\N日本語の行" in output.read_text(encoding="utf-8")


def test_ass_builder_removes_literal_subtitle_escape_markers(tmp_path) -> None:
    source = tmp_path / "subtitle.srt"
    output = tmp_path / "subtitle.ass"
    source.write_text(
        srt.compose([
            srt.Subtitle(
                index=1,
                start=timedelta(seconds=1),
                end=timedelta(seconds=3),
                content=r"English line\n日本語\hの行",
            )
        ]),
        encoding="utf-8",
    )

    main.build_ass_from_srt(
        source,
        output,
        align=1,
        margin_l=20,
        margin_r=20,
        margin_v=20,
        font_size=32,
    )

    ass = output.read_text(encoding="utf-8")
    assert r"English line\n" not in ass
    assert r"日本語\h" not in ass
    assert "English line 日本語 の行" in ass


def test_dual_subtitle_ass_is_centered_and_does_not_duplicate_source(tmp_path) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "translated.srt"
    subtitle = srt.compose([
        srt.Subtitle(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=3),
            content="English line",
        )
    ])
    source.write_text(subtitle, encoding="utf-8")
    translated.write_text(
        subtitle.replace("English line", "English line\n日本語の行"),
        encoding="utf-8",
    )

    args = main.ffmpeg_dual_subtitle_args(source, translated)
    ass = source.with_suffix(".dual.ass").read_text(encoding="utf-8")

    assert args[1].count("ass=") == 1
    assert ",4," in next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 2
    assert dialogues[0].count("English line") == 1
    assert dialogues[1].endswith(",,日本語の行")
    assert r"\N　\N" not in ass
    assert int(dialogues[0].split(",")[7]) > int(dialogues[1].split(",")[7])


def test_dual_subtitle_ass_preserves_source_line_breaks(tmp_path) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "translated.srt"
    source.write_text(
        srt.compose([srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=3), "First line\nSecond line")]),
        encoding="utf-8",
    )
    translated.write_text(
        srt.compose([srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=3), "一行目\n二行目")]),
        encoding="utf-8",
    )

    main.ffmpeg_dual_subtitle_args(source, translated)
    ass = source.with_suffix(".dual.ass").read_text(encoding="utf-8")

    assert r"First line\NSecond line" in ass


def test_dual_subtitle_ass_keeps_source_cues_with_a_ranged_translation(tmp_path) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "translated.srt"
    source.write_text(
        srt.compose(
            [
                srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=2), "First source"),
                srt.Subtitle(2, timedelta(seconds=2), timedelta(seconds=3), "Second source"),
            ]
        ),
        encoding="utf-8",
    )
    translated.write_text(
        srt.compose([srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=3), "まとめた訳")]),
        encoding="utf-8",
    )

    main.ffmpeg_dual_subtitle_args(source, translated)
    ass = source.with_suffix(".dual.ass").read_text(encoding="utf-8")
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    assert len(dialogues) == 3
    assert sum("First source" in line for line in dialogues) == 1
    assert sum("Second source" in line for line in dialogues) == 1
    ranged = next(line for line in dialogues if "まとめた訳" in line)
    assert ",0:00:01.00,0:00:03.00," in ranged


def test_translation_overlay_uses_model_only() -> None:
    assert main.subtitle_translation_service_label(
        {
            "translation_engine": "gemini_2_5_flash_lite",
            "translation_model": "gemini-3.1-flash-lite",
        }
    ) == "[翻訳]gemini-3.1-flash-lite"


def test_translation_overlay_uses_actual_engine_after_fallback() -> None:
    assert main.subtitle_translation_service_label(
        {
            "translation_engine": "gemini_3_5_flash",
            "translation_model": "Gemini 3.5 Flash",
            "translation_fallback_used": True,
        }
    ) == "[翻訳]Gemini 3.5 Flash"


def test_wrap_text_to_width_prefers_punctuation_then_word_boundary() -> None:
    assert main.wrap_text_to_width("一二三、四五六、七八九", 7) == ["一二三、", "四五六、七八九"]
    assert main.wrap_text_to_width("one two three four", 4) == ["one two", "three", "four"]
    assert main.wrap_text_to_width("句読点のない長い日本語", 4) == ["句読点のない長い日本語"]
