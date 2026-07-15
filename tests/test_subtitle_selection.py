from app import main
import srt
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
        translation_engine="google_cloud",
    )

    assert selection["source_language"] == "en"
    assert selection["source_kind"] == "manual"
    assert selection["translated"] is True
    assert selection["translation_engine_requested"] == "google_cloud"


def test_machine_translated_target_caption_is_not_treated_as_original() -> None:
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]},
    }

    selection = main.select_subtitle_language(
        info,
        "ja",
        translation_engine="google_cloud",
    )

    assert selection["source_language"] == "en"
    assert selection["translated"] is True


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
