"""Pure yt-dlp argument construction helpers."""

from __future__ import annotations


def download_format_selector(max_height: int) -> str:
    return (
        f"bv*[height<={max_height}]+ba/"
        f"b[height<={max_height}]/"
        "bv*+ba/"
        "b"
    )


def fallback_format_selector() -> str:
    return "bestvideo*+bestaudio/best"


def args_without_cookies(args: list[str]) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--cookies":
            skip_next = True
            continue
        stripped.append(arg)
    return stripped
