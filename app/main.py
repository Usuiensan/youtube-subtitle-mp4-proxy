from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANG_RE = re.compile(r"^[A-Za-z0-9_-]{2,12}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}_[A-Za-z0-9_-]{2,12}_[a-f0-9]{8}$")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env.local"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_FILE)


class Settings:
    cache_dir = Path(os.getenv("CACHE_DIR", "/tmp/youtube-mp4-cache"))
    default_lang = os.getenv("DEFAULT_LANG", "ja")
    max_duration_seconds = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
    max_height = int(os.getenv("MAX_HEIGHT", "720"))
    cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    job_timeout_seconds = int(os.getenv("JOB_TIMEOUT_SECONDS", "7200"))
    api_key = os.getenv("API_KEY")
    subtitle_font = os.getenv("SUBTITLE_FONT", "BIZ UDGothic")
    subtitle_font_size = int(os.getenv("SUBTITLE_FONT_SIZE", "20"))
    subtitle_margin_v = int(os.getenv("SUBTITLE_MARGIN_V", "34"))
    subtitle_margin_l = int(os.getenv("SUBTITLE_MARGIN_L", "24"))
    subtitle_margin_r = int(os.getenv("SUBTITLE_MARGIN_R", "24"))
    subtitle_primary_colour = os.getenv("SUBTITLE_PRIMARY_COLOUR", "&H00FFFFFF")
    subtitle_back_colour = os.getenv("SUBTITLE_BACK_COLOUR", "&H99000000")
    hls_segment_seconds = int(os.getenv("HLS_SEGMENT_SECONDS", "6"))
    hls_ready_timeout_seconds = int(os.getenv("HLS_READY_TIMEOUT_SECONDS", "1800"))
    ytdlp_cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    ytdlp_proxy = os.getenv("YTDLP_PROXY")
    ytdlp_extra_args = os.getenv("YTDLP_EXTRA_ARGS", "")


settings = Settings()
app = FastAPI(title="YouTube subtitle burned MP4 proxy")

_global_encode_lock = asyncio.Semaphore(1)
_inflight_lock = asyncio.Lock()
_inflight: dict[str, asyncio.Task[Path]] = {}
_hls_inflight: dict[str, asyncio.Task[Path]] = {}


def cache_key(video_id: str, lang: str) -> str:
    return f"{video_id}_{lang}_{subtitle_style_id()}"


def subtitle_style_id() -> str:
    return hashlib.sha1(subtitle_force_style().encode("utf-8")).hexdigest()[:8]


def entry_dir(key: str) -> Path:
    return settings.cache_dir / key


def output_path(key: str) -> Path:
    return entry_dir(key) / "output.mp4"


def hls_dir(key: str) -> Path:
    return entry_dir(key) / "hls"


def hls_playlist_path(key: str) -> Path:
    return hls_dir(key) / "index.m3u8"


def meta_path(key: str) -> Path:
    return entry_dir(key) / "meta.json"


