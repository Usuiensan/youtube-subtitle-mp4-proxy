import asyncio

from app import main as app_main


def test_nvenc_open_error_uses_cpu_fallback() -> None:
    message = "[h264_nvenc] Error while opening encoder - maybe incorrect parameters"

    assert app_main.is_nvenc_driver_error(message)


def test_unrelated_ffmpeg_error_does_not_use_nvenc_fallback() -> None:
    message = "subtitles filter failed: Unable to find a suitable output format"

    assert not app_main.is_nvenc_driver_error(message)


def test_slideshow_ffmpeg_runner_uses_global_encode_lock(monkeypatch) -> None:
    events: list[str] = []

    class Lock:
        async def __aenter__(self):
            events.append("acquire")

        async def __aexit__(self, *_args):
            events.append("release")

    async def fake_ffmpeg(_args: list[str]) -> None:
        events.append("encode")

    monkeypatch.setattr(app_main, "_global_encode_lock", Lock())
    monkeypatch.setattr(app_main, "run_ffmpeg_with_optional_nvenc_fallback", fake_ffmpeg)
    asyncio.run(app_main.run_slideshow_ffmpeg(["ffmpeg"]))
    assert events == ["acquire", "encode", "release"]


def test_ffmpeg_progress_handles_long_stderr_without_newline(monkeypatch) -> None:
    class Process:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_data(b"x" * 70000)
            self.stderr.feed_eof()
            self.returncode = 0

        async def wait(self) -> int:
            return 0

    async def create_process(*_args, **_kwargs) -> Process:
        return Process()

    monkeypatch.setattr(app_main.asyncio, "create_subprocess_exec", create_process)

    asyncio.run(app_main.run_ffmpeg_with_progress(["ffmpeg"], "test", 1))
