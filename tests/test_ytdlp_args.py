from app.ytdlp_args import args_without_cookies, download_format_selector, fallback_format_selector


def test_download_format_selectors_preserve_quality_fallbacks() -> None:
    assert download_format_selector(720) == "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
    assert fallback_format_selector() == "bestvideo*+bestaudio/best"


def test_args_without_cookies_removes_flag_and_its_value() -> None:
    args = ["yt-dlp", "--cookies", "cookies.txt", "--format", "best", "--cookies-from-browser"]
    assert args_without_cookies(args) == ["yt-dlp", "--format", "best", "--cookies-from-browser"]
