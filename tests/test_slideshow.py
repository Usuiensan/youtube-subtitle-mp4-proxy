import asyncio
from pathlib import Path

import pytest

from app.slideshow import (
    SlideshowLimits,
    convert_slideshow,
    ffmpeg_slideshow_args,
    image_paths,
    natural_sort_key,
    slideshow_filter,
)


def test_natural_sort() -> None:
    paths = [Path("10.png"), Path("2.png"), Path("1.png")]
    assert [path.name for path in sorted(paths, key=natural_sort_key)] == ["1.png", "2.png", "10.png"]


def test_supported_and_unsupported_extensions() -> None:
    assert [path.name for path in image_paths([Path("a.webp"), Path("b.JPG")])] == ["a.webp", "b.JPG"]
    with pytest.raises(ValueError, match="Only PNG"):
        image_paths([Path("a.gif")])


def test_ffmpeg_args_and_aspect_ratio_filter() -> None:
    args = ffmpeg_slideshow_args(Path("slides.txt"), Path("output.mp4"), ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    assert "-an" in args
    assert "-movflags" in args and "+faststart" in args
    assert slideshow_filter() == "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black"


def test_pdf_pages_are_sorted_and_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"pdf")
    commands: list[list[str]] = []

    async def fake_command(args: list[str], **kwargs: object) -> str:
        commands.append(args)
        output_prefix = Path(args[-1])
        output_prefix.parent.mkdir(exist_ok=True)
        (output_prefix.parent / "page-10.png").write_bytes(b"10")
        (output_prefix.parent / "page-2.png").write_bytes(b"2")
        return ""

    monkeypatch.setattr("app.slideshow.run_command", fake_command)
    encoded: list[list[str]] = []
    concat_contents: list[str] = []

    async def fake_ffmpeg(args: list[str]) -> None:
        encoded.append(args)
        concat_contents.append(Path(args[args.index("-i") + 1]).read_text(encoding="utf-8"))

    work = tmp_path / "work"
    result = asyncio.run(
        convert_slideshow(
            pdf,
            work / "output.mp4",
            work_dir=work,
            video_args_factory=lambda: ["-c:v", "libx264"],
            ffmpeg_runner=fake_ffmpeg,
            limits=SlideshowLimits(max_pdf_pages=2),
        )
    )
    assert result == (work / "output.mp4").resolve()
    assert commands[0][0:4] == ["pdftoppm", "-png", "-r", "150"]
    assert "slide-000000.png" in concat_contents[0]
    assert concat_contents[0].find("slide-000000.png") < concat_contents[0].find("slide-000001.png")
