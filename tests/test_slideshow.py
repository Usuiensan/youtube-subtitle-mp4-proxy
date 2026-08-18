import asyncio
from pathlib import Path

import pytest

from app.slideshow import (
    SlideshowLimits,
    convert_slideshow,
    estimate_workspace_bytes,
    ffmpeg_slideshow_args,
    image_paths,
    natural_sort_key,
    slideshow_filter,
    validate_total_duration,
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

    timed_args = ffmpeg_slideshow_args(Path("slides.txt"), Path("output.mp4"), [], duration_seconds=4)
    assert timed_args[timed_args.index("-frames:v") + 1] == "120"


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


def test_pdf_page_limit_rejects_extra_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"pdf")

    async def fake_command(args: list[str], **kwargs: object) -> str:
        output_prefix = Path(args[-1])
        for index in range(1, 4):
            (output_prefix.parent / f"page-{index}.png").write_bytes(b"page")
        return ""

    monkeypatch.setattr("app.slideshow.run_command", fake_command)
    with pytest.raises(ValueError, match="page count"):
        asyncio.run(
            convert_slideshow(
                pdf,
                tmp_path / "work" / "output.mp4",
                work_dir=tmp_path / "work",
                limits=SlideshowLimits(max_pdf_pages=2),
            )
        )


def test_mixed_images_are_normalized_before_concat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = []
    for name in ("10.png", "2.jpg", "1.webp"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        sources.append(path)
    commands: list[list[str]] = []

    async def fake_command(args: list[str], **kwargs: object) -> str:
        commands.append(args)
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"png")
        return ""

    monkeypatch.setattr("app.slideshow.run_command", fake_command)
    concat_contents: list[str] = []

    async def fake_ffmpeg(args: list[str]) -> None:
        concat_contents.append(Path(args[args.index("-i") + 1]).read_text(encoding="utf-8"))

    work = tmp_path / "work"
    asyncio.run(
        convert_slideshow(
            sources,
            work / "output.mp4",
            work_dir=work,
            video_args_factory=lambda: ["-c:v", "libx264"],
            ffmpeg_runner=fake_ffmpeg,
        )
    )
    assert len(commands) == 2
    assert all(path.endswith(".png") for path in (command[-1] for command in commands))
    assert concat_contents[0].count(".png") == 4


def test_total_duration_limit_rejects_images_but_allows_exact_limit(tmp_path: Path) -> None:
    sources = [tmp_path / "1.png", tmp_path / "2.png"]
    for path in sources:
        path.write_bytes(b"png")

    with pytest.raises(ValueError, match="Total slideshow duration"):
        asyncio.run(
            convert_slideshow(
                sources,
                tmp_path / "work-over" / "over.mp4",
                work_dir=tmp_path / "work-over",
                slide_seconds=2,
                limits=SlideshowLimits(max_total_duration_seconds=3),
                ffmpeg_runner=lambda _args: _noop(),
            )
        )

    asyncio.run(
        convert_slideshow(
            sources,
            tmp_path / "work-exact" / "exact.mp4",
            work_dir=tmp_path / "work-exact",
            slide_seconds=2,
            limits=SlideshowLimits(max_total_duration_seconds=4),
            ffmpeg_runner=lambda _args: _noop(),
        )
    )


def test_total_duration_limit_rejects_pdf_pages_but_allows_exact_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"pdf")

    async def fake_command(args: list[str], **kwargs: object) -> str:
        output_prefix = Path(args[-1])
        for index in range(1, 3):
            (output_prefix.parent / f"page-{index}.png").write_bytes(b"page")
        return ""

    monkeypatch.setattr("app.slideshow.run_command", fake_command)
    for maximum, output in ((3, "over.mp4"), (4, "exact.mp4")):
        operation = convert_slideshow(
            pdf,
            tmp_path / output.removesuffix(".mp4") / output,
            work_dir=tmp_path / output.removesuffix(".mp4"),
            slide_seconds=2,
            limits=SlideshowLimits(max_total_duration_seconds=maximum),
            ffmpeg_runner=lambda _args: _noop(),
        )
        if maximum == 3:
            with pytest.raises(ValueError, match="Total slideshow duration"):
                asyncio.run(operation)
        else:
            asyncio.run(operation)


async def _noop() -> None:
    return None


def test_slideshow_workspace_estimate_includes_input_png_and_mp4_space() -> None:
    estimate = estimate_workspace_bytes(100, 2, 10)
    assert estimate > 100 + 2 * 1920 * 1080 * 4
    assert estimate_workspace_bytes(100, 10, 10) > estimate_workspace_bytes(100, 1, 10)
    validate_total_duration(2, 2, 4)
