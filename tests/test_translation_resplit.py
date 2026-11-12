from datetime import timedelta

import srt

from app.translation import resplit_translation_onto_source


def cue(index: int, start: float, end: float, content: str) -> srt.Subtitle:
    return srt.Subtitle(
        index=index,
        start=timedelta(seconds=start),
        end=timedelta(seconds=end),
        content=content,
    )


def test_sentence_translation_is_split_onto_source_fragments():
    source = [
        cue(11, 0.0, 1.4, "Baidu is one of the biggest search"),
        cue(12, 1.4, 3.4, "engines in China, and as it's got a"),
        cue(13, 3.4, 5.2, "Chinese name, none of you probably know"),
        cue(14, 5.2, 6.5, "what it means."),
    ]
    translated = [
        cue(3, 0.0, 6.5, "百度は中国最大の検索エンジンの一つですが、中国語の名前なので、皆さんはその意味をご存じないでしょう。"),
    ]
    result = resplit_translation_onto_source(source, translated)
    assert [(item.start, item.end) for item in result] == [(item.start, item.end) for item in source]
    assert "".join(item.content for item in result) == translated[0].content
    assert all(item.content for item in result)


def test_aligned_translation_is_unchanged():
    source = [cue(1, 0.0, 1.0, "hello"), cue(2, 1.0, 2.0, "world")]
    translated = [cue(1, 0.0, 1.0, "こんにちは"), cue(2, 1.0, 2.0, "世界")]
    result = resplit_translation_onto_source(source, translated)
    assert result == translated


def test_unsplittable_translation_is_kept_as_is():
    source = [cue(1, 0.0, 1.0, "a"), cue(2, 1.0, 2.0, "b"), cue(3, 2.0, 3.0, "c")]
    translated = [cue(9, 0.0, 3.0, "短")]
    result = resplit_translation_onto_source(source, translated)
    assert result == translated


def test_empty_inputs():
    assert resplit_translation_onto_source([], [cue(1, 0.0, 1.0, "x")]) == [cue(1, 0.0, 1.0, "x")]
    assert resplit_translation_onto_source([cue(1, 0.0, 1.0, "x")], []) == []
