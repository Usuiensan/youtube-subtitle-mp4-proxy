from app import main as app_main


def test_nvenc_open_error_uses_cpu_fallback() -> None:
    message = "[h264_nvenc] Error while opening encoder - maybe incorrect parameters"

    assert app_main.is_nvenc_driver_error(message)


def test_unrelated_ffmpeg_error_does_not_use_nvenc_fallback() -> None:
    message = "subtitles filter failed: Unable to find a suitable output format"

    assert not app_main.is_nvenc_driver_error(message)
