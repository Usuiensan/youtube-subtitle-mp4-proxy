"""Pure yt-dlp argument construction helpers."""

from __future__ import annotations


def download_format_selector(max_height: int, original_language: str | None = None) -> str:
    base = (
        f"bv*[height<={max_height}]+ba/"
        f"b[height<={max_height}]/"
        "bv*+ba/"
        "b"
    )
    language = (original_language or "").strip().lower().split("-", 1)[0]
    if not language:
        return base
    original_audio = f"mergeall[vcodec=none][language^={language}]"
    return (
        f"bv*[height<={max_height}]+{original_audio}/"
        f"bv*[height<={max_height}]+ba[language^={language}]/"
        f"{base}"
    )


def fallback_format_selector(original_language: str | None = None) -> str:
    language = (original_language or "").strip().lower().split("-", 1)[0]
    if language:
        return f"bestvideo*+mergeall[vcodec=none][language^={language}]/bestvideo*+bestaudio/best"
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
