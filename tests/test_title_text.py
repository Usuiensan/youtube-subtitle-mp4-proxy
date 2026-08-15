from bot.main import title_text


def test_title_text_shows_only_priority_languages_and_counts_the_rest() -> None:
    result = title_text(
        "Default title",
        [
            {"language": "default", "title": "Default title"},
            {"language": "ja", "title": "日本語タイトル"},
            {"language": "en", "title": "English title"},
            {"language": "ko", "title": "한국어 제목"},
            {"language": "zh-Hans", "title": "中文标题"},
            {"language": "fr", "title": "Titre français"},
            {"language": "de", "title": "Deutscher Titel"},
        ],
    )

    assert result == (
        "動画タイトル :\n"
        "[既定] Default title\n"
        "[日本語] 日本語タイトル\n"
        "[英語] English title\n"
        "[韓国語] 한국어 제목\n"
        "[中国語] 中文标题\n"
        "ほか2言語"
    )