def write_meta(key: str, video_id: str, lang: str, info: dict, mode: str) -> None:
    meta_path(key).write_text(
        json.dumps(
            {
                "video_id": video_id,
                "lang": lang,
                "title": info.get("title"),
                "duration": info.get("duration"),
                "created_at": int(time.time()),
                "mode": mode,
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )


def validate_input(video_id: str, lang: str) -> None:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video id")
    if not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid subtitle language")


def assert_authorized(x_api_key: str | None) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def yt_dlp_base_args() -> list[str]:
    args = ["yt-dlp", "--ignore-config"]
    if settings.ytdlp_cookies_file:
        args.extend(["--cookies", settings.ytdlp_cookies_file])
    if settings.ytdlp_proxy:
        args.extend(["--proxy", settings.ytdlp_proxy])
    if settings.ytdlp_extra_args:
        args.extend(shlex.split(settings.ytdlp_extra_args))
    return args


def is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return time.time() - path.stat().st_mtime < settings.cache_ttl_seconds


def is_hls_fresh(key: str) -> bool:
    playlist = hls_playlist_path(key)
    if not is_fresh(playlist):
        return False
    if "#EXT-X-ENDLIST" not in playlist.read_text(encoding="utf-8", errors="ignore"):
        return False
    return any(hls_dir(key).glob("segment_*.ts"))


def is_hls_started(key: str) -> bool:
    playlist = hls_playlist_path(key)
    if not playlist.exists():
        return False
    return any(hls_dir(key).glob("segment_*.ts"))


def cleanup_expired_cache() -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for child in settings.cache_dir.iterdir():
        if not child.is_dir() or child.name.startswith(".work-"):
            continue
        mp4 = child / "output.mp4"
        hls_playlist = child / "hls" / "index.m3u8"
        newest = max(
            (p.stat().st_mtime for p in (mp4, hls_playlist) if p.exists()),
            default=0,
        )
        if newest == 0 or now - newest > settings.cache_ttl_seconds:
            shutil.rmtree(child, ignore_errors=True)


async def run_command(args: list[str], cwd: Path | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.job_timeout_seconds
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise HTTPException(status_code=504, detail="Conversion timed out")

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(
            status_code=502,
            detail=message[-1000:] or f"Command failed: {args[0]}",
        )
    return stdout.decode("utf-8", errors="replace")


async def fetch_video_info(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    raw = await run_command(
        yt_dlp_base_args()
        + [
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            url,
        ]
    )
    return json.loads(raw)


def assert_duration_allowed(info: dict) -> None:
    duration = info.get("duration")
    if not isinstance(duration, (int, float)):
        raise HTTPException(status_code=422, detail="Video duration is unknown")
    if duration > settings.max_duration_seconds:
        raise HTTPException(status_code=413, detail="Video is longer than 30 minutes")


def find_downloaded_video(work_dir: Path) -> Path:
    candidates = sorted(
        p for p in work_dir.iterdir() if p.suffix.lower() in {".mp4", ".mkv", ".webm"}
    )
    if not candidates:
        raise HTTPException(status_code=502, detail="yt-dlp did not produce a video")
    return max(candidates, key=lambda p: p.stat().st_size)


def find_subtitle(work_dir: Path, lang: str) -> Path:
    subtitles = sorted(work_dir.glob(f"*.{lang}.srt")) or sorted(work_dir.glob("*.srt"))
    if not subtitles:
        raise HTTPException(status_code=422, detail=f"No subtitle found for language: {lang}")
    return subtitles[0]


def escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def subtitle_force_style() -> str:
    return ",".join(
        [
            f"FontName={settings.subtitle_font}",
            f"FontSize={settings.subtitle_font_size}",
            f"PrimaryColour={settings.subtitle_primary_colour}",
            f"BackColour={settings.subtitle_back_colour}",
            "BorderStyle=4",
            "Outline=0",
            "Shadow=0",
            f"MarginV={settings.subtitle_margin_v}",
            f"MarginL={settings.subtitle_margin_l}",
            f"MarginR={settings.subtitle_margin_r}",
            "Alignment=2",
        ]
    )


def ffmpeg_subtitle_arg(path: Path) -> str:
    value = path.as_posix()
    return (
        f"subtitles='{escape_filter_value(value)}':"
        f"force_style='{escape_filter_value(subtitle_force_style())}'"
    )


async def download_sources(video_id: str, lang: str, work_dir: Path) -> tuple[Path, Path]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    format_selector = (
        f"bv*[height<={settings.max_height}]+ba/"
        f"b[height<={settings.max_height}]/"
        "bv*+ba/"
        "b"
    )
    await run_command(
        yt_dlp_base_args()
        + [
            "--no-playlist",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            lang,
            "--convert-subs",
            "srt",
            "--paths",
            str(work_dir),
            "-f",
            format_selector,
            "--merge-output-format",
            "mkv",
            "-o",
            "%(id)s.%(ext)s",
            url,
        ],
        cwd=work_dir,
    )
    return find_downloaded_video(work_dir), find_subtitle(work_dir, lang)


async def burn_subtitles(video: Path, subtitle: Path, destination: Path) -> None:
    tmp_output = destination.with_suffix(".tmp.mp4")
    await run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            ffmpeg_subtitle_arg(subtitle),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(tmp_output),
        ]
    )
    tmp_output.replace(destination)


async def create_hls(video: Path, subtitle: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for old_file in destination_dir.glob("*"):
        if old_file.is_file():
            old_file.unlink()

    await run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            ffmpeg_subtitle_arg(subtitle),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-f",
            "hls",
            "-hls_time",
            str(settings.hls_segment_seconds),
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(destination_dir / "segment_%05d.ts"),
            str(destination_dir / "index.m3u8"),
        ]
    )


async def create_mp4(video_id: str, lang: str) -> Path:
    key = cache_key(video_id, lang)
    final_output = output_path(key)
    if is_fresh(final_output):
        return final_output

    async with _global_encode_lock:
        if is_fresh(final_output):
            return final_output

        work_dir = settings.cache_dir / f".work-{key}-{uuid.uuid4().hex}"
        final_output.parent.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = await fetch_video_info(video_id)
            assert_duration_allowed(info)
            video, subtitle = await download_sources(video_id, lang, work_dir)
            await burn_subtitles(video, subtitle, final_output)
            write_meta(key, video_id, lang, info, "mp4")
            return final_output
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def create_hls_job(video_id: str, lang: str) -> Path:
    key = cache_key(video_id, lang)
    playlist = hls_playlist_path(key)
    if is_hls_fresh(key):
        return playlist

    async with _global_encode_lock:
        if is_hls_fresh(key):
            return playlist

        work_dir = settings.cache_dir / f".work-{key}-{uuid.uuid4().hex}"
        playlist.parent.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = await fetch_video_info(video_id)
            assert_duration_allowed(info)
            video, subtitle = await download_sources(video_id, lang, work_dir)
            write_meta(key, video_id, lang, info, "hls")
            await create_hls(video, subtitle, playlist.parent)
            return playlist
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def get_or_create_mp4(video_id: str, lang: str) -> Path:
    key = cache_key(video_id, lang)
    cached = output_path(key)
    if is_fresh(cached):
        return cached

    async with _inflight_lock:
        task = _inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(create_mp4(video_id, lang))
            _inflight[key] = task

    try:
        return await task
    finally:
        if task.done():
            async with _inflight_lock:
                if _inflight.get(key) is task:
                    _inflight.pop(key, None)


async def wait_until_hls_ready(key: str, task: asyncio.Task[Path]) -> Path:
    playlist = hls_playlist_path(key)
    deadline = time.monotonic() + settings.hls_ready_timeout_seconds
    while time.monotonic() < deadline:
        if is_hls_started(key):
            return playlist
        if task.done():
            return await task
        await asyncio.sleep(0.5)
    raise HTTPException(status_code=504, detail="HLS playlist was not ready in time")


async def get_or_start_hls(video_id: str, lang: str) -> Path:
    key = cache_key(video_id, lang)
    if is_hls_fresh(key):
        return hls_playlist_path(key)

    async with _inflight_lock:
        task = _hls_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(create_hls_job(video_id, lang))
            _hls_inflight[key] = task

    try:
        return await wait_until_hls_ready(key, task)
    finally:
        if task.done():
            async with _inflight_lock:
                if _hls_inflight.get(key) is task:
                    _hls_inflight.pop(key, None)


def parse_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Invalid byte range")

    start_raw, end_raw = match.groups()
    if start_raw == "" and end_raw == "":
        raise HTTPException(status_code=416, detail="Invalid byte range")
    if start_raw == "":
        suffix_length = int(end_raw)
        start = max(file_size - suffix_length, 0)
        end = file_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1

    if start >= file_size or end < start:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable")
    return start, min(end, file_size - 1)


async def file_iterator(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    with path.open("rb") as file:
        file.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def mp4_response(request: Request, path: Path) -> Response:
    file_size = path.stat().st_size
    byte_range = parse_range(request.headers.get("range"), file_size)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": f"public, max-age={settings.cache_ttl_seconds}",
    }
    if byte_range is None:
        return FileResponse(path, media_type="video/mp4", headers=headers)

    start, end = byte_range
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
        }
    )
    return StreamingResponse(
        file_iterator(path, start, end),
        status_code=206,
        media_type="video/mp4",
        headers=headers,
    )


