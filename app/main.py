from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANG_RE = re.compile(r"^[A-Za-z0-9_-]{2,12}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}_[A-Za-z0-9_-]{2,12}_[a-f0-9]{8}$")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
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
    ffmpeg_video_encoder = os.getenv("FFMPEG_VIDEO_ENCODER", "libx264")
    ffmpeg_video_preset = os.getenv("FFMPEG_VIDEO_PRESET")
    ffmpeg_video_crf = os.getenv("FFMPEG_VIDEO_CRF", "23")
    ffmpeg_video_cq = os.getenv("FFMPEG_VIDEO_CQ", "23")
    ytdlp_cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    ytdlp_bin = os.getenv("YTDLP_BIN")
    ytdlp_proxy = os.getenv("YTDLP_PROXY")
    ytdlp_extra_args = os.getenv("YTDLP_EXTRA_ARGS", "")
    youtube_data_api_key = os.getenv("YOUTUBE_DATA_API_KEY")


settings = Settings()
app = FastAPI(title="YouTube subtitle burned MP4 proxy")

_global_encode_lock = asyncio.Semaphore(1)
_inflight_lock = asyncio.Lock()
_inflight: dict[str, asyncio.Task[Path]] = {}
_hls_inflight: dict[str, asyncio.Task[Path]] = {}


class CommandError(Exception):
    def __init__(self, args: list[str], message: str) -> None:
        super().__init__(message)
        self.args_list = args
        self.message = message


def cache_key(video_id: str, lang: str) -> str:
    return f"{video_id}_{lang}_{render_profile_id()}"


def render_profile_id() -> str:
    return hashlib.sha1(
        "\n".join([subtitle_force_style(), *ffmpeg_video_args()]).encode("utf-8")
    ).hexdigest()[:8]


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
    args = [yt_dlp_executable(), "--ignore-config"]
    if settings.ytdlp_cookies_file:
        args.extend(["--cookies", settings.ytdlp_cookies_file])
    if settings.ytdlp_proxy:
        args.extend(["--proxy", settings.ytdlp_proxy])
    if settings.ytdlp_extra_args:
        args.extend(shlex.split(settings.ytdlp_extra_args))
    return args


def yt_dlp_executable() -> str:
    if settings.ytdlp_bin:
        return settings.ytdlp_bin

    executable_name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    venv_executable = Path(sys.executable).parent / executable_name
    if venv_executable.exists():
        return str(venv_executable)

    return "yt-dlp"


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


async def run_command(
    args: list[str],
    cwd: Path | None = None,
    raise_http: bool = True,
) -> str:
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
        print(
            f"Command failed: {' '.join(args)}\n{message}",
            flush=True,
        )
        if not raise_http:
            raise CommandError(args, message)
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


def ffmpeg_video_args(encoder: str | None = None) -> list[str]:
    encoder = (encoder or settings.ffmpeg_video_encoder).strip().lower()
    if encoder in {"nvenc", "h264_nvenc"}:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            settings.ffmpeg_video_preset or "fast",
            "-rc",
            "vbr",
            "-cq",
            settings.ffmpeg_video_cq,
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]

    if encoder != "libx264":
        raise HTTPException(status_code=500, detail=f"Unsupported video encoder: {encoder}")

    return [
        "-c:v",
        "libx264",
        "-preset",
        settings.ffmpeg_video_preset or "veryfast",
        "-crf",
        settings.ffmpeg_video_crf,
        "-pix_fmt",
        "yuv420p",
    ]


def wants_nvenc() -> bool:
    return settings.ffmpeg_video_encoder.strip().lower() in {"nvenc", "h264_nvenc"}


def is_nvenc_driver_error(message: str) -> bool:
    return (
        "Driver does not support the required nvenc API version" in message
        or "The minimum required Nvidia driver for nvenc" in message
        or "Cannot load nvcuda.dll" in message
        or "No NVENC capable devices found" in message
    )


