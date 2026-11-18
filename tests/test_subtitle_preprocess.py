from datetime import timedelta

import srt

from app.subtitle_preprocess import (
    is_word_incremental_subtitles,
    normalize_word_incremental_subtitles,
    preprocess_downloaded_subtitle,
)


def cue(index: int, start: float, end: float, text: str) -> srt.Subtitle:
    return srt.Subtitle(index, timedelta(seconds=start), timedelta(seconds=end), text)


def test_word_incremental_subtitles_drop_transient_updates_and_remove_overlap() -> None:
    subtitles = [
        cue(1, 0, 1, "one two"),
        cue(2, 1, 1.1, "one two"),
        cue(3, 1.1, 2, "one two three"),
        cue(4, 2, 2.1, "two three"),
        cue(5, 2.1, 3, "two three four"),
    ]

    assert is_word_incremental_subtitles(subtitles)
    normalized = normalize_word_incremental_subtitles(subtitles)

    assert normalized is not None
    assert [item.content for item in normalized] == ["one two", "three", "four"]
    assert [(item.start.total_seconds(), item.end.total_seconds()) for item in normalized] == [
        (0.0, 1.0),
        (1.1, 2.0),
        (2.1, 3.0),
    ]


def test_normal_subtitles_are_left_unchanged() -> None:
    subtitles = [
        cue(1, 0, 1, "One sentence."),
        cue(2, 1, 2, "Another sentence."),
        cue(3, 2, 3, "A third sentence."),
    ]

    assert not is_word_incremental_subtitles(subtitles)
    assert normalize_word_incremental_subtitles(subtitles) is None


def test_preprocess_keeps_raw_sidecar(tmp_path) -> None:
    path = tmp_path / "subtitle.en.srt"
    path.write_text(
        srt.compose([
            cue(1, 0, 1, "one two"),
            cue(2, 1, 1.1, "one two"),
            cue(3, 1.1, 2, "one two three"),
            cue(4, 2, 2.1, "two three"),
            cue(5, 2.1, 3, "two three four"),
        ]),
        encoding="utf-8",
    )

    assert preprocess_downloaded_subtitle(path) == path
    assert path.with_name("subtitle.en.raw.srt").exists()
    assert len(list(srt.parse(path.read_text(encoding="utf-8")))) == 3
    assert len(list(srt.parse(path.with_name("subtitle.en.raw.srt").read_text(encoding="utf-8")))) == 5


def test_detection_uses_half_second_transient_threshold() -> None:
    subtitles = [
        cue(1, 0, 1, "one two"),
        cue(2, 1, 1.3, "one two"),
        cue(3, 1.3, 2.3, "one two three"),
        cue(4, 2.3, 2.6, "two three"),
        cue(5, 2.6, 3.6, "two three four"),
    ]

    assert is_word_incremental_subtitles(subtitles)
    normalized = normalize_word_incremental_subtitles(subtitles)
    assert normalized is not None
    assert [item.content for item in normalized] == ["one two", "three", "four"]


def _mixed_subtitles() -> list[srt.Subtitle]:
    return [
        cue(1, 0, 1, "hello world"),
        cue(2, 1, 1.3, "hello world"),
        cue(3, 1.3, 2.3, "hello world again"),
        cue(4, 2.3, 3.3, "a completely different sentence here"),
        cue(5, 3.3, 3.6, "short"),
        cue(6, 3.6, 4.6, "final sentence"),
    ]


def test_force_normalizes_asr_even_when_detection_misses() -> None:
    subtitles = _mixed_subtitles()

    assert not is_word_incremental_subtitles(subtitles)
    assert normalize_word_incremental_subtitles(subtitles) is None

    forced = normalize_word_incremental_subtitles(subtitles, force=True)
    assert forced is not None
    # 途中経過の関連キューだけが落ち、無関係な短いキュー ("short") は残る
    assert [item.content for item in forced] == [
        "hello world",
        "again",
        "a completely different sentence here",
        "short",
        "final sentence",
    ]


def test_japanese_two_line_rolling_keeps_each_line_once() -> None:
    # YouTube の日本語ASRは2行ローリング形式: 各行が次のキューでもう一度出る。
    # 1行=1単語扱いになるため、単語単位の重なり除去では行の重複を取り除けない。
    subtitles = [
        cue(1, 16.4, 18.99, "おはよう直火焼きワッパーセットの頂点に"),
        cue(2, 18.99, 19.0, "おはよう直火焼きワッパーセットの頂点に\n "),
        cue(3, 19.0, 20.99, "おはよう直火焼きワッパーセットの頂点に\n君臨するビーフ焼き"),
        cue(4, 20.99, 21.0, "君臨するビーフ焼き\n "),
        cue(5, 21.0, 23.09, "君臨するビーフ焼き\nそれこそが我らの癒し"),
    ]

    normalized = normalize_word_incremental_subtitles(subtitles, force=True)

    assert normalized is not None
    assert [item.content for item in normalized] == [
        "おはよう直火焼きワッパーセットの頂点に",
        "君臨するビーフ焼き",
        "それこそが我らの癒し",
    ]


def test_chinese_single_line_growth_keeps_only_the_new_tail() -> None:
    subtitles = [
        cue(1, 0, 2, "今天是第三場電視"),
        cue(2, 2, 4, "今天是第三場電視發表會由中選會"),
        cue(3, 4, 6, "發表會由中選會主辦"),
    ]

    normalized = normalize_word_incremental_subtitles(subtitles, force=True)

    assert normalized is not None
    assert [item.content for item in normalized] == ["今天是第三場電視", "發表會由中選會", "主辦"]


def test_single_word_overlap_is_removed_when_the_word_is_long() -> None:
    subtitles = [
        cue(1, 0, 2, "a bell will ring twice."),
        cue(2, 2, 4, "twice. The representative must then conclude"),
    ]

    normalized = normalize_word_incremental_subtitles(subtitles, force=True)

    assert normalized is not None
    assert [item.content for item in normalized] == [
        "a bell will ring twice.",
        "The representative must then conclude",
    ]


def test_short_single_word_overlap_is_kept() -> None:
    subtitles = [
        cue(1, 0, 2, "this is the"),
        cue(2, 2, 4, "the house on the hill"),
    ]

    normalized = normalize_word_incremental_subtitles(subtitles, force=True)

    assert normalized is not None
    assert [item.content for item in normalized] == ["this is the", "the house on the hill"]


def test_preprocess_without_force_leaves_undetected_file_unchanged(tmp_path) -> None:
    path = tmp_path / "subtitle.ja.srt"
    path.write_text(srt.compose(_mixed_subtitles()), encoding="utf-8")

    assert preprocess_downloaded_subtitle(path) == path
    assert not path.with_name("subtitle.ja.raw.srt").exists()
    assert len(list(srt.parse(path.read_text(encoding="utf-8")))) == 6
