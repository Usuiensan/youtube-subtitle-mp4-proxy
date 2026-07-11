from bot.prepare_input import (
    extract_video_id,
    is_ambiguous_prepare_input,
    is_manual_video_list,
    looks_like_playlist_or_channel,
)


def test_extract_video_id_accepts_common_formats() -> None:
    assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_playlist_and_ambiguous_detection() -> None:
    assert looks_like_playlist_or_channel("https://www.youtube.com/playlist?list=PL123")
    assert looks_like_playlist_or_channel("@GoogleDevelopers")
    assert looks_like_playlist_or_channel("https://www.youtube.com/@GoogleDevelopers")
    assert is_manual_video_list("dQw4w9WgXcQ eY52Zsg-KVI")
    assert is_ambiguous_prepare_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123")
    assert not is_ambiguous_prepare_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
