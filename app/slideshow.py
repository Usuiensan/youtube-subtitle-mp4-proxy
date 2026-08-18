"""Create a padded, silent MP4 slideshow from a PDF or images."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.command_runner import run_command

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_SLIDE_SECONDS = 10.0
SLIDESHOW_WIDTH = 1920
SLIDESHOW_HEIGHT = 1080


def detect_upload_format(header: bytes) -> str | None:
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return None


@dataclass(frozen=True)
class SlideshowLimits:
    max_slides: int = 500
    max_pdf_pages: int = 500
    max_input_bytes: int = 512 * 1024 * 1024
    max_total_duration_seconds: float = float("inf")


def validate_total_duration(
    slide_seconds: float, slide_count: int, max_total_seconds: float
) -> None:
    total_seconds = slide_seconds * slide_count
    if total_seconds > max_total_seconds + 1e-9:
        raise ValueError(
            f"Total slideshow duration exceeds the maximum ({total_seconds:g} > {max_total_seconds:g} seconds)"
        )


def estimate_workspace_bytes(
    input_bytes: int, slide_count: int, duration_seconds: float
) -> int:
    # ponytail: fixed conservative estimate; use measured encoder output only if this becomes a storage bottleneck.
    temporary_png_bytes = slide_count * SLIDESHOW_WIDTH * SLIDESHOW_HEIGHT * 4
    output_mp4_bytes = max(64 * 1024 * 1024, int(duration_seconds * 2 * 1024 * 1024))
    return input_bytes + temporary_png_bytes + output_mp4_bytes + 64 * 1024 * 1024


async def pdf_page_count(pdf_path: Path) -> int | None:
    try:
        output = await run_command(
            ["pdfinfo", str(pdf_path)], cwd=pdf_path.parent, timeout_seconds=30
        )
    except Exception:
        return None
    match = re.search(r"^Pages:\s*(\d+)\s*$", output, re.MULTILINE)
    return int(match.group(1)) if match else None


def natural_sort_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def image_paths(paths: Sequence[Path], *, limits: SlideshowLimits = SlideshowLimits()) -> list[Path]:
    if not paths:
        raise ValueError("At least one image is required")
    result = sorted(paths, key=natural_sort_key)
    if len(result) > limits.max_slides:
        raise ValueError(f"Too many slides; maximum is {limits.max_slides}")
    if any(path.suffix.casefold() not in SUPPORTED_IMAGE_EXTENSIONS for path in result):
        raise ValueError("Only PNG, JPEG, and WebP images are supported")
    return result


def slideshow_filter(width: int = 1920, height: int = 1080) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def ffmpeg_slideshow_args(
    concat_file: Path,
    output_path: Path,
    video_args: Sequence[str],
    duration_seconds: float | None = None,
) -> list[str]:
    args = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-vf",
        slideshow_filter(),
        "-r",
        "30",
        "-an",
        *video_args,
        "-movflags",
        "+faststart",
    ]
    if duration_seconds is not None:
        args.extend(["-frames:v", str(max(1, round(duration_seconds * 30)))])
    args.extend(["-f", "mp4", str(output_path)])
    return args


def _concat_line(path: Path, seconds: float | None = None) -> str:
    escaped = path.as_posix().replace("'", "'\\''")
    return f"file '{escaped}'\n" + (f"duration {seconds:g}\n" if seconds is not None else "")


async def convert_slideshow(
    source: Path | Sequence[Path],
    output_path: Path,
    *,
    work_dir: Path,
    slide_seconds: float = DEFAULT_SLIDE_SECONDS,
    limits: SlideshowLimits = SlideshowLimits(),
    video_args_factory: Callable[[], Sequence[str]] | None = None,
    ffmpeg_runner: Callable[[list[str]], Awaitable[None]] | None = None,
) -> Path:
    """Convert a PDF or image sequence; source and output must be in work_dir."""
    if slide_seconds <= 0:
        raise ValueError("slide_seconds must be positive")
    work_dir = work_dir.resolve()
    output_path = output_path.resolve()
    if work_dir not in output_path.parents:
        raise ValueError("output_path must be inside work_dir")
    work_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=work_dir) as temp_name:
        temp_dir = Path(temp_name)
        if isinstance(source, Path):
            if source.suffix.casefold() == ".pdf":
                if source.stat().st_size > limits.max_input_bytes:
                    raise ValueError("Input file is too large")
                pdf = temp_dir / "input.pdf"
                shutil.copyfile(source, pdf)
                await run_command(
                    [
                        "pdftoppm",
                        "-png",
                        "-r",
                        "150",
                        "-f",
                        "1",
                        "-l",
                        str(limits.max_pdf_pages + 1),
                        str(pdf),
                        str(temp_dir / "page"),
                    ],
                    cwd=temp_dir,
                    timeout_seconds=600,
                )
                slides = sorted(temp_dir.glob("page-*.png"), key=natural_sort_key)
                if not slides or len(slides) > limits.max_pdf_pages:
                    raise ValueError(f"PDF page count must be between 1 and {limits.max_pdf_pages}")
            else:
                slides = image_paths([source], limits=limits)
        else:
            slides = image_paths(source, limits=limits)

        validate_total_duration(
            slide_seconds, len(slides), limits.max_total_duration_seconds
        )

        copied: list[Path] = []
        total_size = 0
        for index, slide in enumerate(slides):
            size = slide.stat().st_size
            total_size += size
            if total_size > limits.max_input_bytes:
                raise ValueError("Input files are too large")
            target = temp_dir / f"slide-{index:06d}.png"
            if slide.suffix.casefold() == ".png":
                shutil.copyfile(slide, target)
            else:
                await run_command(
                    ["ffmpeg", "-y", "-i", str(slide), "-frames:v", "1", str(target)],
                    cwd=temp_dir,
                    timeout_seconds=120,
                )
            copied.append(target)

        concat_file = temp_dir / "slides.txt"
        concat_file.write_text(
            "".join(_concat_line(path, slide_seconds) for path in copied[:-1])
            + _concat_line(copied[-1], slide_seconds)
            + _concat_line(copied[-1]),
            encoding="utf-8",
        )
        if video_args_factory is None:
            from app.main import ffmpeg_video_args

            video_args_factory = ffmpeg_video_args
        args = ffmpeg_slideshow_args(
            concat_file,
            output_path,
            video_args_factory(),
            duration_seconds=slide_seconds * len(copied),
        )
        if ffmpeg_runner is None:
            from app.main import run_ffmpeg_with_optional_nvenc_fallback

            ffmpeg_runner = run_ffmpeg_with_optional_nvenc_fallback
        await ffmpeg_runner(args)
    return output_path
