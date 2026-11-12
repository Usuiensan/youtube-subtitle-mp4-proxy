from app import main
import asyncio
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


def test_automatic_captions_are_translation_sources_but_not_passthrough() -> None:
    info = {
        "language": "en",
        "automatic_captions": {"en": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]},
    }

    selection = main.select_subtitle_language(info, "ja")

    assert selection["source_language"] == "en"
    assert selection["source_kind"] == "automatic"
    assert selection["translated"] is True

    with pytest.raises(main.HTTPException) as error:
        main.select_subtitle_language({"automatic_captions": {"ja": [{}]}}, "ja")
    assert error.value.status_code == 422


def test_translation_api_503_is_retryable_but_other_errors_are_not() -> None:
    assert main.is_retryable_translation_api_503(RuntimeError("translation api http error 503: unavailable"))
    assert not main.is_retryable_translation_api_503(RuntimeError("translation api http error 429: quota"))


def test_subtitle_candidates_include_automatic_captions_with_manual_priority() -> None:
    info = {
        "subtitles": {"en": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]},
        "automatic_captions": {"ja": [{"ext": "vtt"}], "de": [{"ext": "vtt"}]},
    }

    candidates = main.subtitle_candidates(info, "ja")
    assert {item["language"]: item["source_kind"] for item in candidates} == {
        "en": "manual",
        "ja": "manual",
        "de": "automatic",
    }


def test_subtitle_candidates_classify_and_hide_dubbed_asr_tracks() -> None:
    info = {
        "language": "en-US",
        "subtitles": {"fr": [{"ext": "vtt"}]},
        "automatic_captions": {
            "en-orig": [{"url": "https://example.test/en"}],
            "en": [{"url": "https://example.test/en?tlang=en"}],
            "ja": [{"url": "https://example.test/en?tlang=ja"}],
            "ja-orig": [{"url": "https://example.test/ja"}],
            "ko": [{"url": "https://example.test/en?tlang=ko"}],
            "de": [{"url": "https://example.test/de"}],
        },
    }

    candidates = main.subtitle_candidates(info, "ja")

    assert [(item["language"], item["track_kind"]) for item in candidates] == [
        ("fr", "manual"),
        ("en-orig", "asr_original"),
        ("de", "asr_original"),
        ("en", "youtube_mt"),
        ("ja", "youtube_mt"),
        ("ko", "youtube_mt"),
    ]


def test_subtitle_candidates_treat_single_automatic_track_as_original_asr() -> None:
    candidates = main.subtitle_candidates(
        {
            "language": "en-US",
            "automatic_captions": {"fr": [{"url": "https://example.test/fr"}]},
        },
        "ja",
    )

    assert candidates[0]["track_kind"] == "asr_original"


def test_explicit_automatic_translation_source_is_selected() -> None:
    selection = main.select_subtitle_language(
        {"automatic_captions": {"de": [{"ext": "vtt"}]}},
        "ja",
        source_lang="de",
    )

    assert selection["source_language"] == "de"
    assert selection["source_kind"] == "automatic"


@pytest.mark.parametrize(
    ("source_kind", "expected_flag"),
    [("automatic", "--write-auto-subs"), ("manual", "--write-subs")],
)
def test_download_subtitle_only_uses_source_specific_flag(
    tmp_path, monkeypatch, source_kind: str, expected_flag: str
) -> None:
    calls: list[list[str]] = []

    async def fake_run_command(args, cwd=None, raise_http=True):
        calls.append(args)
        (cwd / "video.en.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nsource\n", encoding="utf-8"
        )
        return ""

    async def fake_translate_subtitle_if_needed(**kwargs):
        return kwargs["subtitle"], kwargs["selection"]

    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "translate_subtitle_if_needed", fake_translate_subtitle_if_needed)
    info = {"subtitles": {"en": [{}]}} if source_kind == "manual" else {
        "automatic_captions": {"en": [{}]}
    }

    asyncio.run(main.download_subtitle_only("video", "ja", tmp_path, info))

    assert expected_flag in calls[0]


