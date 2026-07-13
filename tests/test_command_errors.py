from app.command_errors import (
    CommandError,
    is_ytdlp_requested_format_unavailable_error,
    is_youtube_video_unavailable_error,
)


def test_command_error_preserves_command_and_stderr() -> None:
    error = CommandError(["yt-dlp", "--version"], "failed")
    assert error.args_list == ["yt-dlp", "--version"]
    assert error.message == "failed"
    assert error.stderr == "failed"


def test_ytdlp_error_classifiers_are_case_insensitive() -> None:
    assert is_youtube_video_unavailable_error("This video is not available")
    assert is_youtube_video_unavailable_error("Sign In To Confirm Your Age")
    assert not is_youtube_video_unavailable_error("network connection reset")
    assert is_ytdlp_requested_format_unavailable_error("Requested Format Is Not Available")
    assert not is_ytdlp_requested_format_unavailable_error("download completed")
