from app import main


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