def test_variant_japanese_track_is_recognized_as_requested_language(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "translation_enabled", True)
    body = main.subtitle_choice_body(
        {
            "id": "video",
            "subtitles": {"ja-p4xb9ptA1GQ": [{"ext": "vtt"}]},
        },
        "ja",
    )

    assert body["requires_choice"] is True
    assert "error" not in body

    monkeypatch.setattr(main.settings, "translation_enabled", False)
    body = main.subtitle_choice_body(
        {
            "id": "video",
            "subtitles": {"ja-p4xb9ptA1GQ": [{"ext": "vtt"}]},
        },
        "ja",
    )

    assert body["requires_choice"] is False
    assert "error" not in body


def test_subtitle_language_prefers_stable_dtvcc_track_over_cc_track() -> None:
    info = {
        "subtitles": {
            "en-uYU-mmqFLq8": [{"name": "English - CC1", "ext": "json3"}],
            "en-JkeT_87f4cc": [{"name": "English - DTVCC1", "ext": "json3"}],
        }
    }

    selection = main.select_subtitle_language(info, "ja")

    assert selection["source_language"] == "en-JkeT_87f4cc"


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


def test_dual_subtitle_uses_detected_japanese_font(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "translated.srt"
    value = srt.compose([srt.Subtitle(1, timedelta(0), timedelta(seconds=1), "終わり")])
    source.write_text(value, encoding="utf-8")
    translated.write_text(value, encoding="utf-8")
    monkeypatch.setattr(main.settings, "subtitle_font", "Noto Sans CJK SC")
    monkeypatch.setattr(main, "find_japanese_font_spec", lambda: ("/fonts/NotoSansJP-Regular.otf", "Noto Sans JP"))

    main.ffmpeg_dual_subtitle_args(source, translated)

    ass = source.with_suffix(".dual.ass").read_text(encoding="utf-8")
    style = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
    assert ",Noto Sans JP," in style
    assert ",Noto Sans CJK SC," not in style


def test_japanese_gothic_file_uses_its_actual_family_name(monkeypatch) -> None:
    japanese_font = "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"
    monkeypatch.setattr(main.os.path, "exists", lambda path: path == japanese_font)

    assert main.find_japanese_font_spec() == (japanese_font, "IPAGothic")


def test_noto_sans_regular_is_preferred_when_available(monkeypatch) -> None:
    noto_font = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
    monkeypatch.setattr(main.os.path, "exists", lambda path: path == noto_font)

    assert main.find_japanese_font_spec() == (noto_font, "Noto Sans")


def test_noto_sans_cjk_jp_is_always_preferred(monkeypatch) -> None:
    monkeypatch.setattr(main.os.path, "exists", lambda path: True)

    assert main.find_japanese_font_spec() == (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "Noto Sans CJK JP",
    )


def test_render_profile_changes_when_detected_font_changes(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "subtitle_font", "Noto Sans CJK SC")
    monkeypatch.setattr(main, "find_japanese_font_spec", lambda: ("/fonts/NotoSansJP-Regular.otf", "Noto Sans JP"))
    japanese_profile = main.render_profile_id()
    monkeypatch.setattr(main, "find_japanese_font_spec", lambda: (None, None))

    assert japanese_profile != main.render_profile_id()


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


def test_dual_subtitle_size_matches_single_subtitle_scale(tmp_path) -> None:
    source = tmp_path / "source.srt"
    translated = tmp_path / "translated.srt"
    value = srt.compose([srt.Subtitle(1, timedelta(0), timedelta(seconds=1), "line")])
    source.write_text(value, encoding="utf-8")
    translated.write_text(value, encoding="utf-8")

    # 単言語字幕は PlayRes 288 基準で描画されるため、二言語字幕（PlayResY 720）
    # では設定値を 720/288 倍して初めて見た目の大きさが一致する。
    args = main.ffmpeg_dual_subtitle_args(source, translated, subtitle_font_size=16)
    ass = source.with_suffix(".dual.ass").read_text(encoding="utf-8")

    assert args[1].startswith(r"scale=-2:min(ih\,")
    assert ",40," in next(line for line in ass.splitlines() if line.startswith("Style: Default,"))

    main.ffmpeg_dual_subtitle_args(source, translated, subtitle_font_size=25)
    ass = source.with_suffix(".dual.ass").read_text(encoding="utf-8")

    assert ",63," in next(line for line in ass.splitlines() if line.startswith("Style: Default,"))


def test_dual_subtitle_ass_aligns_ranged_translation_to_source_cues(tmp_path) -> None:
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

    assert len(dialogues) == 4
    assert sum("First source" in line for line in dialogues) == 1
    assert sum("Second source" in line for line in dialogues) == 1
    # 範囲訳文は原文キューのタイミングへ分割され、連結すると元の訳文に戻る
    translated_events = sorted(
        (line for line in dialogues if "source" not in line),
        key=lambda line: line.split(",")[1],
    )
    assert len(translated_events) == 2
    assert ",0:00:01.00,0:00:02.00," in translated_events[0]
    assert ",0:00:02.00,0:00:03.00," in translated_events[1]
    joined = "".join(line.split(",", 9)[9] for line in translated_events)
    assert joined == "まとめた訳"


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


def test_translation_overlay_omits_incomplete_fallback_label() -> None:
    assert main.get_subtitle_overlay_label({}) == ""
    assert main.subtitle_overlay_drawtext_filters({}, None, "Noto Sans JP") == []


def test_translation_overlay_uses_detected_japanese_font_file(monkeypatch, tmp_path) -> None:
    font_file = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    monkeypatch.setattr(main, "find_japanese_font_spec", lambda: (font_file, "Noto Sans CJK JP"))
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")

    args = main.ffmpeg_subtitle_arg(
        subtitle,
        {"requested_language": "ja"},
    )

    assert f":fontfile='{font_file}'" in args
    assert "text='[字幕]日本語'" in args


def test_translation_overlay_contains_languages_and_model() -> None:
    label = main.get_subtitle_overlay_label({
        "source_language": "es",
        "requested_language": "ja",
        "translated": True,
        "translation_model": "gemini-3.1-flash-lite",
    })
    assert label.splitlines() == [
        "[字幕]スペイン語 → 日本語",
        "[subs]Spanish → Japanese",
        "[翻訳]gemini-3.1-flash-lite",
    ]


def test_wrap_text_to_width_prefers_punctuation_then_word_boundary() -> None:
    assert main.wrap_text_to_width("一二三、四五六、七八九", 7) == ["一二三、", "四五六、七八九"]
    assert main.wrap_text_to_width("one two three four", 4) == ["one two", "three", "four"]
    # 句読点も空白も無い場合は収まる位置で強制改行する（旧仕様は行全体を
    # 出して字幕が画面からはみ出していたため変更した）
    assert main.wrap_text_to_width("句読点のない長い日本語", 4) == ["句読点の", "ない長い", "日本語"]


def test_subtitle_candidates_prioritize_languages_without_dropping_tracks() -> None:
    remaining = "fr de es it pt ru ar hi th vi id ms nl pl uk cs da fi el he hu no ro sk sv tr bg ca et fa hr lt lv sl ta te ur".split()
    languages = remaining[:12] + ["ko", "en-US"] + remaining[12:] + [
        "zh-Hant", "ja", "en", "zh-Hans", "ja-JP", "zh", "zh-CN", "zh-TW",
    ]
    info = {"language": "zh-Hant", "subtitles": {lang: [{"ext": "vtt"}] for lang in languages}}

    candidates = main.subtitle_candidates(info, "ja")
    ordered = [candidate["language"] for candidate in candidates]

    assert languages.index("ja") > 25
    assert "ja" in ordered[:25]
    assert ordered[0] == "zh-Hant"
    assert len(ordered) == len(languages)
    assert set(ordered) == set(languages)
    assert [lang for lang in ordered if lang in remaining] == remaining
    assert [lang for lang in ordered if main.normalize_lang(lang) == "en"] == ["en-US", "en"]
    assert [lang for lang in ordered if main.normalize_lang(lang) == "ja"] == ["ja", "ja-JP"]