def hls_playlist_response(request: Request, key: str, playlist: Path) -> Response:
    base_url = str(request.base_url).rstrip("/")
    lines = playlist.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        if not line or line.startswith("#") or "://" in line:
            rewritten.append(line)
        else:
            rewritten.append(f"{base_url}/hls/{key}/{line}")
    body = "\n".join(rewritten) + "\n"
    return PlainTextResponse(
        body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    default_lang = json.dumps(settings.default_lang)
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Subtitle URL</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: "Segoe UI", "Yu Gothic", Meiryo, sans-serif;
      background: #f6f7f8;
      color: #15171a;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    main {{
      width: min(720px, 100%);
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 26px;
      font-weight: 700;
    }}
    form {{
      display: grid;
      gap: 14px;
    }}
    label {{
      display: grid;
      gap: 7px;
      font-size: 14px;
      font-weight: 600;
    }}
    input, select {{
      box-sizing: border-box;
      width: 100%;
      min-height: 42px;
      border: 1px solid #c8ced6;
      border-radius: 8px;
      padding: 9px 11px;
      font: inherit;
      background: #fff;
      color: inherit;
    }}
    .row {{
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 12px;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    button, a.button {{
      min-height: 42px;
      border: 0;
      border-radius: 8px;
      padding: 9px 14px;
      font: inherit;
      font-weight: 700;
      color: #fff;
      background: #1f6feb;
      text-decoration: none;
      cursor: pointer;
    }}
    button.secondary {{
      color: #17202a;
      background: #e7ebf0;
    }}
    a.button[aria-disabled="true"] {{
      pointer-events: none;
      opacity: .45;
    }}
    output {{
      display: block;
      min-height: 22px;
      overflow-wrap: anywhere;
      font-family: Consolas, "Courier New", monospace;
      font-size: 14px;
    }}
    .error {{
      color: #b42318;
      font-weight: 600;
    }}
    @media (max-width: 560px) {{
      .row {{
        grid-template-columns: 1fr;
      }}
      h1 {{
        font-size: 22px;
      }}
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        background: #101316;
        color: #f2f4f7;
      }}
      input, select {{
        background: #191d22;
        border-color: #3a424d;
      }}
      button.secondary {{
        color: #f2f4f7;
        background: #303842;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>YouTube Subtitle URL</h1>
    <form id="converter">
      <label>
        YouTube URL
        <input id="youtubeUrl" name="youtubeUrl" type="url" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" required>
      </label>
      <div class="row">
        <label>
          Lang
          <input id="lang" name="lang" value="" maxlength="12" autocomplete="off">
        </label>
        <label>
          Output
          <select id="mode" name="mode">
            <option value="youtube-hls">HLS playlist</option>
            <option value="youtube">MP4</option>
          </select>
        </label>
      </div>
      <label>
        Converted URL
        <output id="result"></output>
      </label>
      <div class="actions">
        <button type="button" id="copyButton" class="secondary">Copy</button>
        <a id="openLink" class="button" target="_blank" rel="noopener" aria-disabled="true">Open New Tab</a>
      </div>
      <output id="message" class="error"></output>
    </form>
  </main>
  <script>
    const defaultLang = {default_lang};
    const form = document.getElementById("converter");
    const input = document.getElementById("youtubeUrl");
    const lang = document.getElementById("lang");
    const mode = document.getElementById("mode");
    const result = document.getElementById("result");
    const message = document.getElementById("message");
    const openLink = document.getElementById("openLink");
    const copyButton = document.getElementById("copyButton");

    lang.value = defaultLang;

    function extractVideoId(value) {{
      const trimmed = value.trim();
      if (/^[A-Za-z0-9_-]{{11}}$/.test(trimmed)) return trimmed;
      let url;
      try {{
        url = new URL(trimmed);
      }} catch {{
        return "";
      }}
      const host = url.hostname.replace(/^www\\./, "");
      if (host === "youtu.be") {{
        return url.pathname.split("/").filter(Boolean)[0] || "";
      }}
      if (!host.endsWith("youtube.com")) return "";
      const watchId = url.searchParams.get("v");
      if (watchId) return watchId;
      const parts = url.pathname.split("/").filter(Boolean);
      if (["shorts", "embed", "live"].includes(parts[0])) return parts[1] || "";
      return "";
    }}

    function update() {{
      const videoId = extractVideoId(input.value);
      const language = (lang.value || defaultLang).trim();
      if (!input.value.trim()) {{
        result.textContent = "";
        message.textContent = "";
        openLink.removeAttribute("href");
        openLink.setAttribute("aria-disabled", "true");
        return;
      }}
      if (!/^[A-Za-z0-9_-]{{11}}$/.test(videoId)) {{
        result.textContent = "";
        message.textContent = "Invalid YouTube URL";
        openLink.removeAttribute("href");
        openLink.setAttribute("aria-disabled", "true");
        return;
      }}
      if (!/^[A-Za-z0-9_-]{{2,12}}$/.test(language)) {{
        result.textContent = "";
        message.textContent = "Invalid language";
        openLink.removeAttribute("href");
        openLink.setAttribute("aria-disabled", "true");
        return;
      }}
      const url = `${{location.origin}}/${{mode.value}}/${{videoId}}/${{language}}`;
      result.textContent = url;
      message.textContent = "";
      openLink.href = url;
      openLink.setAttribute("aria-disabled", "false");
    }}

    input.addEventListener("input", update);
    lang.addEventListener("input", update);
    mode.addEventListener("change", update);
    form.addEventListener("submit", (event) => {{
      event.preventDefault();
      update();
      if (openLink.href) window.open(openLink.href, "_blank", "noopener");
    }});
    copyButton.addEventListener("click", async () => {{
      update();
      if (result.textContent) await navigator.clipboard.writeText(result.textContent);
    }});
  </script>
</body>
</html>"""


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/youtube/{video_id}")
@app.get("/youtube/{video_id}/{lang}")
async def youtube(
    video_id: str,
    request: Request,
    lang: str | None = None,
    x_api_key: str | None = Header(default=None),
) -> Response:
    lang = lang or settings.default_lang
    validate_input(video_id, lang)
    assert_authorized(x_api_key)
    cleanup_expired_cache()
    path = await get_or_create_mp4(video_id, lang)
    return mp4_response(request, path)


@app.get("/youtube-hls/{video_id}")
@app.get("/youtube-hls/{video_id}/{lang}")
async def youtube_hls(
    video_id: str,
    request: Request,
    lang: str | None = None,
    x_api_key: str | None = Header(default=None),
) -> Response:
    lang = lang or settings.default_lang
    validate_input(video_id, lang)
    assert_authorized(x_api_key)
    cleanup_expired_cache()
    key = cache_key(video_id, lang)
    playlist = await get_or_start_hls(video_id, lang)
    return hls_playlist_response(request, key, playlist)


@app.get("/hls/{key}/{filename}")
async def hls_asset(key: str, filename: str) -> Response:
    if not KEY_RE.fullmatch(key):
        raise HTTPException(status_code=400, detail="Invalid HLS key")
    if filename != "index.m3u8" and not re.fullmatch(r"segment_\d{5}\.ts", filename):
        raise HTTPException(status_code=400, detail="Invalid HLS filename")

    path = hls_dir(key) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="HLS asset not found")

    if filename.endswith(".m3u8"):
        return FileResponse(
            path,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    return FileResponse(
        path,
        media_type="video/mp2t",
        headers={"Cache-Control": f"public, max-age={settings.cache_ttl_seconds}"},
    )
