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
        translation_engine="gemini_2_5_flash_lite",
    )

    assert selection["source_language"] == "en"
    assert selection["source_kind"] == "manual"
    assert selection["translated"] is True
    assert selection["translation_engine_requested"] == "gemini_2_5_flash_lite"


def test_automatic_captions_are_always_excluded() -> None:
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]},
    }

    selection = main.select_subtitle_language(info, "ja", translation_engine="gemini_2_5_flash_lite")

    assert selection["source_language"] == "en"
    assert selection["source_kind"] == "manual"

    with pytest.raises(main.HTTPException) as error:
        main.select_subtitle_language({"automatic_captions": {"en": [{}]}}, "ja")
    assert error.value.status_code == 422


def test_google_cloud_translation_is_disabled() -> None:
    with pytest.raises(main.HTTPException) as error:
        main.select_subtitle_language(
            {"subtitles": {"en": [{"ext": "vtt"}]}},
            "ja",
            source_lang="en",
            translation_engine="google_cloud",
        )

    assert error.value.status_code == 422
    assert "disabled" in error.value.detail


def test_translation_engine_is_fixed_by_environment() -> None:
    configured = main.configured_translation_engine()
    assert main.enforce_configured_translation_engine(configured) == configured
    with pytest.raises(main.HTTPException) as error:
        main.enforce_configured_translation_engine("qwen3_8b" if configured != "qwen3_8b" else "gemma3_12b")
    assert error.value.status_code == 422
    assert "TRANSLATION_DEFAULT_PROFILE" in error.value.detail


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


def test_translation_overlay_uses_model_only() -> None:
    assert main.subtitle_translation_service_label(
        {
            "translation_engine": "gemini_2_5_flash_lite",
            "translation_model": "gemini-3.1-flash-lite",
        }
    ) == "[翻訳]gemini-3.1-flash-lite"
