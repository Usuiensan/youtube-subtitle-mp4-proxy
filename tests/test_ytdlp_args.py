from app.ytdlp_args import args_without_cookies, direct_mp4_format_selector, download_format_selector, fallback_format_selector


def test_download_format_selectors_preserve_quality_fallbacks() -> None:
    assert download_format_selector(720) == "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
    assert fallback_format_selector() == "bestvideo*+bestaudio/best"


def test_original_language_selector_keeps_all_same_language_audio_tracks() -> None:
    selector = download_format_selector(720, "de-DE")
    assert "mergeall[vcodec=none][language^=de]" in selector
    assert fallback_format_selector("de-DE").startswith("bestvideo*+mergeall[vcodec=none][language^=de]")


def test_args_without_cookies_removes_flag_and_its_value() -> None:
    args = ["yt-dlp", "--cookies", "cookies.txt", "--format", "best", "--cookies-from-browser"]
    assert args_without_cookies(args) == ["yt-dlp", "--format", "best", "--cookies-from-browser"]


def test_direct_mp4_selector_requires_vrchat_compatible_streams() -> None:
    assert direct_mp4_format_selector(720) == (
        "bv*[height<=720][vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a]/"
        "b[height<=720][vcodec^=avc1][acodec^=mp4a][ext=mp4]"
    )
