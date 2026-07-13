"""Error types and classifiers for external media commands."""

from __future__ import annotations


class CommandError(Exception):
    def __init__(self, args: list[str], message: str) -> None:
        super().__init__(message)
        self.args_list = args
        self.message = message
        self.stderr = message


def is_youtube_video_unavailable_error(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "confirm you're not a bot",
            "content isn't available",
            "content is not available",
            "cookies",
            "country",
            "geo-restricted",
            "not available in your country",
            "please sign in",
            "po token",
            "sign in to confirm your age",
            "sign in to confirm you’re not a bot",
            "this video may be inappropriate",
            "this video is not available",
            "video unavailable",
            "private video",
            "this video is private",
            "has been removed",
            "video has been removed",
            "this video has been removed",
        )
    )


def is_ytdlp_requested_format_unavailable_error(message: str) -> bool:
    return "requested format is not available" in message.lower()


def is_ytdlp_rate_limited_error(message: str) -> bool:
    text = message.lower()
    return "429" in text or "too many requests" in text
