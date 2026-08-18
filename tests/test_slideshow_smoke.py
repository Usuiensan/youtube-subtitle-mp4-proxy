import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.main import run_slideshow_ffmpeg
from app.slideshow import SlideshowLimits, convert_slideshow


def _write_pdf(path: Path) -> None:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 160 90] /Resources << /ProcSet [/PDF] >> /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 160 90] /Resources << /ProcSet [/PDF] >> /Contents 6 0 R >>",
    ]
    stream = b"0.2 0.4 0.8 rg\n0 0 160 90 re\nf\n"
    objects.extend(
        [
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        ]
    )
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(content)


@pytest.mark.skipif(
    not all(shutil.which(command) for command in ("ffmpeg", "pdftoppm")),
    reason="ffmpeg and pdftoppm are required for the slideshow smoke test",
)
def test_pdf_to_mp4_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        wrapper = Path(shutil.which("pdftoppm") or "")
        native_dir = wrapper.parents[2] / "native" / "poppler" / "Library" / "bin"
        if (native_dir / "pdftoppm.exe").exists():
            monkeypatch.setenv("PATH", f"{native_dir}{os.pathsep}{os.environ['PATH']}")
    pdf = tmp_path / "source.pdf"
    work_dir = tmp_path / "work"
    output = work_dir / "output.mp4"
    _write_pdf(pdf)

    asyncio.run(
        convert_slideshow(
            pdf,
            output,
            work_dir=work_dir,
            slide_seconds=0.2,
            limits=SlideshowLimits(max_total_duration_seconds=0.4),
            video_args_factory=lambda: ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
            ffmpeg_runner=run_slideshow_ffmpeg,
        )
    )

    assert output.is_file()
    assert output.stat().st_size > 0
    if shutil.which("ffprobe"):
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate:format=duration",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        info = json.loads(probe.stdout)
        streams = info["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        assert video["codec_name"] == "h264"
        assert (video["width"], video["height"]) == (1920, 1080)
        assert video["pix_fmt"] == "yuv420p"
        assert video["r_frame_rate"] == "30/1"
        assert all(stream["codec_type"] != "audio" for stream in streams)
        assert float(info["format"]["duration"]) == pytest.approx(0.4, abs=0.15)