async def run_ffmpeg_with_optional_nvenc_fallback(args: list[str]) -> None:
    try:
        await run_command(args, raise_http=False)
        return
    except CommandError as error:
        if not wants_nvenc() or not is_nvenc_driver_error(error.message):
            raise HTTPException(
                status_code=502,
                detail=error.message[-1000:] or f"Command failed: {error.args_list[0]}",
            ) from error

        print(
            "NVENC is unavailable with the current NVIDIA driver; retrying with libx264.",
            flush=True,
        )

    fallback_args: list[str] = []
    skip_next = False
    video_arg_values = {
        "-preset",
        "-rc",
        "-cq",
        "-b:v",
        "-pix_fmt",
        "-c:v",
    }
    for index, item in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if item in video_arg_values:
            skip_next = index + 1 < len(args)
            continue
        fallback_args.append(item)

    output_path = Path(fallback_args[-1])
    if output_path.exists():
        output_path.unlink()

    insert_at = fallback_args.index("-c:a")
    fallback_args[insert_at:insert_at] = ffmpeg_video_args("libx264")
    await run_command(fallback_args)


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
    await run_ffmpeg_with_optional_nvenc_fallback(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            ffmpeg_subtitle_arg(subtitle),
            *ffmpeg_video_args(),
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

    await run_ffmpeg_with_optional_nvenc_fallback(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            ffmpeg_subtitle_arg(subtitle),
            *ffmpeg_video_args(),
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


def require_youtube_data_api_key() -> str:
    if not settings.youtube_data_api_key:
        raise HTTPException(
            status_code=500,
            detail="YOUTUBE_DATA_API_KEY is not configured",
        )
    return settings.youtube_data_api_key


def extract_playlist_id(value: str) -> str:
    value = value.strip()
    if YOUTUBE_ID_RE.fullmatch(value):
        return value
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    playlist_id = query.get("list", [""])[0]
    if YOUTUBE_ID_RE.fullmatch(playlist_id):
        return playlist_id
    raise HTTPException(status_code=400, detail="Invalid YouTube playlist id or URL")


def extract_channel_lookup(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("@"):
        return "forHandle", value
    if value.startswith("UC") and YOUTUBE_ID_RE.fullmatch(value):
        return "id", value

    parsed = urllib.parse.urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and YOUTUBE_ID_RE.fullmatch(parts[1]):
        return "id", parts[1]
    if parts and parts[0].startswith("@"):
        return "forHandle", parts[0]
    if len(parts) >= 2 and parts[0] in {"c", "user"}:
        return "forUsername", parts[1]

    raise HTTPException(status_code=400, detail="Invalid YouTube channel id, handle, or URL")


def youtube_api_get_sync(path: str, params: dict[str, str | int]) -> dict:
    params = {**params, "key": require_youtube_data_api_key()}
    url = f"https://www.googleapis.com/youtube/v3/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except json.JSONDecodeError:
            detail = body
        raise HTTPException(status_code=error.code, detail=detail) from error
    except urllib.error.URLError as error:
        raise HTTPException(status_code=502, detail=str(error.reason)) from error


async def youtube_api_get(path: str, params: dict[str, str | int]) -> dict:
    return await asyncio.to_thread(youtube_api_get_sync, path, params)


async def fetch_playlist_title(playlist_id: str) -> str | None:
    data = await youtube_api_get(
        "playlists",
        {
            "part": "snippet",
            "id": playlist_id,
            "maxResults": 1,
            "fields": "items(snippet(title))",
        },
    )
    items = data.get("items") or []
    if not items:
        return None
    return items[0].get("snippet", {}).get("title")


async def fetch_channel_uploads_playlist(channel: str) -> tuple[str, str]:
    lookup_key, lookup_value = extract_channel_lookup(channel)
    data = await youtube_api_get(
        "channels",
        {
            "part": "snippet,contentDetails",
            lookup_key: lookup_value,
            "maxResults": 1,
            "fields": "items(snippet(title),contentDetails(relatedPlaylists(uploads)))",
        },
    )
    items = data.get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="YouTube channel not found")
    item = items[0]
    title = item.get("snippet", {}).get("title") or lookup_value
    uploads = (
        item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads:
        raise HTTPException(status_code=404, detail="Channel uploads playlist not found")
    return uploads, title


async def fetch_playlist_tracks(playlist_id: str, max_items: int) -> list[dict[str, str]]:
    tracks: list[dict[str, str]] = []
    page_token = ""
    while len(tracks) < max_items:
        page_size = min(50, max_items - len(tracks))
        params: dict[str, str | int] = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": page_size,
            "fields": (
                "nextPageToken,items(snippet(title,resourceId(videoId)))"
            ),
        }
        if page_token:
            params["pageToken"] = page_token

        data = await youtube_api_get("playlistItems", params)
        for item in data.get("items") or []:
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            if not video_id:
                continue
            tracks.append(
                {
                    "title": snippet.get("title") or video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return tracks


def normalize_yamaplayer_mode(mode: int) -> int:
    if mode not in {0, 1, 2}:
        raise HTTPException(status_code=400, detail="mode must be 0, 1, or 2")
    return mode


def normalize_max_items(max_items: int) -> int:
    if max_items < 1 or max_items > 5000:
        raise HTTPException(status_code=400, detail="maxItems must be between 1 and 5000")
    return max_items


def yamaplayer_export_response(
    playlist_name: str,
    youtube_list_id: str,
    tracks: list[dict[str, str]],
    mode: int,
) -> Response:
    body = {
        "playlists": [
            {
                "active": True,
                "name": playlist_name,
                "youtubeListId": youtube_list_id,
                "tracks": [
                    {
                        "mode": mode,
                        "title": track["title"],
                        "url": track["url"],
                    }
                    for track in tracks
                ],
            }
        ]
    }
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", playlist_name).strip("_") or "yamaplayer"
    return Response(
        json.dumps(body, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.json"',
            "Cache-Control": "no-cache",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    default_lang = json.dumps(settings.default_lang)
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Tools</title>
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
    .tabs {{
      display: flex;
      gap: 8px;
      margin: 0 0 18px;
    }}
    .tabs button {{
      min-height: 38px;
      color: #17202a;
      background: #e7ebf0;
    }}
    .tabs button[aria-selected="true"] {{
      color: #fff;
      background: #1f6feb;
    }}
    .tool[hidden] {{
      display: none;
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
    .json-row {{
      display: grid;
      grid-template-columns: 1fr 120px 140px;
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
      .json-row {{
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
    <h1>YouTube Tools</h1>
    <div class="tabs" role="tablist" aria-label="Tool">
      <button type="button" id="videoTab" role="tab" aria-controls="converter" aria-selected="true">Video</button>
      <button type="button" id="jsonTab" role="tab" aria-controls="jsonExporter" aria-selected="false">JSON</button>
    </div>
    <form id="converter" class="tool" aria-labelledby="videoTab">
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
    <form id="jsonExporter" class="tool" aria-labelledby="jsonTab" hidden>
      <label>
        Channel or Playlist URL
        <input id="sourceUrl" name="sourceUrl" type="url" placeholder="https://www.youtube.com/@channel or https://www.youtube.com/playlist?list=..." autocomplete="off">
      </label>
      <div class="json-row">
        <label>
          Source
          <select id="sourceType" name="sourceType">
            <option value="auto">Auto</option>
            <option value="playlist">Playlist</option>
            <option value="channel">Channel</option>
          </select>
        </label>
        <label>
          Mode
          <select id="playerMode" name="playerMode">
            <option value="0">Unity</option>
            <option value="1">AVPro</option>
            <option value="2">Image</option>
          </select>
        </label>
        <label>
          Max
          <input id="maxItems" name="maxItems" type="number" min="1" max="5000" step="1" value="500">
        </label>
      </div>
      <label>
        Name
        <input id="playlistName" name="playlistName" type="text" placeholder="Optional" autocomplete="off">
      </label>
      <label>
        JSON URL
        <output id="jsonResult"></output>
      </label>
      <div class="actions">
        <button type="button" id="jsonCopyButton" class="secondary">Copy</button>
        <a id="downloadJsonLink" class="button" aria-disabled="true">Download JSON</a>
      </div>
      <output id="jsonMessage" class="error"></output>
    </form>
  </main>
  <script>
    const defaultLang = {default_lang};
    const form = document.getElementById("converter");
    const jsonForm = document.getElementById("jsonExporter");
    const videoTab = document.getElementById("videoTab");
    const jsonTab = document.getElementById("jsonTab");
    const input = document.getElementById("youtubeUrl");
    const lang = document.getElementById("lang");
    const mode = document.getElementById("mode");
    const result = document.getElementById("result");
    const message = document.getElementById("message");
    const openLink = document.getElementById("openLink");
    const copyButton = document.getElementById("copyButton");
    const sourceUrl = document.getElementById("sourceUrl");
    const sourceType = document.getElementById("sourceType");
    const playerMode = document.getElementById("playerMode");
    const maxItems = document.getElementById("maxItems");
    const playlistName = document.getElementById("playlistName");
    const jsonResult = document.getElementById("jsonResult");
    const jsonMessage = document.getElementById("jsonMessage");
    const downloadJsonLink = document.getElementById("downloadJsonLink");
    const jsonCopyButton = document.getElementById("jsonCopyButton");

    lang.value = defaultLang;

    function selectTool(tool) {{
      const jsonSelected = tool === "json";
      form.hidden = jsonSelected;
      jsonForm.hidden = !jsonSelected;
      videoTab.setAttribute("aria-selected", String(!jsonSelected));
      jsonTab.setAttribute("aria-selected", String(jsonSelected));
    }}

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

    function detectJsonSource(value) {{
      const trimmed = value.trim();
      if (!trimmed) return "";
      if (/^PL[A-Za-z0-9_-]+$/.test(trimmed) || /^UU[A-Za-z0-9_-]+$/.test(trimmed)) return "playlist";
      if (/^UC[A-Za-z0-9_-]+$/.test(trimmed) || trimmed.startsWith("@")) return "channel";
      let url;
      try {{
        url = new URL(trimmed);
      }} catch {{
        return "";
      }}
      const parts = url.pathname.split("/").filter(Boolean);
      if (url.searchParams.get("list")) return "playlist";
      if (parts[0] === "channel" || parts[0] === "c" || parts[0] === "user" || (parts[0] || "").startsWith("@")) return "channel";
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

    function updateJson() {{
      const value = sourceUrl.value.trim();
      const selectedType = sourceType.value === "auto" ? detectJsonSource(value) : sourceType.value;
      const count = Number.parseInt(maxItems.value, 10);
      const modeValue = playerMode.value;
      if (!value) {{
        jsonResult.textContent = "";
        jsonMessage.textContent = "";
        downloadJsonLink.removeAttribute("href");
        downloadJsonLink.setAttribute("aria-disabled", "true");
        return;
      }}
      if (!selectedType) {{
        jsonResult.textContent = "";
        jsonMessage.textContent = "Invalid channel or playlist URL";
        downloadJsonLink.removeAttribute("href");
        downloadJsonLink.setAttribute("aria-disabled", "true");
        return;
      }}
      if (!Number.isInteger(count) || count < 1 || count > 5000) {{
        jsonResult.textContent = "";
        jsonMessage.textContent = "Max must be 1-5000";
        downloadJsonLink.removeAttribute("href");
        downloadJsonLink.setAttribute("aria-disabled", "true");
        return;
      }}
      const params = new URLSearchParams();
      params.set(selectedType === "playlist" ? "list" : "channel", value);
      params.set("mode", modeValue);
      params.set("maxItems", String(count));
      if (playlistName.value.trim()) params.set("name", playlistName.value.trim());
      const url = `${{location.origin}}/yamaplayer/${{selectedType}}?${{params.toString()}}`;
      jsonResult.textContent = url;
      jsonMessage.textContent = "";
      downloadJsonLink.href = url;
      downloadJsonLink.setAttribute("aria-disabled", "false");
    }}

    videoTab.addEventListener("click", () => selectTool("video"));
    jsonTab.addEventListener("click", () => selectTool("json"));
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
    sourceUrl.addEventListener("input", updateJson);
    sourceType.addEventListener("change", updateJson);
    playerMode.addEventListener("change", updateJson);
    maxItems.addEventListener("input", updateJson);
    playlistName.addEventListener("input", updateJson);
    jsonForm.addEventListener("submit", (event) => {{
      event.preventDefault();
      updateJson();
      if (downloadJsonLink.href) window.location.href = downloadJsonLink.href;
    }});
    jsonCopyButton.addEventListener("click", async () => {{
      updateJson();
      if (jsonResult.textContent) await navigator.clipboard.writeText(jsonResult.textContent);
    }});
  </script>
</body>
</html>"""


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/yamaplayer/playlist")
async def yamaplayer_playlist(
    list_id_or_url: str = Query(alias="list"),
    name: str | None = None,
    mode: int = 0,
    max_items: int = Query(default=500, alias="maxItems"),
    x_api_key: str | None = Header(default=None),
) -> Response:
    assert_authorized(x_api_key)
    playlist_id = extract_playlist_id(list_id_or_url)
    normalized_mode = normalize_yamaplayer_mode(mode)
    normalized_max_items = normalize_max_items(max_items)
    playlist_name = name or await fetch_playlist_title(playlist_id) or playlist_id
    tracks = await fetch_playlist_tracks(playlist_id, normalized_max_items)
    return yamaplayer_export_response(playlist_name, playlist_id, tracks, normalized_mode)


@app.get("/yamaplayer/channel")
async def yamaplayer_channel(
    channel: str,
    name: str | None = None,
    mode: int = 0,
    max_items: int = Query(default=500, alias="maxItems"),
    x_api_key: str | None = Header(default=None),
) -> Response:
    assert_authorized(x_api_key)
    normalized_mode = normalize_yamaplayer_mode(mode)
    normalized_max_items = normalize_max_items(max_items)
    uploads_playlist_id, channel_title = await fetch_channel_uploads_playlist(channel)
    playlist_name = name or channel_title
    tracks = await fetch_playlist_tracks(uploads_playlist_id, normalized_max_items)
    return yamaplayer_export_response(
        playlist_name,
        uploads_playlist_id,
        tracks,
        normalized_mode,
    )


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
