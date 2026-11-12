from app.main import ass_text_width, wrap_text_to_width

# 二言語字幕の既定相当（設定25 → ASS 63、可用幅キャップ後 ≈19.6）
WIDTH = 19.6


def test_long_japanese_without_punctuation_is_hard_broken():
    # 幅内に句読点が無い長い文でも、全行が幅に収まること（はみ出し回帰）
    text = "彼らがかつてボールベアリングを製造していた時代に由来しており、それも転がるものです。"
    lines = wrap_text_to_width(text, WIDTH)
    assert len(lines) >= 2
    assert all(ass_text_width(line) <= WIDTH + 1.0 for line in lines)
    assert "".join(lines) == text


def test_prohibited_char_does_not_start_a_line():
    text = "あ" * 19 + "、続きの文章です、これはテストです。"
    lines = wrap_text_to_width(text, WIDTH)
    prohibited = "、。，．・：；！？）］｝」』】〉》ー"
    assert all(not line.startswith(tuple(prohibited)) for line in lines[1:])


def test_opening_bracket_does_not_end_a_line():
    text = "彼はその時（本当に）驚いた、という話をずっと前から聞かされていたのです。"
    lines = wrap_text_to_width(text, WIDTH)
    assert all(not line.endswith(("（", "「", "『", "【")) for line in lines)


def test_long_english_word_is_hard_broken():
    text = "pneumonoultramicroscopicsilicovolcanoconiosis is a long word"
    lines = wrap_text_to_width(text, 10.0)
    assert len(lines) >= 2
    assert all(ass_text_width(line) <= 10.0 + 1.0 for line in lines)


def test_punctuation_break_is_preferred():
    text = "短い文です。次の文も短いです。さらに続きます、まだまだ続きます。"
    lines = wrap_text_to_width(text, WIDTH)
    assert len(lines) >= 2
    assert all(ass_text_width(line) <= WIDTH + 1.0 for line in lines)


def test_short_text_is_untouched():
    assert wrap_text_to_width("短い字幕です。", WIDTH) == ["短い字幕です。"]
    assert wrap_text_to_width("", WIDTH) == [""]
