from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import srt
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from app.translation import (
    TranslationSettings,
    google_translate_events,
    load_srt,
    save_srt,
    translate_srt_with_local_worker,
)


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANG_RE = re.compile(r"^[A-Za-z0-9_-]{2,12}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}(?:_[A-Za-z0-9_-]{2,32})+_[a-f0-9]{8}$")
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
    cache_hot_dir = Path(os.getenv("CACHE_HOT_DIR", os.getenv("CACHE_DIR", "/tmp/youtube-mp4-cache")))
    cache_archive_dir = (
        Path(os.environ["CACHE_ARCHIVE_DIR"]) if os.getenv("CACHE_ARCHIVE_DIR") else None
    )
    cache_archive_after_seconds = int(os.getenv("CACHE_ARCHIVE_AFTER_SECONDS", "604800"))
    cache_hot_min_free_bytes = int(os.getenv("CACHE_HOT_MIN_FREE_BYTES", "0"))
    cache_promote_archive_on_access = os.getenv("CACHE_PROMOTE_ARCHIVE_ON_ACCESS", "1") != "0"
    prepare_job_retention_seconds = int(os.getenv("PREPARE_JOB_RETENTION_SECONDS", "86400"))
    default_lang = os.getenv("DEFAULT_LANG", "ja")
    max_duration_seconds = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
    max_height = int(os.getenv("MAX_HEIGHT", "720"))
    cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    job_timeout_seconds = int(os.getenv("JOB_TIMEOUT_SECONDS", "7200"))
    subtitle_font = os.getenv("SUBTITLE_FONT", "BIZ UDGothic")
    subtitle_font_size = int(os.getenv("SUBTITLE_FONT_SIZE", "20"))
    subtitle_margin_v = int(os.getenv("SUBTITLE_MARGIN_V", "34"))
    subtitle_margin_l = int(os.getenv("SUBTITLE_MARGIN_L", "24"))
    subtitle_margin_r = int(os.getenv("SUBTITLE_MARGIN_R", "24"))
    subtitle_primary_colour = os.getenv("SUBTITLE_PRIMARY_COLOUR", "&H00FFFFFF")
    subtitle_back_colour = os.getenv("SUBTITLE_BACK_COLOUR", "&H40000000")
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
    discord_prepare_token = os.getenv("DISCORD_PREPARE_TOKEN")
    webui_temp_key_secret = os.getenv("WEBUI_TEMP_KEY_SECRET", os.getenv("DISCORD_PREPARE_TOKEN", ""))
    youtube_proxy_base_url = os.getenv("YOUTUBE_PROXY_BASE_URL", "").rstrip("/")
    translation_enabled = os.getenv("TRANSLATION_ENABLED", "1") != "0"
    translation_source_langs = os.getenv("TRANSLATION_SOURCE_LANGS", "en,ko,zh-Hans,zh-Hant,zh,zh-CN,zh-TW")
    local_llm_engine = os.getenv("LOCAL_LLM_ENGINE", "openai_compatible")
    local_llm_model = os.getenv("LOCAL_LLM_MODEL", "qwen2.5:3b-instruct-q4_K_M")
    local_llm_timeout_seconds = int(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "300"))
    local_llm_target_window_seconds = int(os.getenv("LOCAL_LLM_TARGET_WINDOW_SECONDS", "120"))
    local_llm_target_max_events = int(os.getenv("LOCAL_LLM_TARGET_MAX_EVENTS", "10"))
    local_llm_context_before_seconds = int(os.getenv("LOCAL_LLM_CONTEXT_BEFORE_SECONDS", os.getenv("LOCAL_LLM_CONTEXT_SECONDS", "120")))
    local_llm_context_before_max_events = int(os.getenv("LOCAL_LLM_CONTEXT_BEFORE_MAX_EVENTS", "25"))
    local_llm_context_after_seconds = int(os.getenv("LOCAL_LLM_CONTEXT_AFTER_SECONDS", os.getenv("LOCAL_LLM_CONTEXT_SECONDS", "120")))
    local_llm_context_after_max_events = int(os.getenv("LOCAL_LLM_CONTEXT_AFTER_MAX_EVENTS", "25"))
    translation_fallback_engine = os.getenv("TRANSLATION_FALLBACK_ENGINE", "google_cloud")
    translation_topic = os.getenv("TRANSLATION_TOPIC", "")
    translation_glossary = os.getenv("TRANSLATION_GLOSSARY", "")
    google_cloud_project = os.getenv("GOOGLE_CLOUD_PROJECT", "")


settings = Settings()


class MetricsManager:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.data = {
            "download_speed": [],      # bytes per second
            "encode_speed_ratio": [],  # video_duration / encode_time
            "translate_speed": [],     # subtitle_events / translate_time
            "archive_speed": []        # bytes per second
        }
        self.load()

    def load(self):
        if self.file_path.exists():
            try:
                self.data = json.loads(self.file_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def save(self):
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def record_download(self, size_bytes: float, time_seconds: float):
        if time_seconds > 0:
            self.data.setdefault("download_speed", []).append(size_bytes / time_seconds)
            self.save()

    def record_encode(self, duration: float, time_seconds: float):
        if time_seconds > 0:
            self.data.setdefault("encode_speed_ratio", []).append(duration / time_seconds)
            self.save()

    def record_translate(self, events_count: int, time_seconds: float):
        if time_seconds > 0:
            self.data.setdefault("translate_speed", []).append(events_count / time_seconds)
            self.save()

    def record_archive(self, size_bytes: float, time_seconds: float):
        if time_seconds > 0:
            self.data.setdefault("archive_speed", []).append(size_bytes / time_seconds)
            self.save()

    def get_avg(self, key: str, fallback: float) -> float:
        vals = self.data.get(key)
        if not vals:
            return fallback
        # keep last 50 entries
        vals = vals[-50:]
        return sum(vals) / len(vals)

    def reset(self):
        self.data = {
            "download_speed": [],
            "encode_speed_ratio": [],
            "translate_speed": [],
            "archive_speed": [],
        }
        self.save()


metrics_manager = MetricsManager(settings.cache_hot_dir / "metrics.json")
app = FastAPI(title="YouTube subtitle burned MP4 proxy")

_global_encode_lock = asyncio.Semaphore(1)
_inflight_lock = asyncio.Lock()
_inflight: dict[str, asyncio.Task[Path]] = {}
_hls_inflight: dict[str, asyncio.Task[Path]] = {}
_prepare_lock = asyncio.Lock()
_prepare_jobs: dict[str, dict] = {}
_prepare_by_key: dict[str, str] = {}
_prepare_batches: dict[str, dict] = {}
_cleanup_lock = asyncio.Lock()


def estimate_total_seconds(
    duration: float,
    has_sources: bool,
    needs_translation: bool,
    subtitle_events_count: int | None = None
) -> int:
    dl_speed = metrics_manager.get_avg("download_speed", 3 * 1024 * 1024)
    enc_ratio = metrics_manager.get_avg("encode_speed_ratio", 3.0)
    tr_speed = metrics_manager.get_avg("translate_speed", 1.0)
    
    # Estimate size as 1.5 Mbps for 720p video
    est_size = duration * 1.5 * 1024 * 1024 / 8
    
    dl_time = 0.0 if has_sources else (est_size / dl_speed)
    
    if needs_translation:
        events = subtitle_events_count if subtitle_events_count is not None else (duration / 2.0)
        tr_time = events / tr_speed
    else:
        tr_time = 0.0
        
    enc_time = duration / enc_ratio
    
    # add a small buffer (e.g. 10 seconds for process startup)
    total = dl_time + tr_time + enc_time + 10.0
    return max(15, int(total))


class YtdlpProgressParser:
    def __init__(self):
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.percent_re = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
        self.speed_re = re.compile(r"at\s+(\S+)")
        self.eta_re = re.compile(r"ETA\s+(\S+)")

    def parse_line(self, line: str):
        pct_match = self.percent_re.search(line)
        if pct_match:
            try:
                self.percent = float(pct_match.group(1))
            except ValueError:
                pass
        speed_match = self.speed_re.search(line)
        if speed_match:
            self.speed = speed_match.group(1)
        eta_match = self.eta_re.search(line)
        if eta_match:
            self.eta = eta_match.group(1)


class FfmpegProgressParser:
    def __init__(self, duration_seconds: float):
        self.duration = duration_seconds
        self.out_time_seconds = 0.0
        self.speed = 1.0
        self.percent = 0.0

    def parse_line(self, line: str):
        if "=" in line:
            parts = line.strip().split("=", 1)
            if len(parts) == 2:
                key, val = parts
                if key == "out_time_us":
                    try:
                        us = int(val)
                        self.out_time_seconds = us / 1000000.0
                        if self.duration > 0:
                            self.percent = min(100.0, (self.out_time_seconds / self.duration) * 100.0)
                    except ValueError:
                        pass
                elif key == "speed":
                    val_str = val.strip().replace("x", "")
                    try:
                        self.speed = float(val_str)
                    except ValueError:
                        pass


def update_job_progress(
    job_id: str,
    phase: str,
    percent: float,
    eta_seconds: int | None = None,
    details: str = "",
):
    if job_id not in _prepare_jobs:
        return
    _prepare_jobs[job_id]["progress"] = {
        "phase": phase,
        "percent": percent,
        "eta_seconds": eta_seconds,
        "details": details,
        "updated_at": time.time(),
    }


class CommandError(Exception):
    def __init__(self, args: list[str], message: str) -> None:
        super().__init__(message)
        self.args_list = args
        self.message = message


def variant_id(
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> str:
    if not subtitle_source_lang and not translation_engine:
        return ""
    source = (subtitle_source_lang or "auto").lower()
    engine = normalize_translation_engine(translation_engine) if translation_engine else "auto"
    return f"{source}_{engine}"


def cache_key(
    video_id: str,
    lang: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> str:
    variant = variant_id(subtitle_source_lang, translation_engine)
    if variant:
        return f"{video_id}_{lang}_{variant}_{render_profile_id()}"
    return f"{video_id}_{lang}_{render_profile_id()}"


def render_profile_id() -> str:
    return hashlib.sha1(
        "\n".join([subtitle_force_style(), *ffmpeg_video_args(), translation_profile_id()]).encode("utf-8")
    ).hexdigest()[:8]


def translation_profile_id() -> str:
    return json.dumps(
        {
            "enabled": settings.translation_enabled,
            "target": "ja",
            "source_langs": settings.translation_source_langs,
            "engine": settings.local_llm_engine,
            "fallback": settings.translation_fallback_engine,
            "model": settings.local_llm_model,
            "window_seconds": settings.local_llm_target_window_seconds,
            "max_events": settings.local_llm_target_max_events,
            "context_before_seconds": settings.local_llm_context_before_seconds,
            "context_before_max_events": settings.local_llm_context_before_max_events,
            "context_after_seconds": settings.local_llm_context_after_seconds,
            "context_after_max_events": settings.local_llm_context_after_max_events,
        },
        sort_keys=True,
    )


def entry_dir(key: str) -> Path:
    return settings.cache_hot_dir / key


def archive_entry_dir(key: str) -> Path | None:
    if settings.cache_archive_dir is None:
        return None
    return settings.cache_archive_dir / key


def output_path(key: str) -> Path:
    return entry_dir(key) / "output.mp4"


def hls_dir(key: str) -> Path:
    return entry_dir(key) / "hls"


def hls_playlist_path(key: str) -> Path:
    return hls_dir(key) / "index.m3u8"


def meta_path(key: str) -> Path:
    return entry_dir(key) / "meta.json"


def source_dir(key: str) -> Path:
    return entry_dir(key) / "source"


def source_meta_path(key: str) -> Path:
    return entry_dir(key) / "source.json"


def translation_meta_path(key: str) -> Path:
    return entry_dir(key) / "source" / "translation.json"


def read_subtitle_meta(key: str) -> dict:
    for base in (entry_dir(key), archive_entry_dir(key)):
        if base is None:
            continue
        path = base / "source" / "translation.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        source_json = base / "source.json"
        if source_json.exists():
            data = json.loads(source_json.read_text(encoding="utf-8"))
            meta = data.get("subtitle_meta")
            if isinstance(meta, dict):
                return meta
    return {}


def write_meta(key: str, video_id: str, lang: str, info: dict, mode: str) -> None:
    meta_path(key).parent.mkdir(parents=True, exist_ok=True)
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


def write_source_meta(
    key: str,
    video_id: str,
    lang: str,
    info: dict,
    video: Path,
    subtitle: Path,
    subtitle_meta: dict | None = None,
) -> None:
    source_meta_path(key).write_text(
        json.dumps(
            {
                "video_id": video_id,
                "lang": lang,
                "title": info.get("title"),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url")
                or f"https://www.youtube.com/watch?v={video_id}",
                "source_video": str(video.relative_to(entry_dir(key))).replace("\\", "/"),
                "subtitle": str(subtitle.relative_to(entry_dir(key))).replace("\\", "/"),
                "subtitle_meta": subtitle_meta or {},
                "downloaded_at": int(time.time()),
                "yt_dlp": {
                    "id": info.get("id"),
                    "extractor": info.get("extractor"),
                    "format_id": info.get("format_id"),
                    "ext": info.get("ext"),
                    "resolution": info.get("resolution"),
                    "fps": info.get("fps"),
                },
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )


def move_replace(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    shutil.move(str(source), destination)


def hot_free_bytes() -> int:
    settings.cache_hot_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(settings.cache_hot_dir).free


def cache_entry_newest_mtime(path: Path) -> float:
    mp4 = path / "output.mp4"
    hls_playlist = path / "hls" / "index.m3u8"
    return max(
        (p.stat().st_mtime for p in (mp4, hls_playlist) if p.exists()),
        default=0,
    )


def is_usable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def hot_output_path(key: str) -> Path | None:
    path = output_path(key)
    return path if is_usable_file(path) else None


def archived_output_path(key: str) -> Path | None:
    archive_dir = archive_entry_dir(key)
    path = archive_dir / "output.mp4" if archive_dir else None
    return path if path and is_usable_file(path) else None


def hot_hls_playlist_path(key: str) -> Path | None:
    playlist = hls_playlist_path(key)
    if not is_usable_file(playlist):
        return None
    if "#EXT-X-ENDLIST" not in playlist.read_text(encoding="utf-8", errors="ignore"):
        return None
    if not any(hls_dir(key).glob("segment_*.ts")):
        return None
    return playlist


def archive_cache_entry(key: str) -> bool:
    archive_dir = archive_entry_dir(key)
    hot_dir = entry_dir(key)
    if archive_dir is None or not hot_dir.exists():
        return False
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive_dir = archive_dir.with_name(f".moving-{archive_dir.name}-{uuid.uuid4().hex}")
    if tmp_archive_dir.exists():
        shutil.rmtree(tmp_archive_dir, ignore_errors=True)
    if archive_dir.exists():
        shutil.rmtree(archive_dir, ignore_errors=True)
    shutil.copytree(hot_dir, tmp_archive_dir)
    tmp_archive_dir.replace(archive_dir)
    shutil.rmtree(hot_dir, ignore_errors=True)
    return True


def promote_archive_entry(key: str) -> bool:
    archive_dir = archive_entry_dir(key)
    hot_dir = entry_dir(key)
    if archive_dir is None or not archive_dir.exists() or hot_dir.exists():
        return False
    if not settings.cache_promote_archive_on_access:
        return False
    hot_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_hot_dir = hot_dir.with_name(f".promote-{hot_dir.name}-{uuid.uuid4().hex}")
    if tmp_hot_dir.exists():
        shutil.rmtree(tmp_hot_dir, ignore_errors=True)
    shutil.copytree(archive_dir, tmp_hot_dir)
    if hot_dir.exists():
        shutil.rmtree(tmp_hot_dir, ignore_errors=True)
        return False
    try:
        tmp_hot_dir.replace(hot_dir)
    except Exception:
        shutil.rmtree(tmp_hot_dir, ignore_errors=True)
        return False
    return True


def prepared_output_path(key: str) -> Path | None:
    hot = hot_output_path(key)
    if hot:
        return hot
    archive_dir = archive_entry_dir(key)
    archived = archive_dir / "output.mp4" if archive_dir else None
    if archived and is_usable_file(archived):
        if promote_archive_entry(key):
            return hot_output_path(key)
    return None


def prepared_hls_playlist_path(key: str) -> Path | None:
    hot = hot_hls_playlist_path(key)
    if hot:
        return hot
    archive_dir = archive_entry_dir(key)
    if archive_dir is None:
        return None
    archived_playlist = archive_dir / "hls" / "index.m3u8"
    if not is_usable_file(archived_playlist):
        return None
    if "#EXT-X-ENDLIST" not in archived_playlist.read_text(encoding="utf-8", errors="ignore"):
        return None
    if not any((archive_dir / "hls").glob("segment_*.ts")):
        return None
    if promote_archive_entry(key):
        return hot_hls_playlist_path(key)
    return None


def dir_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def archived_entry_size_bytes(key: str) -> int | None:
    archive_dir = archive_entry_dir(key)
    if archive_dir is None or not archive_dir.exists():
        return None
    return dir_size_bytes(archive_dir)


def archived_ready_entry_exists(key: str, mode: str) -> bool:
    archive_dir = archive_entry_dir(key)
    if archive_dir is None or not archive_dir.exists():
        return False
    if mode == "hls":
        playlist = archive_dir / "hls" / "index.m3u8"
        if not is_usable_file(playlist):
            return False
        if "#EXT-X-ENDLIST" not in playlist.read_text(encoding="utf-8", errors="ignore"):
            return False
        return any((archive_dir / "hls").glob("segment_*.ts"))
    return is_usable_file(archive_dir / "output.mp4")


def candidate_cache_keys(video_id: str, lang: str) -> list[str]:
    profile = render_profile_id()
    names: set[str] = set()
    for base in (settings.cache_hot_dir, settings.cache_archive_dir):
        if base is None or not base.exists():
            continue
        for child in base.glob(f"{video_id}_{lang}_*_{profile}"):
            if child.is_dir():
                names.add(child.name)
    return sorted(names)


def default_variant_priority(key: str) -> tuple[int, str]:
    if "_local_llm_" in key:
        return (1, key)
    if "_google_cloud_" in key:
        return (2, key)
    return (3, key)


def default_serving_key(video_id: str, lang: str, mode: str) -> str:
    exact = cache_key(video_id, lang)
    if mode == "hls":
        if hot_hls_playlist_path(exact):
            return exact
    elif hot_output_path(exact) or archived_output_path(exact):
        return exact

    for key in sorted(candidate_cache_keys(video_id, lang), key=default_variant_priority):
        if key == exact:
            continue
        if mode == "hls":
            if hot_hls_playlist_path(key):
                return key
        elif hot_output_path(key) or archived_output_path(key):
            return key
    return exact


def validate_input(video_id: str, lang: str) -> None:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video id")
    validate_lang(lang)


def validate_lang(lang: str) -> None:
    if not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid subtitle language")


def validate_translation_variant(source_lang: str, translation_engine: str) -> str:
    validate_lang(source_lang)
    return normalize_translation_engine(translation_engine)


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
    return hot_hls_playlist_path(key) is not None


def is_hls_started(key: str) -> bool:
    playlist = hls_playlist_path(key)
    if not playlist.exists():
        return False
    return any(hls_dir(key).glob("segment_*.ts"))


def cleanup_expired_cache() -> None:
    settings.cache_hot_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    expire_after = (
        settings.cache_archive_after_seconds
        if settings.cache_archive_dir is not None
        else settings.cache_ttl_seconds
    )
    candidates: list[tuple[float, str]] = []
    for child in settings.cache_hot_dir.iterdir():
        if not child.is_dir() or child.name.startswith(".work-"):
            continue
        newest = cache_entry_newest_mtime(child)
        if newest == 0:
            if any(child.iterdir()):
                continue
            shutil.rmtree(child, ignore_errors=True)
            continue
        candidates.append((newest, child.name))
        if now - newest > expire_after:
            if not archive_cache_entry(child.name):
                shutil.rmtree(child, ignore_errors=True)

    if settings.cache_archive_dir is None or settings.cache_hot_min_free_bytes <= 0:
        return

    for _newest, key in sorted(candidates):
        if hot_free_bytes() >= settings.cache_hot_min_free_bytes:
            break
        if entry_dir(key).exists():
            archive_cache_entry(key)


async def cleanup_expired_cache_async() -> None:
    async with _cleanup_lock:
        await asyncio.to_thread(cleanup_expired_cache)


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


def normalize_lang(value: str | None) -> str:
    return (value or "").split("-")[0].lower()


def subtitle_lang_available(subtitles: dict, lang: str) -> str | None:
    if lang in subtitles:
        return lang
    wanted = normalize_lang(lang)
    for candidate in subtitles.keys():
        if normalize_lang(candidate) == wanted:
            return candidate
    return None


def configured_translation_source_langs() -> list[str]:
    return [lang.strip() for lang in settings.translation_source_langs.split(",") if lang.strip()]


def normalize_translation_engine(value: str | None) -> str:
    engine = (value or settings.local_llm_engine or "local_llm").strip().lower()
    if engine in {"llm", "local", "local_llm", "openai_compatible"}:
        return "local_llm"
    if engine in {"google", "google_cloud", "google_translate"}:
        return "google_cloud"
    raise HTTPException(status_code=400, detail="translationEngine must be local_llm or google_cloud")


def manual_subtitle_candidates(info: dict, requested_lang: str) -> list[dict]:
    manual_subtitles = info.get("subtitles") or {}
    if not isinstance(manual_subtitles, dict):
        manual_subtitles = {}

    candidates = []
    seen: set[str] = set()
    for lang in manual_subtitles.keys():
        if normalize_lang(lang) == normalize_lang(requested_lang):
            continue
        normalized = lang.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(
            {
                "language": lang,
                "name": get_lang_name_ja(lang),
                "name_en": get_lang_name_en(lang),
                "source_kind": "manual",
            }
        )
    return candidates


def subtitle_choice_body(info: dict, requested_lang: str) -> dict:
    manual_subtitles = info.get("subtitles") or {}
    if not isinstance(manual_subtitles, dict):
        manual_subtitles = {}

    requested = subtitle_lang_available(manual_subtitles, requested_lang)
    body = {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "requested_language": requested_lang,
        "translation_enabled": settings.translation_enabled,
    }
    if requested:
        body.update(
            {
                "requires_choice": False,
                "selection": {
                    "requested_language": requested_lang,
                    "source_language": requested,
                    "translated": False,
                    "source_kind": "manual",
                },
            }
        )
        return body

    candidates = manual_subtitle_candidates(info, requested_lang)
    body.update(
        {
            "requires_choice": requested_lang == "ja" and settings.translation_enabled and bool(candidates),
            "candidates": candidates,
            "translation_engines": [
                {
                    "value": "local_llm",
                    "label": "LLM翻訳",
                    "default": True,
                },
                {
                    "value": "google_cloud",
                    "label": "Google翻訳",
                    "default": False,
                },
            ],
        }
    )
    if requested_lang != "ja" or not settings.translation_enabled:
        body["error"] = f"No subtitle found for language: {requested_lang}"
    elif not candidates:
        body["error"] = "No translatable manual subtitle found"
    return body


def select_subtitle_language(
    info: dict,
    requested_lang: str,
    source_lang: str | None = None,
    translation_engine: str | None = None,
) -> dict:
    manual_subtitles = info.get("subtitles") or {}
    if not isinstance(manual_subtitles, dict):
        manual_subtitles = {}

    requested = subtitle_lang_available(manual_subtitles, requested_lang)
    if requested:
        return {
            "requested_language": requested_lang,
            "source_language": requested,
            "translated": False,
            "source_kind": "manual",
        }

    if requested_lang != "ja" or not settings.translation_enabled:
        raise HTTPException(status_code=422, detail=f"No subtitle found for language: {requested_lang}")

    if source_lang:
        selected = subtitle_lang_available(manual_subtitles, source_lang)
        if not selected:
            raise HTTPException(status_code=422, detail=f"No subtitle found for source language: {source_lang}")
        return {
            "requested_language": requested_lang,
            "source_language": selected,
            "translated": True,
            "source_kind": "manual",
            "translation_engine_requested": normalize_translation_engine(translation_engine),
        }

    priorities: list[str] = []
    video_language = info.get("language")
    if isinstance(video_language, str) and video_language:
        priorities.append(video_language)
    priorities.extend(["en", "ko", "zh-Hans", "zh-Hant", "zh", "zh-CN", "zh-TW"])
    priorities.extend(configured_translation_source_langs())

    seen: set[str] = set()
    for lang in priorities:
        normalized = lang.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        selected = subtitle_lang_available(manual_subtitles, lang)
        if selected:
            return {
                "requested_language": requested_lang,
                "source_language": selected,
                "translated": True,
                "source_kind": "manual",
                "translation_engine_requested": normalize_translation_engine(translation_engine),
            }

    raise HTTPException(status_code=422, detail="No translatable manual subtitle found")


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


LANG_NAMES_JA = {
    "af": "アフリカーンス語",
    "sq": "アルバニア語",
    "am": "アムハラ語",
    "ar": "アラビア語",
    "hy": "アルメニア語",
    "as": "アッサム語",
    "az": "アゼルバイジャン語",
    "eu": "バスク語",
    "bn": "ベンガル語",
    "bg": "ブルガリア語",
    "my": "ビルマ語",
    "ca": "カタロニア語",
    "chr": "チェロキー語",
    "zh-hk": "中国語(香港)",
    "zh-cn": "中国語(簡体字)",
    "zh-tw": "中国語(繁体字)",
    "zh-hans": "中国語(簡体字)",
    "zh-hant": "中国語(繁体字)",
    "zh": "中国語",
    "hr": "クロアチア語",
    "cs": "チェコ語",
    "da": "デンマーク語",
    "nl": "オランダ語",
    "en-gb": "英語(英国)",
    "en": "英語",
    "et": "エストニア語",
    "fil": "フィリピノ語",
    "fi": "フィンランド語",
    "fr": "フランス語",
    "fr-ca": "フランス語(カナダ)",
    "gl": "ガリシア語",
    "ka": "ジョージア語",
    "de": "ドイツ語",
    "el": "ギリシャ語",
    "gu": "グジャラート語",
    "iw": "ヘブライ語",
    "he": "ヘブライ語",
    "hi": "ヒンディー語",
    "hu": "ハンガリー語",
    "is": "アイスランド語",
    "id": "インドネシア語",
    "ga": "アイルランド語",
    "it": "イタリア語",
    "ja": "日本語",
    "kn": "カンナダ語",
    "kk": "カザフ語",
    "km": "クメール語",
    "ko": "韓国語",
    "lo": "ラオ語",
    "lv": "ラトビア語",
    "lt": "リトアニア語",
    "mk": "マケドニア語",
    "ms": "マレー語",
    "ml": "マラヤーラム語",
    "mr": "マラーティー語",
    "mn": "モンゴル語",
    "ne": "ネパール語",
    "no": "ノルウェー語",
    "or": "オリヤー語",
    "fa": "ペルシア語",
    "pl": "ポーランド語",
    "pt-br": "ポルトガル語(ブラジル)",
    "pt-pt": "ポルトガル語(ポルトガル)",
    "pt": "ポルトガル語",
    "pa": "パンジャーブ語",
    "ro": "ルーマニア語",
    "ru": "ロシア語",
    "sr": "セルビア語",
    "si": "シンハラ語",
    "sk": "スロバキア語",
    "sl": "スロベニア語",
    "es": "スペイン語",
    "es-419": "スペイン語(ラテンアメリカ)",
    "sw": "スワヒリ語",
    "sv": "スウェーデン語",
    "ta": "タミル語",
    "te": "テルグ語",
    "th": "タイ語",
    "tr": "トルコ語",
    "uk": "ウクライナ語",
    "ur": "ウルドゥー語",
    "uz": "ウズベク語",
    "vi": "ベトナム語",
    "cy": "ウェールズ語",
    "zu": "ズールー語",
}


LANG_NAMES_EN = {
    "af": "Afrikaans",
    "sq": "Albanian",
    "am": "Amharic",
    "ar": "Arabic",
    "hy": "Armenian",
    "as": "Assamese",
    "az": "Azerbaijani",
    "eu": "Basque",
    "bn": "Bengali",
    "bg": "Bulgarian",
    "my": "Burmese",
    "ca": "Catalan",
    "chr": "Cherokee",
    "zh-hk": "Chinese (Hong Kong)",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "zh-hans": "Chinese (Simplified)",
    "zh-hant": "Chinese (Traditional)",
    "zh": "Chinese",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en-gb": "English (UK)",
    "en": "English",
    "et": "Estonian",
    "fil": "Filipino",
    "fi": "Finnish",
    "fr": "French",
    "fr-ca": "French (Canada)",
    "gl": "Galician",
    "ka": "Georgian",
    "de": "German",
    "el": "Greek",
    "gu": "Gujarati",
    "iw": "Hebrew",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "ga": "Irish",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "kk": "Kazakh",
    "km": "Khmer",
    "ko": "Korean",
    "lo": "Lao",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "mk": "Macedonian",
    "ms": "Malay",
    "ml": "Malayalam",
    "mr": "Marathi",
    "mn": "Mongolian",
    "ne": "Nepali",
    "no": "Norwegian",
    "or": "Oriya",
    "fa": "Persian",
    "pl": "Polish",
    "pt-br": "Portuguese (Brazil)",
    "pt-pt": "Portuguese (Portugal)",
    "pt": "Portuguese",
    "pa": "Punjabi",
    "ro": "Romanian",
    "ru": "Russian",
    "sr": "Serbian",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "es": "Spanish",
    "es-419": "Spanish (Latin America)",
    "sw": "Swahili",
    "sv": "Swedish",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "zu": "Zulu",
}


def get_lang_name_en(code: str) -> str:
    normalized = code.lower().strip()
    base = normalized.split("-")[0]
    return LANG_NAMES_EN.get(normalized) or LANG_NAMES_EN.get(base) or normalized


def get_lang_name_ja(code: str) -> str:
    normalized = code.lower().strip()
    base = normalized.split("-")[0]
    return LANG_NAMES_JA.get(normalized) or LANG_NAMES_JA.get(base) or code.upper()


def get_subtitle_overlay_label(subtitle_meta: dict) -> str:
    source_lang = subtitle_meta.get("source_language") or ""
    requested_lang = subtitle_meta.get("requested_language") or ""
    translated = subtitle_meta.get("translated", False)
    if translated and source_lang and requested_lang:
        src_ja = get_lang_name_ja(source_lang)
        req_ja = get_lang_name_ja(requested_lang)
        src_en = get_lang_name_en(source_lang)
        req_en = get_lang_name_en(requested_lang)
        line1 = f"[字幕]{src_ja} → {req_ja}"
        line2 = f"[subs]{src_en} → {req_en}"
        return f"{line1}\n{line2}"
    elif requested_lang:
        req_ja = get_lang_name_ja(requested_lang)
        req_en = get_lang_name_en(requested_lang)
        line1 = f"[字幕]{req_ja}"
        line2 = f"[subs]{req_en}"
        return f"{line1}\n{line2}"
    return "[subs]"


def find_japanese_font_file() -> str | None:
    candidates = [
        "/usr/local/share/fonts/truetype/form-udp-gothic/FORMUDPGothic-Regular.ttf",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/ipaexfont/ipaexg.ttf",
        "/usr/share/fonts/truetype/vlgothic/VL-PGothic-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def ffmpeg_subtitle_arg(path: Path, subtitle_meta: dict | None = None) -> str:
    value = path.as_posix()
    filter_str = (
        f"subtitles='{escape_filter_value(value)}':"
        f"force_style='{escape_filter_value(subtitle_force_style())}'"
    )
    if subtitle_meta:
        label = get_subtitle_overlay_label(subtitle_meta)
        lines = label.split("\n")
        
        font_file = find_japanese_font_file()
        if font_file:
            font_opt = f":fontfile='{font_file}'"
        else:
            font_opt = f":font='{settings.subtitle_font}'" if settings.subtitle_font else ""
            
        drawtext_filters = []
        for i, line in enumerate(lines):
            escaped_line = line.replace("'", "'\\\\''").replace(":", "\\:")
            y_expr = f"h/30+{i}*h/20"
            drawtext_filter = (
                f"drawtext=text='{escaped_line}'"
                f":x=h/30:y={y_expr}:fontsize=h/25:fontcolor=white@1.0"
                f":box=1:boxcolor=black@1.0:boxborderw=h/100"
                f":enable='lt(t,5)'{font_opt}"
            )
            drawtext_filters.append(drawtext_filter)
            
        filter_str = f"{filter_str},{','.join(drawtext_filters)}"
    return filter_str


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


async def run_yt_dlp_with_progress(args: list[str], job_id: str | None = None, cwd: Path | None = None) -> str:
    if job_id:
        if "--newline" not in args:
            args.insert(1, "--newline")

    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    parser = YtdlpProgressParser()
    stdout_chunks = []
    
    async def read_stdout():
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace")
            stdout_chunks.append(line_str)
            if job_id:
                parser.parse_line(line_str)
                eta_sec = None
                if parser.eta and ":" in parser.eta:
                    try:
                        parts = parser.eta.split(":")
                        if len(parts) == 2:
                            eta_sec = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            eta_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    except ValueError:
                        pass
                
                details = f"DL速度: {parser.speed} | ETA: {parser.eta}" if parser.speed else ""
                update_job_progress(job_id, "download", parser.percent, eta_seconds=eta_sec, details=details)
            
    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            
    try:
        if job_id:
            await asyncio.wait_for(
                asyncio.gather(read_stdout(), read_stderr()),
                timeout=settings.job_timeout_seconds
            )
        else:
            await process.communicate()
        await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="yt-dlp download timed out")
        
    if process.returncode != 0:
        raise CommandError(args, f"yt-dlp failed with exit code {process.returncode}")
        
    return "".join(stdout_chunks)


async def run_ffmpeg_with_progress(args: list[str], job_id: str, duration_seconds: float) -> None:
    args.insert(1, "-progress")
    args.insert(2, "-")
    
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    parser = FfmpegProgressParser(duration_seconds)
    
    async def read_stdout():
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace")
            parser.parse_line(line_str)
            
            remaining_seconds = None
            if parser.speed > 0 and duration_seconds > parser.out_time_seconds:
                remaining_seconds = int((duration_seconds - parser.out_time_seconds) / parser.speed)
                if job_id in _prepare_jobs:
                    _prepare_jobs[job_id]["eta_seconds"] = remaining_seconds
                    _prepare_jobs[job_id]["estimated_ready_at"] = int(time.time()) + remaining_seconds
            
            details = f"エンコード速度: {parser.speed:.2f}x" if parser.speed > 0 else ""
            phase = "hls" if "-f" in args and "hls" in args else "encode"
            update_job_progress(
                job_id,
                phase,
                parser.percent,
                eta_seconds=remaining_seconds,
                details=details
            )
            
    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            
    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr()),
            timeout=settings.job_timeout_seconds
        )
        await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="ffmpeg timed out")
        
    if process.returncode != 0:
        raise CommandError(args, f"ffmpeg failed with exit status {process.returncode}")


async def run_ffmpeg_with_optional_nvenc_fallback(
    args: list[str],
    job_id: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    try:
        if job_id and duration_seconds:
            await run_ffmpeg_with_progress(args, job_id, duration_seconds)
        else:
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
    
    if job_id and duration_seconds:
        await run_ffmpeg_with_progress(fallback_args, job_id, duration_seconds)
    else:
        await run_command(fallback_args)


def translation_settings() -> TranslationSettings:
    return TranslationSettings(
        enabled=settings.translation_enabled,
        target_window_seconds=settings.local_llm_target_window_seconds,
        target_max_events=settings.local_llm_target_max_events,
        context_before_seconds=settings.local_llm_context_before_seconds,
        context_before_max_events=settings.local_llm_context_before_max_events,
        context_after_seconds=settings.local_llm_context_after_seconds,
        context_after_max_events=settings.local_llm_context_after_max_events,
        model_name=settings.local_llm_model,
        engine=settings.local_llm_engine,
        fallback_engine=settings.translation_fallback_engine,
        glossary=settings.translation_glossary,
        topic=settings.translation_topic,
        google_project=settings.google_cloud_project,
    )


async def run_translation_worker(payload: dict) -> dict:
    work_dir = Path(payload["_work_dir"])
    input_path = work_dir / f"translation-input-{uuid.uuid4().hex}.json"
    output_path = work_dir / f"translation-output-{uuid.uuid4().hex}.json"
    clean_payload = {key: value for key, value in payload.items() if key != "_work_dir"}
    input_path.write_text(json.dumps(clean_payload, ensure_ascii=False), encoding="utf-8")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.translation_worker",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.local_llm_timeout_seconds,
        )
    except asyncio.TimeoutError as error:
        process.kill()
        await process.communicate()
        raise RuntimeError("local llm translation timed out") from error

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(message[-1000:] or "local llm translation failed")
    return json.loads(output_path.read_text(encoding="utf-8"))


async def translate_subtitle_if_needed(
    *,
    key: str,
    subtitle: Path,
    info: dict,
    selection: dict,
    work_dir: Path,
    job_id: str | None = None,
) -> tuple[Path, dict]:
    if not selection["translated"]:
        return subtitle, selection

    translated_path = work_dir / f"{subtitle.stem}.ja.translated.srt"
    requested_engine = normalize_translation_engine(selection.get("translation_engine_requested"))
    selected_settings = translation_settings()

    if requested_engine == "google_cloud":
        start_t = time.time()
        subtitles = load_srt(subtitle)
        if job_id:
            update_job_progress(job_id, "translate", 0.0, details="Google翻訳中...")
        translated_map = google_translate_events(
            subtitles,
            selection["requested_language"],
            selected_settings,
        )
        translated_subtitles = []
        for sub in subtitles:
            translated_subtitles.append(
                srt.Subtitle(
                    index=sub.index,
                    start=sub.start,
                    end=sub.end,
                    content=translated_map[str(sub.index)],
                    proprietary=sub.proprietary,
                )
            )
        if translated_subtitles:
            notice = f"{selection['source_language']}>>{selection['requested_language']} (google_cloud)"
            translated_subtitles[0] = srt.Subtitle(
                index=translated_subtitles[0].index,
                start=translated_subtitles[0].start,
                end=translated_subtitles[0].end,
                content=f"{notice}\n{translated_subtitles[0].content}",
                proprietary=translated_subtitles[0].proprietary,
            )
        save_srt(translated_path, translated_subtitles)
        metrics_manager.record_translate(len(subtitles), time.time() - start_t)
        if job_id:
            update_job_progress(job_id, "translate", 100.0, details="Google翻訳完了")
        metadata = {
            **selection,
            "translation_engine": "google_cloud",
            "translation_model": None,
            "translation_fallback_used": False,
            "translation_created_at": int(time.time()),
        }
        return translated_path, metadata

    async def worker(payload: dict) -> dict:
        payload["_work_dir"] = str(work_dir)
        return await run_translation_worker(payload)

    on_prog = None
    start_t = time.time()
    if job_id:
        def on_prog(current_window: int, total_windows: int):
            pct = (current_window / total_windows) * 100.0 if total_windows > 0 else 0.0
            elapsed = time.time() - start_t
            remaining = None
            if current_window > 0:
                avg_time = elapsed / current_window
                remaining = int(avg_time * (total_windows - current_window))
                if job_id in _prepare_jobs:
                    _prepare_jobs[job_id]["eta_seconds"] = remaining
                    _prepare_jobs[job_id]["estimated_ready_at"] = int(time.time()) + remaining
            
            details = f"翻訳中... {current_window}/{total_windows} ウィンドウ"
            update_job_progress(job_id, "translate", pct, eta_seconds=remaining, details=details)

    result = await translate_srt_with_local_worker(
        subtitle_path=subtitle,
        output_path=translated_path,
        video_title=str(info.get("title") or ""),
        source_language=selection["source_language"],
        target_language=selection["requested_language"],
        settings=selected_settings,
        run_worker=worker,
        on_progress=on_prog,
    )
    end_t = time.time()
    try:
        subtitles = load_srt(translated_path)
        metrics_manager.record_translate(len(subtitles), end_t - start_t)
    except Exception:
        pass
        
    metadata = {**selection, **result.metadata}
    return result.subtitle_path, metadata


async def download_sources(
    video_id: str,
    lang: str,
    work_dir: Path,
    info: dict,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> tuple[Path, Path, Path, dict]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    subtitle_selection = select_subtitle_language(
        info,
        lang,
        source_lang=subtitle_source_lang,
        translation_engine=translation_engine,
    )
    source_lang = subtitle_selection["source_language"]
    format_selector = (
        f"bv*[height<={settings.max_height}]+ba/"
        f"b[height<={settings.max_height}]/"
        "bv*+ba/"
        "b"
    )
    
    start_t = time.time()
    dl_args = yt_dlp_base_args() + [
        "--no-playlist",
        "--write-subs",
        "--sub-langs",
        source_lang,
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
    ]
    if job_id:
        await run_yt_dlp_with_progress(dl_args, job_id=job_id, cwd=work_dir)
    else:
        await run_command(dl_args, cwd=work_dir)
    end_t = time.time()
    
    video = find_downloaded_video(work_dir)
    original_subtitle = find_subtitle(work_dir, source_lang)
    
    metrics_manager.record_download(video.stat().st_size, end_t - start_t)
    
    subtitle, subtitle_meta = await translate_subtitle_if_needed(
        key=cache_key(video_id, lang, subtitle_source_lang, translation_engine),
        subtitle=original_subtitle,
        info=info,
        selection=subtitle_selection,
        work_dir=work_dir,
        job_id=job_id,
    )
    return video, original_subtitle, subtitle, subtitle_meta


async def burn_subtitles(
    video: Path,
    subtitle: Path,
    destination: Path,
    job_id: str | None = None,
    duration_seconds: float | None = None,
    subtitle_meta: dict | None = None,
) -> None:
    tmp_output = destination.with_suffix(".tmp.mp4")
    start_t = time.time()
    await run_ffmpeg_with_optional_nvenc_fallback(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            ffmpeg_subtitle_arg(subtitle, subtitle_meta),
            *ffmpeg_video_args(),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(tmp_output),
        ],
        job_id,
        duration_seconds
    )
    tmp_output.replace(destination)
    end_t = time.time()
    if duration_seconds:
        metrics_manager.record_encode(duration_seconds, end_t - start_t)


async def create_hls(
    video: Path,
    subtitle: Path,
    destination_dir: Path,
    job_id: str | None = None,
    duration_seconds: float | None = None,
    subtitle_meta: dict | None = None,
) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for old_file in destination_dir.glob("*"):
        if old_file.is_file():
            old_file.unlink()

    start_t = time.time()
    await run_ffmpeg_with_optional_nvenc_fallback(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            ffmpeg_subtitle_arg(subtitle, subtitle_meta),
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
        ],
        job_id,
        duration_seconds
    )
    end_t = time.time()
    if duration_seconds:
        metrics_manager.record_encode(duration_seconds, end_t - start_t)


def get_cached_video_info(key: str) -> dict | None:
    meta_p = source_meta_path(key)
    if meta_p.exists():
        try:
            return json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            pass

    archive_dir = archive_entry_dir(key)
    if archive_dir:
        archive_meta_p = archive_dir / "source" / "source.json"
        if archive_meta_p.exists():
            try:
                return json.loads(archive_meta_p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


def check_existing_sources(key: str) -> tuple[Path, Path, dict] | None:
    source_meta = get_cached_video_info(key)
    if not source_meta:
        return None

    base_dir = None
    for d in (entry_dir(key), archive_entry_dir(key)):
        if d and d.exists():
            base_dir = d
            break

    if not base_dir:
        return None

    source_video_rel = source_meta.get("source_video")
    if not source_video_rel:
        return None
    video_path = base_dir / source_video_rel
    if not video_path.exists() or video_path.stat().st_size == 0:
        return None

    subtitle_meta = source_meta.get("subtitle_meta") or {}
    source_lang = subtitle_meta.get("source_language")

    s_dir = base_dir / "source"
    if subtitle_meta.get("translated"):
        candidates = list(s_dir.glob(f"subtitle.{source_lang}.original.*"))
        if not candidates:
            return None
        original_subtitle_path = candidates[0]
    else:
        subtitle_rel = source_meta.get("subtitle")
        if not subtitle_rel:
            return None
        original_subtitle_path = base_dir / subtitle_rel
        if not original_subtitle_path.exists() or original_subtitle_path.stat().st_size == 0:
            return None

    return video_path, original_subtitle_path, subtitle_meta


async def prepare_sources(
    key: str,
    video_id: str,
    lang: str,
    work_dir: Path,
    info: dict,
    job_id: str | None = None,
    duration_seconds: float | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> tuple[Path, Path, dict]:
    existing = check_existing_sources(key)
    if existing and subtitle_source_lang:
        _saved_video, _original_subtitle, existing_subtitle_meta = existing
        existing_source_lang = existing_subtitle_meta.get("source_language")
        if normalize_lang(existing_source_lang) != normalize_lang(subtitle_source_lang):
            existing = None
    if existing:
        saved_video, original_subtitle, subtitle_meta = existing
        if translation_engine and subtitle_meta.get("translated"):
            subtitle_meta = {
                **subtitle_meta,
                "translation_engine_requested": normalize_translation_engine(translation_engine),
            }

        hot_dir = entry_dir(key)
        archive_dir = archive_entry_dir(key)
        if archive_dir and archive_dir.exists() and not hot_dir.exists():
            shutil.copytree(archive_dir, hot_dir)
            saved_video = hot_dir / saved_video.relative_to(archive_dir)
            original_subtitle = hot_dir / original_subtitle.relative_to(archive_dir)

        if subtitle_meta.get("translated"):
            subtitle, new_subtitle_meta = await translate_subtitle_if_needed(
                key=key,
                subtitle=original_subtitle,
                info=info,
                selection=subtitle_meta,
                work_dir=work_dir,
                job_id=job_id,
            )
            saved_subtitle = source_dir(key) / f"subtitle.{lang}.translated{subtitle.suffix.lower()}"
            move_replace(subtitle, saved_subtitle)
            translation_meta_path(key).write_text(
                json.dumps(new_subtitle_meta, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        else:
            saved_subtitle = original_subtitle
            new_subtitle_meta = subtitle_meta

        write_source_meta(key, video_id, lang, info, saved_video, saved_subtitle, new_subtitle_meta)
        return saved_video, saved_subtitle, new_subtitle_meta

    # Normal download path
    video, original_subtitle, subtitle, subtitle_meta = await download_sources(
        video_id,
        lang,
        work_dir,
        info,
        job_id=job_id,
        subtitle_source_lang=subtitle_source_lang,
        translation_engine=translation_engine,
    )
    source_dir(key).mkdir(parents=True, exist_ok=True)
    saved_video = source_dir(key) / f"input{video.suffix.lower()}"
    source_lang = subtitle_meta.get("source_language", lang)
    saved_original_subtitle = source_dir(key) / f"subtitle.{source_lang}.original{original_subtitle.suffix.lower()}"
    saved_subtitle = (
        source_dir(key) / f"subtitle.{lang}.translated{subtitle.suffix.lower()}"
        if subtitle_meta.get("translated")
        else source_dir(key) / f"subtitle.{lang}{subtitle.suffix.lower()}"
    )
    move_replace(video, saved_video)
    if original_subtitle != subtitle:
        move_replace(original_subtitle, saved_original_subtitle)
    move_replace(subtitle, saved_subtitle)
    if subtitle_meta.get("translated"):
        translation_meta_path(key).write_text(
            json.dumps(subtitle_meta, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    write_source_meta(key, video_id, lang, info, saved_video, saved_subtitle, subtitle_meta)
    return saved_video, saved_subtitle, subtitle_meta


async def create_mp4(
    video_id: str,
    lang: str,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Path:
    key = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    final_output = output_path(key)
    prepared = prepared_output_path(key)
    if prepared:
        return prepared

    async with _global_encode_lock:
        prepared = prepared_output_path(key)
        if prepared:
            return prepared

        work_dir = settings.cache_hot_dir / f".work-{key}-{uuid.uuid4().hex}"
        final_output.parent.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = await fetch_video_info(video_id)
            assert_duration_allowed(info)
            duration = float(info.get("duration") or 0.0)
            saved_video, saved_subtitle, subtitle_meta = await prepare_sources(
                key,
                video_id,
                lang,
                work_dir,
                info,
                job_id=job_id,
                duration_seconds=duration,
                subtitle_source_lang=subtitle_source_lang,
                translation_engine=translation_engine,
            )
            await burn_subtitles(
                saved_video,
                saved_subtitle,
                final_output,
                job_id=job_id,
                duration_seconds=duration,
                subtitle_meta=subtitle_meta,
            )
            write_meta(key, video_id, lang, info, "mp4")
            return final_output
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def create_hls_job(
    video_id: str,
    lang: str,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Path:
    key = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    playlist = hls_playlist_path(key)
    prepared = prepared_hls_playlist_path(key)
    if prepared:
        return prepared

    async with _global_encode_lock:
        prepared = prepared_hls_playlist_path(key)
        if prepared:
            return prepared

        work_dir = settings.cache_hot_dir / f".work-{key}-{uuid.uuid4().hex}"
        playlist.parent.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            info = await fetch_video_info(video_id)
            assert_duration_allowed(info)
            duration = float(info.get("duration") or 0.0)
            saved_video, saved_subtitle, subtitle_meta = await prepare_sources(
                key,
                video_id,
                lang,
                work_dir,
                info,
                job_id=job_id,
                duration_seconds=duration,
                subtitle_source_lang=subtitle_source_lang,
                translation_engine=translation_engine,
            )
            write_meta(key, video_id, lang, info, "hls")
            await create_hls(
                saved_video,
                saved_subtitle,
                playlist.parent,
                job_id=job_id,
                duration_seconds=duration,
                subtitle_meta=subtitle_meta,
            )
            return playlist
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def get_or_create_mp4(
    video_id: str,
    lang: str,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Path:
    key = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    cached = prepared_output_path(key)
    if cached:
        return cached

    async with _inflight_lock:
        task = _inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                create_mp4(
                    video_id,
                    lang,
                    job_id=job_id,
                    subtitle_source_lang=subtitle_source_lang,
                    translation_engine=translation_engine,
                )
            )
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


async def get_or_start_hls(
    video_id: str,
    lang: str,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Path:
    key = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    cached = prepared_hls_playlist_path(key)
    if cached:
        return cached

    async with _inflight_lock:
        task = _hls_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                create_hls_job(
                    video_id,
                    lang,
                    job_id=job_id,
                    subtitle_source_lang=subtitle_source_lang,
                    translation_engine=translation_engine,
                )
            )
            _hls_inflight[key] = task

    try:
        return await wait_until_hls_ready(key, task)
    finally:
        if task.done():
            async with _inflight_lock:
                if _hls_inflight.get(key) is task:
                    _hls_inflight.pop(key, None)


async def get_or_create_hls(
    video_id: str,
    lang: str,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Path:
    key = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    cached = prepared_hls_playlist_path(key)
    if cached:
        return cached

    async with _inflight_lock:
        task = _hls_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                create_hls_job(
                    video_id,
                    lang,
                    job_id=job_id,
                    subtitle_source_lang=subtitle_source_lang,
                    translation_engine=translation_engine,
                )
            )
            _hls_inflight[key] = task

    try:
        return await task
    finally:
        if task.done():
            async with _inflight_lock:
                if _hls_inflight.get(key) is task:
                    _hls_inflight.pop(key, None)


def require_prepare_auth(request: Request, allow_temp_key: bool = True) -> None:
    if not settings.discord_prepare_token:
        raise HTTPException(status_code=500, detail="DISCORD_PREPARE_TOKEN is not configured")
    expected = f"Bearer {settings.discord_prepare_token}"
    auth_header = request.headers.get("authorization")
    if auth_header and secrets.compare_digest(auth_header, expected):
        return
    if allow_temp_key and auth_header and is_valid_webui_temp_key(auth_header.removeprefix("Bearer ").strip()):
        return
    if not auth_header or not secrets.compare_digest(auth_header, expected):
        raise HTTPException(status_code=401, detail="Invalid prepare token")


JST = timezone(timedelta(hours=9))
WEBUI_TEMP_KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([A-Za-z0-9_-]{22,64})$")


def webui_temp_key_signature(expires_on: str) -> str:
    secret = settings.webui_temp_key_secret or ""
    if not secret:
        raise HTTPException(status_code=500, detail="WEBUI_TEMP_KEY_SECRET is not configured")
    digest = hmac_sha256(f"webui-temp-key:{expires_on}", secret)
    return digest[:32]


def hmac_sha256(message: str, secret: str) -> str:
    import hmac
    import base64

    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_webui_temp_key(days: int) -> dict:
    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days must be between 1 and 30")
    today = datetime.now(JST).date()
    expires_date = today + timedelta(days=days - 1)
    expires_on = expires_date.isoformat()
    key = f"{expires_on}-{webui_temp_key_signature(expires_on)}"
    expires_at = datetime.combine(expires_date + timedelta(days=1), datetime.min.time(), tzinfo=JST)
    return {
        "key": key,
        "expires_on": expires_on,
        "expires_at": int(expires_at.timestamp()),
        "timezone": "Asia/Tokyo",
    }


def is_valid_webui_temp_key(token: str) -> bool:
    match = WEBUI_TEMP_KEY_RE.fullmatch(token)
    if not match:
        return False
    expires_on = match.group(1)
    signature = match.group(2)
    try:
        expires_date = datetime.strptime(expires_on, "%Y-%m-%d").date()
    except ValueError:
        return False
    expires_at = datetime.combine(expires_date + timedelta(days=1), datetime.min.time(), tzinfo=JST)
    if datetime.now(JST) >= expires_at:
        return False
    try:
        expected = webui_temp_key_signature(expires_on)
    except HTTPException:
        return False
    return secrets.compare_digest(signature, expected)


def prepare_key(
    video_id: str,
    lang: str,
    mode: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> str:
    option_key = ""
    if subtitle_source_lang or translation_engine:
        option_key = f":src={subtitle_source_lang or ''}:engine={translation_engine or ''}"
    return f"{mode}:{cache_key(video_id, lang)}{option_key}"


def prepared_media_url(
    request: Request,
    video_id: str,
    lang: str,
    mode: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> str:
    base_url = settings.youtube_proxy_base_url or str(request.base_url).rstrip("/")
    suffix = ""
    if subtitle_source_lang or translation_engine:
        suffix = f"/{subtitle_source_lang or 'auto'}/{normalize_translation_engine(translation_engine)}"
    if mode == "hls":
        return f"{base_url}/youtube-hls/{video_id}/{lang}{suffix}"
    return f"{base_url}/youtube/{video_id}/{lang}{suffix}"


def prepare_status_url(request: Request, job_id: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/prepare/jobs/{job_id}"


def prepare_batch_status_url(request: Request, batch_id: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/prepare/batches/{batch_id}"


def prepare_ready_path(
    video_id: str,
    lang: str,
    mode: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Path | None:
    key = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    if mode == "hls":
        return hot_hls_playlist_path(key)
    return hot_output_path(key)


def validate_discord_user_id(discord_user_id: str | None) -> str | None:
    if discord_user_id is None or discord_user_id == "":
        return None
    if not re.fullmatch(r"\d{17,20}", discord_user_id):
        raise HTTPException(status_code=400, detail="Invalid Discord user id")
    return discord_user_id


def discord_mention(discord_user_id: str) -> str:
    return f"<@{discord_user_id}>"


def estimate_archive_prepare_seconds(key: str) -> int | None:
    size = archived_entry_size_bytes(key)
    if size is None:
        return None
    return max(5, int(size / (30 * 1024 * 1024)) + 5)


def estimate_conversion_seconds(duration: int | float | None, mode: str) -> int | None:
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None
    multiplier = 1.3 if mode == "hls" else 1.6
    return max(30, min(settings.job_timeout_seconds, int(duration * multiplier) + 60))


def update_job_eta(job_id: str, eta_seconds: int | None) -> None:
    if eta_seconds is None:
        return
    now = int(time.time())
    _prepare_jobs[job_id]["eta_seconds"] = eta_seconds
    _prepare_jobs[job_id]["estimated_ready_at"] = now + eta_seconds


def add_job_requester(job: dict, discord_user_id: str | None) -> None:
    if not discord_user_id:
        return
    requesters = job.setdefault("requesters", [])
    if discord_user_id not in requesters:
        requesters.append(discord_user_id)


def job_mentions(job: dict) -> list[str]:
    return [discord_mention(user_id) for user_id in job.get("requesters", [])]


def job_notification(job: dict) -> dict | None:
    mentions = job_mentions(job)
    if not mentions:
        return None
    prefix = " ".join(mentions)
    if job["status"] == "ready":
        return {
            "content": f"{prefix} 準備できました: {job['url']}",
            "mentions": mentions,
        }
    if job["status"] == "failed":
        return {
            "content": f"{prefix} 準備に失敗しました: {job.get('error', 'unknown error')}",
            "mentions": mentions,
        }
    return None


def job_response_body(job_id: str, job: dict, request: Request) -> dict:
    body = {
        "status": job["status"],
        "video_id": job["video_id"],
        "lang": job["lang"],
        "mode": job["mode"],
    }
    if job.get("title") is not None:
        body["title"] = job["title"]
    if job.get("duration") is not None:
        body["duration"] = job["duration"]
    if job.get("subtitle") is not None:
        body["subtitle"] = job["subtitle"]
    if job["status"] in {"queued", "running"}:
        body["job_id"] = job_id
        body["status_url"] = prepare_status_url(request, job_id)
    if job.get("eta_seconds") is not None:
        body["eta_seconds"] = job["eta_seconds"]
    if job.get("estimated_ready_at") is not None:
        body["estimated_ready_at"] = job["estimated_ready_at"]
    mentions = job_mentions(job)
    if mentions:
        body["mentions"] = mentions
    if job["status"] == "ready":
        body["url"] = job["url"]
    if job["status"] == "failed":
        body["error"] = job.get("error", "Prepare job failed")
    notification = job_notification(job)
    if notification:
        body["notification"] = notification
    return body


def batch_item_body(item: dict, request: Request) -> dict:
    job_id = item.get("job_id")
    if job_id:
        job = _prepare_jobs.get(job_id)
        if job:
            body = job_response_body(job_id, job, request)
            if item.get("title") is not None and body.get("title") is None:
                body["title"] = item["title"]
            return body

    body = {
        "status": item.get("status", "unknown"),
        "video_id": item["video_id"],
        "lang": item["lang"],
        "mode": item["mode"],
    }
    if item.get("title") is not None:
        body["title"] = item["title"]
    if item.get("url") is not None:
        body["url"] = item["url"]
    if item.get("error") is not None:
        body["error"] = item["error"]
    return body


def batch_response_body(batch_id: str, batch: dict, request: Request, include_items: bool = True) -> dict:
    item_bodies = [batch_item_body(item, request) for item in batch.get("items", [])]
    counts = {
        "total": len(item_bodies),
        "ready": sum(1 for item in item_bodies if item.get("status") == "ready"),
        "failed": sum(1 for item in item_bodies if item.get("status") == "failed"),
        "queued": sum(1 for item in item_bodies if item.get("status") == "queued"),
        "running": sum(1 for item in item_bodies if item.get("status") == "running"),
    }
    terminal_count = counts["ready"] + counts["failed"]
    if counts["total"] == 0:
        status = "failed"
    elif terminal_count < counts["total"]:
        status = "running" if counts["running"] else "queued"
    elif counts["failed"]:
        status = "failed"
    else:
        status = "ready"

    pending_etas = [
        item.get("eta_seconds")
        for item in item_bodies
        if item.get("status") in {"queued", "running"} and isinstance(item.get("eta_seconds"), (int, float))
    ]
    pending_ready_at = [
        item.get("estimated_ready_at")
        for item in item_bodies
        if item.get("status") in {"queued", "running"}
        and isinstance(item.get("estimated_ready_at"), (int, float))
    ]
    body = {
        "status": status,
        "batch_id": batch_id,
        "source_type": batch.get("source_type"),
        "source": batch.get("source"),
        "playlist_id": batch.get("playlist_id"),
        "playlist_name": batch.get("playlist_name"),
        "lang": batch.get("lang"),
        "mode": batch.get("mode"),
        "counts": counts,
        "status_url": prepare_batch_status_url(request, batch_id),
    }
    if pending_etas:
        body["eta_seconds"] = int(sum(pending_etas))
        body["estimated_ready_at"] = int(time.time()) + int(sum(pending_etas))
    elif pending_ready_at:
        body["estimated_ready_at"] = int(max(pending_ready_at))
    mentions = batch.get("mentions") or []
    if mentions:
        body["mentions"] = mentions
    if include_items:
        body["items"] = item_bodies

    if status in {"ready", "failed"} and mentions:
        prefix = " ".join(mentions)
        ready_count = counts["ready"]
        failed_count = counts["failed"]
        if status == "ready":
            content = f"{prefix} 一括準備が完了しました。{ready_count}/{counts['total']} 件 ready。"
        else:
            content = f"{prefix} 一括準備が終了しました。ready {ready_count}/{counts['total']} 件、failed {failed_count} 件。"
        ready_urls = [public_url for public_url in (item.get("url") for item in item_bodies) if public_url]
        if ready_urls:
            content += "\n" + "\n".join(ready_urls[:10])
            if len(ready_urls) > 10:
                content += f"\n...ほか {len(ready_urls) - 10} 件"
        body["notification"] = {"content": content, "mentions": mentions}
    return body


def prune_prepare_jobs() -> None:
    now = int(time.time())
    expired = [
        job_id
        for job_id, job in _prepare_jobs.items()
        if job.get("status") in {"ready", "failed"}
        and now - int(job.get("completed_at") or job.get("created_at") or now)
        > settings.prepare_job_retention_seconds
    ]
    for job_id in expired:
        _prepare_jobs.pop(job_id, None)

    if len(_prepare_jobs) <= 1000:
        return

    completed = sorted(
        (
            (int(job.get("completed_at") or job.get("created_at") or now), job_id)
            for job_id, job in _prepare_jobs.items()
            if job.get("status") in {"ready", "failed"}
        )
    )
    for _timestamp, job_id in completed[: max(0, len(_prepare_jobs) - 1000)]:
        _prepare_jobs.pop(job_id, None)

    expired_batches = [
        batch_id
        for batch_id, batch in _prepare_batches.items()
        if now - int(batch.get("created_at") or now) > settings.prepare_job_retention_seconds
    ]
    for batch_id in expired_batches:
        _prepare_batches.pop(batch_id, None)


async def run_prepare_job(
    job_id: str,
    job_key: str,
    video_id: str,
    lang: str,
    mode: str,
    url: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> None:
    _prepare_jobs[job_id]["status"] = "running"
    try:
        cache_id = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
        if archived_ready_entry_exists(cache_id, mode):
            update_job_eta(job_id, estimate_archive_prepare_seconds(cache_id))
        else:
            cached_info = get_cached_video_info(cache_id)
            if cached_info and cached_info.get("duration"):
                info = cached_info
                _prepare_jobs[job_id]["title"] = info.get("title")
                _prepare_jobs[job_id]["duration"] = info.get("duration")
                has_sources = check_existing_sources(cache_id) is not None
                sub_sel = info.get("subtitle_meta") or {}
                needs_translation = sub_sel.get("translated", False)
                eta = estimate_total_seconds(
                    duration=float(info.get("duration")),
                    has_sources=has_sources,
                    needs_translation=needs_translation
                )
                update_job_eta(job_id, eta)
            else:
                info = await fetch_video_info(video_id)
                assert_duration_allowed(info)
                _prepare_jobs[job_id]["title"] = info.get("title")
                _prepare_jobs[job_id]["duration"] = info.get("duration")
                eta = estimate_total_seconds(
                    duration=float(info.get("duration")),
                    has_sources=False,
                    needs_translation=True # assume true by default if not cached
                )
                update_job_eta(job_id, eta)
        if mode == "hls":
            await get_or_create_hls(
                video_id,
                lang,
                job_id=job_id,
                subtitle_source_lang=subtitle_source_lang,
                translation_engine=translation_engine,
            )
        else:
            await get_or_create_mp4(
                video_id,
                lang,
                job_id=job_id,
                subtitle_source_lang=subtitle_source_lang,
                translation_engine=translation_engine,
            )
        now = int(time.time())
        subtitle_meta = read_subtitle_meta(cache_id)
        _prepare_jobs[job_id].update(
            {
                "status": "ready",
                "url": url,
                "subtitle": subtitle_meta,
                "eta_seconds": 0,
                "estimated_ready_at": now,
                "completed_at": now,
            }
        )
    except Exception as error:
        now = int(time.time())
        _prepare_jobs[job_id].update(
            {
                "status": "failed",
                "error": str(getattr(error, "detail", None) or error)[-1000:],
                "eta_seconds": 0,
                "estimated_ready_at": now,
                "completed_at": now,
            }
        )
    finally:
        async with _prepare_lock:
            if _prepare_by_key.get(job_key) == job_id:
                _prepare_by_key.pop(job_key, None)


async def enqueue_prepare_job(
    request: Request,
    video_id: str,
    lang: str,
    mode: str,
    discord_user_id: str | None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> tuple[int, dict]:
    if subtitle_source_lang:
        if not LANG_RE.fullmatch(subtitle_source_lang):
            raise HTTPException(status_code=400, detail="Invalid subtitle source language")
    normalized_engine = normalize_translation_engine(translation_engine) if translation_engine else None
    ready = prepare_ready_path(video_id, lang, mode, subtitle_source_lang, normalized_engine)
    url = prepared_media_url(request, video_id, lang, mode, subtitle_source_lang, normalized_engine)
    if ready:
        cache_id = cache_key(video_id, lang, subtitle_source_lang, normalized_engine)
        job = {
            "status": "ready",
            "video_id": video_id,
            "lang": lang,
            "mode": mode,
            "url": url,
            "subtitle": read_subtitle_meta(cache_id),
            "requesters": [],
        }
        add_job_requester(job, discord_user_id)
        return 200, job_response_body("", job, request)

    job_key = prepare_key(video_id, lang, mode, subtitle_source_lang, normalized_engine)
    async with _prepare_lock:
        prune_prepare_jobs()
        existing_job_id = _prepare_by_key.get(job_key)
        if existing_job_id:
            job = _prepare_jobs.get(existing_job_id)
            if job and job.get("status") in {"queued", "running"}:
                add_job_requester(job, discord_user_id)
                return 202, job_response_body(existing_job_id, job, request)

        job_id = uuid.uuid4().hex
        cache_id = cache_key(video_id, lang, subtitle_source_lang, normalized_engine)
        cached_info = get_cached_video_info(cache_id)
        if archived_ready_entry_exists(cache_id, mode):
            eta_seconds = estimate_archive_prepare_seconds(cache_id)
        elif cached_info and cached_info.get("duration"):
            has_sources = check_existing_sources(cache_id) is not None
            sub_sel = cached_info.get("subtitle_meta") or {}
            needs_translation = sub_sel.get("translated", False)
            eta_seconds = estimate_total_seconds(
                duration=float(cached_info.get("duration")),
                has_sources=has_sources,
                needs_translation=needs_translation
            )
        else:
            eta_seconds = None
        estimated_ready_at = int(time.time()) + eta_seconds if eta_seconds is not None else None
        _prepare_by_key[job_key] = job_id
        _prepare_jobs[job_id] = {
            "status": "queued",
            "video_id": video_id,
            "lang": lang,
            "mode": mode,
            "url": url,
            "created_at": int(time.time()),
            "eta_seconds": eta_seconds,
            "estimated_ready_at": estimated_ready_at,
            "requesters": [],
            "subtitle_source_lang": subtitle_source_lang,
            "translation_engine": normalized_engine,
        }
        if cached_info:
            if cached_info.get("title"):
                _prepare_jobs[job_id]["title"] = cached_info.get("title")
            if cached_info.get("duration"):
                _prepare_jobs[job_id]["duration"] = cached_info.get("duration")
        add_job_requester(_prepare_jobs[job_id], discord_user_id)
        asyncio.create_task(
            run_prepare_job(
                job_id,
                job_key,
                video_id,
                lang,
                mode,
                url,
                subtitle_source_lang=subtitle_source_lang,
                translation_engine=normalized_engine,
            )
        )
        return 202, job_response_body(job_id, _prepare_jobs[job_id], request)


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


def parse_youtube_url(value: str) -> urllib.parse.ParseResult:
    value = value.strip()
    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        return urllib.parse.urlparse("https://" + value)
    return urllib.parse.urlparse(value)


def extract_playlist_id(value: str) -> str:
    value = value.strip()
    if YOUTUBE_ID_RE.fullmatch(value):
        return value
    parsed = parse_youtube_url(value)
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

    parsed = parse_youtube_url(value)
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
                    "video_id": video_id,
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


def normalize_yamaplayer_url_mode(url_mode: str) -> str:
    if url_mode not in {"original", "mp4", "hls"}:
        raise HTTPException(status_code=400, detail="urlMode must be original, mp4, or hls")
    return url_mode


def yamaplayer_track_url(
    track: dict[str, str],
    url_mode: str,
    lang: str,
    base_url: str,
) -> str:
    if url_mode == "original":
        return track["url"]

    video_id = track["video_id"]
    route = "youtube-hls" if url_mode == "hls" else "youtube"
    return f"{base_url}/{route}/{video_id}/{lang}"


def yamaplayer_playlist_entry(
    playlist_name: str,
    youtube_list_id: str,
    tracks: list[dict[str, str]],
    mode: int,
    url_mode: str,
    lang: str,
    base_url: str,
) -> dict:
    return {
        "active": True,
        "name": playlist_name,
        "youtubeListId": youtube_list_id,
        "tracks": [
            {
                "mode": mode,
                "title": track["title"],
                "url": yamaplayer_track_url(track, url_mode, lang, base_url),
            }
            for track in tracks
        ],
    }


def yamaplayer_export_response(playlists: list[dict], filename_base: str) -> Response:
    body = {"playlists": playlists}
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename_base).strip("_") or "yamaplayer"
    return Response(
        json.dumps(body, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.json"',
            "Cache-Control": "no-cache",
        },
    )


def split_yamaplayer_sources(sources: str) -> list[str]:
    values = [line.strip() for line in sources.splitlines() if line.strip()]
    if not values:
        raise HTTPException(status_code=400, detail="At least one source is required")
    if len(values) > 100:
        raise HTTPException(status_code=400, detail="sources must contain 100 items or fewer")
    return values


def detect_yamaplayer_source_type(source: str) -> str:
    value = source.strip()
    if not value:
        return ""
    if value.startswith("@") or (value.startswith("UC") and YOUTUBE_ID_RE.fullmatch(value)):
        return "channel"
    if YOUTUBE_ID_RE.fullmatch(value):
        return "playlist"

    parsed = parse_youtube_url(value)
    parts = [part for part in parsed.path.split("/") if part]
    if urllib.parse.parse_qs(parsed.query).get("list"):
        return "playlist"
    if parts and (
        parts[0] == "channel"
        or parts[0] in {"c", "user"}
        or parts[0].startswith("@")
    ):
        return "channel"
    return ""


async def build_yamaplayer_playlist(
    source_type: str,
    source: str,
    mode: int,
    max_items: int,
    url_mode: str,
    lang: str,
    base_url: str,
    name: str | None = None,
) -> dict:
    if source_type == "auto":
        source_type = detect_yamaplayer_source_type(source)
    if source_type == "playlist":
        playlist_id = extract_playlist_id(source)
        playlist_name = name or await fetch_playlist_title(playlist_id) or playlist_id
        tracks = await fetch_playlist_tracks(playlist_id, max_items)
        return yamaplayer_playlist_entry(
            playlist_name,
            playlist_id,
            tracks,
            mode,
            url_mode,
            lang,
            base_url,
        )
    if source_type == "channel":
        uploads_playlist_id, channel_title = await fetch_channel_uploads_playlist(source)
        playlist_name = name or channel_title
        tracks = await fetch_playlist_tracks(uploads_playlist_id, max_items)
        return yamaplayer_playlist_entry(
            playlist_name,
            uploads_playlist_id,
            tracks,
            mode,
            url_mode,
            lang,
            base_url,
        )
    raise HTTPException(status_code=400, detail=f"Invalid source type for: {source}")


async def expand_prepare_source(source_type: str, source: str, max_items: int) -> tuple[str, str, str, list[dict[str, str]]]:
    if source_type == "auto":
        source_type = detect_yamaplayer_source_type(source)
    if source_type == "playlist":
        playlist_id = extract_playlist_id(source)
        playlist_name = await fetch_playlist_title(playlist_id) or playlist_id
        tracks = await fetch_playlist_tracks(playlist_id, max_items)
        return source_type, playlist_id, playlist_name, tracks
    if source_type == "channel":
        uploads_playlist_id, channel_title = await fetch_channel_uploads_playlist(source)
        tracks = await fetch_playlist_tracks(uploads_playlist_id, max_items)
        return source_type, uploads_playlist_id, channel_title, tracks
    raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, or channel")


async def enqueue_prepare_batch(
    request: Request,
    source: str,
    source_type: str,
    lang: str,
    mode: str,
    max_items: int,
    discord_user_id: str | None,
) -> tuple[int, dict]:
    resolved_type, playlist_id, playlist_name, tracks = await expand_prepare_source(
        source_type,
        source,
        max_items,
    )
    if not tracks:
        raise HTTPException(status_code=404, detail="No videos found in source")

    mentions = [discord_mention(discord_user_id)] if discord_user_id else []
    batch_id = uuid.uuid4().hex
    items = []
    any_pending = False
    for track in tracks:
        video_id = track["video_id"]
        status_code, body = await enqueue_prepare_job(
            request,
            video_id,
            lang,
            mode,
            discord_user_id,
        )
        if body.get("status") in {"queued", "running"}:
            any_pending = True
        items.append(
            {
                "video_id": video_id,
                "title": track.get("title") or body.get("title") or video_id,
                "lang": lang,
                "mode": mode,
                "status": body.get("status", "unknown"),
                "job_id": body.get("job_id"),
                "url": body.get("url"),
                "error": body.get("error"),
                "status_code": status_code,
            }
        )

    batch = {
        "source_type": resolved_type,
        "source": source,
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "lang": lang,
        "mode": mode,
        "created_at": int(time.time()),
        "mentions": mentions,
        "items": items,
    }
    async with _prepare_lock:
        prune_prepare_jobs()
        _prepare_batches[batch_id] = batch

    status_code = 202 if any_pending else 200
    return status_code, batch_response_body(batch_id, batch, request, include_items=True)


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
      color-scheme: light;
      --color-blue-900: #0017c1;
      --color-blue-1000: #00118f;
      --color-blue-1200: #000060;
      --color-blue-100: #d9e6ff;
      --color-blue-200: #c5d7fb;
      --color-yellow-300: #ffd43d;
      --color-gray-50: #f2f2f2;
      --color-gray-100: #e6e6e6;
      --color-gray-200: #cccccc;
      --color-gray-536: #767676;
      --color-gray-600: #666666;
      --color-gray-700: #4d4d4d;
      --color-gray-800: #333333;
      --color-gray-900: #1a1a1a;
      --color-error: #ec0000;
      font-family: "Noto Sans JP", -apple-system, BlinkMacSystemFont, sans-serif;
      background: #fff;
      color: var(--color-gray-800);
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      padding: calc(24 / 16 * 1rem);
      font-size: calc(17 / 16 * 1rem);
      line-height: 1.7;
      letter-spacing: 0.02em;
    }}
    main {{
      width: min(960px, 100%);
      margin-inline: auto;
    }}
    h1 {{
      margin: 0 0 calc(8 / 16 * 1rem);
      color: var(--color-gray-900);
      font-size: calc(45 / 16 * 1rem);
      line-height: 1.4;
      letter-spacing: 0;
      font-weight: 700;
    }}
    h2 {{
      margin: 0 0 calc(16 / 16 * 1rem);
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      padding-left: calc(16 / 16 * 1rem);
      color: var(--color-gray-900);
      font-size: calc(24 / 16 * 1rem);
      line-height: 1.5;
      letter-spacing: 0.02em;
    }}
    p {{
      margin: 0 0 calc(16 / 16 * 1rem);
    }}
    .lead {{
      max-width: 72ch;
      margin-bottom: calc(24 / 16 * 1rem);
      font-size: calc(18 / 16 * 1rem);
      line-height: 1.6;
    }}
    .page-header {{
      padding-block: calc(24 / 16 * 1rem) calc(32 / 16 * 1rem);
      border-bottom: 1px solid var(--color-gray-200);
    }}
    .utility-links {{
      display: flex;
      flex-wrap: wrap;
      gap: calc(16 / 16 * 1rem);
      margin-top: calc(16 / 16 * 1rem);
    }}
    a {{
      color: var(--color-blue-1000);
      text-decoration: underline;
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }}
    @media (hover: hover) {{
      a:hover {{
        text-decoration-thickness: 3px;
      }}
    }}
    a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {{
      outline: 4px solid #000;
      outline-offset: 2px;
      box-shadow: 0 0 0 2px var(--color-yellow-300);
    }}
    .section {{
      padding-block: calc(32 / 16 * 1rem);
      border-bottom: 1px solid var(--color-gray-200);
    }}
    .usage {{
      display: grid;
      gap: calc(16 / 16 * 1rem);
      margin: 0 0 calc(24 / 16 * 1rem);
      padding: 0;
      list-style: none;
    }}
    .usage li {{
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      padding: calc(8 / 16 * 1rem) calc(16 / 16 * 1rem);
      background: var(--color-gray-50);
    }}
    .usage strong {{
      display: block;
      color: var(--color-gray-900);
    }}
    form {{
      display: grid;
      gap: calc(16 / 16 * 1rem);
    }}
    .tabs {{
      display: flex;
      gap: calc(8 / 16 * 1rem);
      margin: 0 0 calc(24 / 16 * 1rem);
    }}
    .tabs button {{
      min-width: calc(96 / 16 * 1rem);
      min-height: calc(48 / 16 * 1rem);
      color: var(--color-blue-900);
      background: #fff;
      border: 1px solid currentcolor;
    }}
    .tabs button[aria-selected="true"] {{
      color: #fff;
      background: var(--color-blue-900);
      border-color: var(--color-blue-900);
    }}
    .tool[hidden] {{
      display: none;
    }}
    label {{
      display: grid;
      gap: calc(8 / 16 * 1rem);
      color: var(--color-gray-900);
      font-size: calc(16 / 16 * 1rem);
      line-height: 1.7;
      letter-spacing: 0.02em;
      font-weight: 600;
    }}
    input, select, textarea {{
      box-sizing: border-box;
      width: 100%;
      min-height: calc(48 / 16 * 1rem);
      border: 1px solid var(--color-gray-600);
      border-radius: 8px;
      padding: calc(12 / 16 * 1rem) calc(16 / 16 * 1rem);
      font: inherit;
      background: #fff;
      color: inherit;
    }}
    textarea {{
      min-height: 112px;
      resize: vertical;
    }}
    .row {{
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: calc(24 / 16 * 1rem);
    }}
    .json-row {{
      display: grid;
      grid-template-columns: 1fr 120px 120px 100px 120px;
      gap: calc(24 / 16 * 1rem);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: calc(8 / 16 * 1rem);
      align-items: center;
    }}
    .prepare-options {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: calc(24 / 16 * 1rem);
    }}
    button, a.button {{
      min-width: calc(96 / 16 * 1rem);
      min-height: calc(48 / 16 * 1rem);
      border: 4px double transparent;
      border-radius: 8px;
      padding: calc(8 / 16 * 1rem) calc(16 / 16 * 1rem);
      font: inherit;
      font-weight: 700;
      color: #fff;
      background: var(--color-blue-900);
      text-decoration: none;
      cursor: pointer;
    }}
    @media (hover: hover) {{
      button:hover, a.button:hover {{
        background: var(--color-blue-1000);
      }}
    }}
    button:active, a.button:active {{
      background: var(--color-blue-1200);
    }}
    button.secondary {{
      color: var(--color-blue-900);
      background: #fff;
      border: 1px solid currentcolor;
    }}
    @media (hover: hover) {{
      button.secondary:hover {{
        color: var(--color-blue-1000);
        background: var(--color-blue-200);
      }}
    }}
    a.button[aria-disabled="true"] {{
      pointer-events: none;
      opacity: .45;
    }}
    output {{
      display: block;
      min-height: 22px;
      overflow-wrap: anywhere;
      font-family: "Noto Sans Mono", monospace;
      font-size: calc(14 / 16 * 1rem);
      line-height: 1.5;
    }}
    .error {{
      color: var(--color-error);
      font-weight: 600;
    }}
    @media (max-width: 47.999rem) {{
      .row {{
        grid-template-columns: 1fr;
      }}
      .json-row {{
        grid-template-columns: 1fr;
      }}
      h1 {{
        font-size: calc(32 / 16 * 1rem);
        line-height: 1.5;
        letter-spacing: 0.01em;
      }}
    }}
    @media (forced-colors: active) {{
      h2::before {{
        background-color: CanvasText;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="page-header">
      <h1>YouTube 字幕焼き込みプロキシ</h1>
      <p class="lead">YouTube の手動字幕を焼き込んだ MP4 / HLS を準備し、VRChat などの動画プレーヤーで使いやすい URL を発行します。</p>
      <div class="utility-links">
        <a href="https://github.com/Usuiensan/youtube-subtitle-mp4-proxy" target="_blank" rel="noopener">GitHub リポジトリを開く ↗</a>
      </div>
    </header>

    <section class="section" aria-labelledby="usageTitle">
      <h2 id="usageTitle">使い方</h2>
      <ol class="usage">
        <li><strong>1. 準備キーを用意</strong>Discord の <code>/webui-key</code> で一時キーを発行し、Video タブの「準備キー」に貼り付けます。</li>
        <li><strong>2. 通知を許可</strong>長い動画では準備に時間がかかるため、必要なら <span lang="en">Enable Notifications</span> を押します。</li>
        <li><strong>3. 動画を指定</strong>YouTube URL、字幕言語、出力形式を選びます。出力形式の初期値は MP4 です。</li>
        <li><strong>4. Prepare を実行</strong>準備完了後に表示される「準備済みURL」を動画プレーヤーへ設定します。</li>
      </ol>
    </section>

    <section class="section" aria-labelledby="toolsTitle">
      <h2 id="toolsTitle">ツール</h2>
      <div class="tabs" role="tablist" aria-label="Tool">
        <button type="button" id="videoTab" role="tab" aria-controls="converter" aria-selected="true">動画準備</button>
        <button type="button" id="jsonTab" role="tab" aria-controls="jsonExporter" aria-selected="false">JSON 書き出し</button>
      </div>
    <form id="converter" class="tool" aria-labelledby="videoTab">
      <label>
        YouTube URL
        <input id="youtubeUrl" name="youtubeUrl" type="url" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" required>
      </label>
      <div class="row">
        <label>
          字幕言語
          <input id="lang" name="lang" value="" maxlength="12" autocomplete="off">
        </label>
        <label>
          出力形式
          <select id="mode" name="mode">
            <option value="youtube">MP4</option>
            <option value="youtube-hls">HLS playlist</option>
          </select>
        </label>
      </div>
      <label>
        準備キー
        <input id="prepareToken" name="prepareToken" type="password" autocomplete="current-password" placeholder="DISCORD_PREPARE_TOKEN">
      </label>
      <div id="prepareOptions" class="prepare-options" hidden>
        <label>
          Source subtitle
          <select id="subtitleSource" name="subtitleSource"></select>
        </label>
        <label>
          Translation
          <select id="translationEngine" name="translationEngine">
            <option value="local_llm">LLM</option>
            <option value="google_cloud">Google</option>
          </select>
        </label>
      </div>
      <div class="actions">
        <button type="button" id="prepareButton">Prepare</button>
        <button type="button" id="notifyButton" class="secondary">Enable Notifications</button>
      </div>
      <output id="prepareStatus"></output>
      <output id="message" class="error"></output>
      <label>
        準備済みURL
        <output id="result"></output>
      </label>
    </form>
    <form id="jsonExporter" class="tool" aria-labelledby="jsonTab" hidden>
      <label>
        Channel or Playlist URLs
        <textarea id="sourceUrl" name="sourceUrl" placeholder="https://www.youtube.com/@channel&#10;https://www.youtube.com/playlist?list=..." autocomplete="off"></textarea>
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
          URL
          <select id="jsonUrlMode" name="jsonUrlMode">
            <option value="original">Original</option>
            <option value="mp4">MP4</option>
            <option value="hls">HLS</option>
          </select>
        </label>
        <label>
          Lang
          <input id="jsonLang" name="jsonLang" value="" maxlength="12" autocomplete="off">
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
    </section>
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
    const prepareToken = document.getElementById("prepareToken");
    const prepareButton = document.getElementById("prepareButton");
    const notifyButton = document.getElementById("notifyButton");
    const prepareStatus = document.getElementById("prepareStatus");
    const prepareOptions = document.getElementById("prepareOptions");
    const subtitleSource = document.getElementById("subtitleSource");
    const translationEngine = document.getElementById("translationEngine");
    const sourceUrl = document.getElementById("sourceUrl");
    const sourceType = document.getElementById("sourceType");
    const playerMode = document.getElementById("playerMode");
    const jsonUrlMode = document.getElementById("jsonUrlMode");
    const jsonLang = document.getElementById("jsonLang");
    const maxItems = document.getElementById("maxItems");
    const playlistName = document.getElementById("playlistName");
    const jsonResult = document.getElementById("jsonResult");
    const jsonMessage = document.getElementById("jsonMessage");
    const downloadJsonLink = document.getElementById("downloadJsonLink");
    const jsonCopyButton = document.getElementById("jsonCopyButton");

    lang.value = defaultLang;
    jsonLang.value = defaultLang;
    prepareToken.value = localStorage.getItem("youtubeProxyPrepareToken") || "";

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
        return;
      }}
      if (!/^[A-Za-z0-9_-]{{11}}$/.test(videoId)) {{
        result.textContent = "";
        message.textContent = "Invalid YouTube URL";
        return;
      }}
      if (!/^[A-Za-z0-9_-]{{2,12}}$/.test(language)) {{
        result.textContent = "";
        message.textContent = "Invalid language";
        return;
      }}
      const url = `${{location.origin}}/${{mode.value}}/${{videoId}}/${{language}}`;
      result.textContent = url;
      message.textContent = "";
    }}

    function prepareMode() {{
      return mode.value === "youtube-hls" ? "hls" : "mp4";
    }}

    function authHeaders() {{
      const token = prepareToken.value.trim();
      if (token) localStorage.setItem("youtubeProxyPrepareToken", token);
      return {{
        "Accept": "application/json",
        "Authorization": `Bearer ${{token}}`
      }};
    }}

    function publicUrl(url) {{
      if (!url) return "";
      try {{
        const parsed = new URL(url);
        if (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost") {{
          parsed.protocol = location.protocol;
          parsed.host = location.host;
        }}
        return parsed.toString();
      }} catch {{
        return url;
      }}
    }}

    function etaText(body) {{
      const parts = [];
      if (typeof body.eta_seconds === "number" && body.eta_seconds > 0) {{
        const seconds = Math.max(1, Math.round(body.eta_seconds));
        const minutes = Math.floor(seconds / 60);
        const rest = seconds % 60;
        parts.push(minutes ? `予想${{minutes}}分${{rest}}秒` : `予想${{rest}}秒`);
      }}
      if (typeof body.estimated_ready_at === "number" && body.estimated_ready_at > 0) {{
        parts.push(`終了予想 ${{new Date(body.estimated_ready_at * 1000).toLocaleTimeString()}}`);
      }}
      return parts.length ? parts.join(" / ") : "終了予想を計算中";
    }}

    function prepareMessage(body) {{
      if (body.status === "ready") {{
        return `準備できました。\\n${{publicUrl(body.url)}}`;
      }}
      if (body.status === "failed") {{
        return `準備に失敗しました。\\n${{body.error || "unknown error"}}`;
      }}
      if (body.counts) {{
        return `${{body.mode?.toUpperCase() || "MP4"}}を一括準備しています。${{etaText(body)}}\\nready ${{body.counts.ready}}/${{body.counts.total}} / running ${{body.counts.running}} / queued ${{body.counts.queued}} / failed ${{body.counts.failed}}`;
      }}
      return `${{body.mode?.toUpperCase() || "MP4"}}を準備しています。${{etaText(body)}}`;
    }}

    function notify(title, body) {{
      if (!("Notification" in window) || Notification.permission !== "granted") return;
      new Notification(title, {{ body }});
    }}

    async function requestNotifications() {{
      if (!("Notification" in window)) {{
        prepareStatus.textContent = "このブラウザは通知に対応していません。";
        return;
      }}
      const permission = await Notification.requestPermission();
      prepareStatus.textContent = permission === "granted" ? "通知を有効にしました。" : "通知は許可されませんでした。";
    }}

    async function apiFetch(url, options = {{}}) {{
      const response = await fetch(url, {{
        ...options,
        headers: {{
          ...authHeaders(),
          ...(options.headers || {{}})
        }}
      }});
      const body = await response.json().catch(() => ({{}}));
      if (!response.ok) {{
        throw new Error(body.detail || body.error || `HTTP ${{response.status}}`);
      }}
      return body;
    }}

    async function pollPrepare(statusUrl) {{
      let latest = null;
      for (let i = 0; i < 720; i++) {{
        await new Promise((resolve) => setTimeout(resolve, i === 0 ? 2000 : 10000));
        const parsed = new URL(statusUrl, location.origin);
        const body = await apiFetch(parsed.pathname + parsed.search);
        latest = body;
        prepareStatus.textContent = prepareMessage(body);
        if (body.status === "ready") {{
          const url = publicUrl(body.url);
          result.textContent = url;
          notify("YouTube準備完了", url);
          return;
        }}
        if (body.status === "failed") {{
          notify("YouTube準備失敗", body.error || "unknown error");
          return;
        }}
      }}
      notify("YouTube準備確認タイムアウト", latest ? prepareMessage(latest) : "status polling timeout");
    }}

    async function loadSubtitleChoices(videoId, language, selectedMode) {{
      const params = new URLSearchParams({{ mode: selectedMode }});
      const body = await apiFetch(`/prepare/youtube/${{videoId}}/${{language}}/subtitles?${{params.toString()}}`);
      if (!body.requires_choice) return false;
      subtitleSource.innerHTML = "";
      for (const candidate of body.candidates || []) {{
        const option = document.createElement("option");
        option.value = candidate.language;
        option.textContent = `${{candidate.language}} / ${{candidate.name || candidate.name_en || candidate.language}}`;
        subtitleSource.appendChild(option);
      }}
      prepareOptions.hidden = false;
      prepareStatus.textContent = "日本語字幕が見つかりませんでした。翻訳元字幕と翻訳方式を選んで、もう一度 Prepare を押してください。";
      return true;
    }}

    async function prepareCurrentVideo() {{
      update();
      const videoId = extractVideoId(input.value);
      const language = (lang.value || defaultLang).trim();
      const token = prepareToken.value.trim();
      const selectedMode = prepareMode();
      if (!token) {{
        prepareStatus.textContent = "Prepare token を入力してください。";
        return;
      }}
      if (!/^[A-Za-z0-9_-]{{11}}$/.test(videoId) || !/^[A-Za-z0-9_-]{{2,12}}$/.test(language)) {{
        prepareStatus.textContent = "YouTube URLと言語を確認してください。";
        return;
      }}
      prepareButton.disabled = true;
      try {{
        let path = `/prepare/youtube/${{videoId}}/${{language}}`;
        if (language === "ja" && prepareOptions.hidden) {{
          const needsChoice = await loadSubtitleChoices(videoId, language, selectedMode);
          if (needsChoice) return;
        }}
        if (!prepareOptions.hidden && subtitleSource.value) {{
          path += `/${{encodeURIComponent(subtitleSource.value)}}/${{encodeURIComponent(translationEngine.value)}}`;
        }}
        const params = new URLSearchParams({{ mode: selectedMode }});
        const body = await apiFetch(`${{path}}?${{params.toString()}}`, {{ method: "POST" }});
        prepareStatus.textContent = prepareMessage(body);
        if (body.status === "ready") {{
          const url = publicUrl(body.url);
          result.textContent = url;
          notify("YouTube準備完了", url);
          return;
        }}
        if (body.status_url) {{
          await pollPrepare(body.status_url);
        }}
      }} catch (error) {{
        prepareStatus.textContent = `準備APIエラー: ${{error.message}}`;
      }} finally {{
        prepareButton.disabled = false;
      }}
    }}

    function updateJson() {{
      const values = sourceUrl.value.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);
      const selectedType = sourceType.value;
      const count = Number.parseInt(maxItems.value, 10);
      const modeValue = playerMode.value;
      const urlModeValue = jsonUrlMode.value;
      const language = (jsonLang.value || defaultLang).trim();
      if (values.length === 0) {{
        jsonResult.textContent = "";
        jsonMessage.textContent = "";
        downloadJsonLink.removeAttribute("href");
        downloadJsonLink.setAttribute("aria-disabled", "true");
        return;
      }}
      const resolvedTypes = values.map((value) => selectedType === "auto" ? detectJsonSource(value) : selectedType);
      if (resolvedTypes.some((type) => !type)) {{
        jsonResult.textContent = "";
        jsonMessage.textContent = "Invalid channel or playlist URL in list";
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
      if (!/^[A-Za-z0-9_-]{{2,12}}$/.test(language)) {{
        jsonResult.textContent = "";
        jsonMessage.textContent = "Invalid language";
        downloadJsonLink.removeAttribute("href");
        downloadJsonLink.setAttribute("aria-disabled", "true");
        return;
      }}
      const params = new URLSearchParams();
      params.set("mode", modeValue);
      params.set("maxItems", String(count));
      params.set("urlMode", urlModeValue);
      params.set("lang", language);
      if (playlistName.value.trim()) params.set("name", playlistName.value.trim());
      let path;
      if (values.length === 1) {{
        const type = resolvedTypes[0];
        params.set(type === "playlist" ? "list" : "channel", values[0]);
        path = `/yamaplayer/${{type}}`;
      }} else {{
        params.set("sourceType", selectedType);
        params.set("sources", values.join("\\n"));
        path = "/yamaplayer/batch";
      }}
      const url = `${{location.origin}}${{path}}?${{params.toString()}}`;
      jsonResult.textContent = url;
      jsonMessage.textContent = "";
      downloadJsonLink.href = url;
      downloadJsonLink.setAttribute("aria-disabled", "false");
    }}

    videoTab.addEventListener("click", () => selectTool("video"));
    jsonTab.addEventListener("click", () => selectTool("json"));
    function resetPrepareChoices() {{
      prepareOptions.hidden = true;
      subtitleSource.innerHTML = "";
    }}
    input.addEventListener("input", () => {{ resetPrepareChoices(); update(); }});
    lang.addEventListener("input", () => {{ resetPrepareChoices(); update(); }});
    mode.addEventListener("change", () => {{ resetPrepareChoices(); update(); }});
    prepareToken.addEventListener("input", () => {{
      localStorage.setItem("youtubeProxyPrepareToken", prepareToken.value.trim());
    }});
    form.addEventListener("submit", (event) => {{
      event.preventDefault();
      prepareCurrentVideo();
    }});
    prepareButton.addEventListener("click", prepareCurrentVideo);
    notifyButton.addEventListener("click", requestNotifications);
    sourceUrl.addEventListener("input", updateJson);
    sourceType.addEventListener("change", updateJson);
    playerMode.addEventListener("change", updateJson);
    jsonUrlMode.addEventListener("change", updateJson);
    jsonLang.addEventListener("input", updateJson);
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
    request: Request,
    list_id_or_url: str = Query(alias="list"),
    name: str | None = None,
    mode: int = 0,
    max_items: int = Query(default=500, alias="maxItems"),
    url_mode: str = Query(default="original", alias="urlMode"),
    lang: str | None = None,
) -> Response:
    normalized_mode = normalize_yamaplayer_mode(mode)
    normalized_max_items = normalize_max_items(max_items)
    normalized_url_mode = normalize_yamaplayer_url_mode(url_mode)
    lang = lang or settings.default_lang
    validate_lang(lang)
    playlist = await build_yamaplayer_playlist(
        "playlist",
        list_id_or_url,
        normalized_mode,
        normalized_max_items,
        normalized_url_mode,
        lang,
        str(request.base_url).rstrip("/"),
        name,
    )
    return yamaplayer_export_response([playlist], playlist["name"])


@app.get("/yamaplayer/channel")
async def yamaplayer_channel(
    request: Request,
    channel: str,
    name: str | None = None,
    mode: int = 0,
    max_items: int = Query(default=500, alias="maxItems"),
    url_mode: str = Query(default="original", alias="urlMode"),
    lang: str | None = None,
) -> Response:
    normalized_mode = normalize_yamaplayer_mode(mode)
    normalized_max_items = normalize_max_items(max_items)
    normalized_url_mode = normalize_yamaplayer_url_mode(url_mode)
    lang = lang or settings.default_lang
    validate_lang(lang)
    playlist = await build_yamaplayer_playlist(
        "channel",
        channel,
        normalized_mode,
        normalized_max_items,
        normalized_url_mode,
        lang,
        str(request.base_url).rstrip("/"),
        name,
    )
    return yamaplayer_export_response([playlist], playlist["name"])


@app.get("/yamaplayer/batch")
async def yamaplayer_batch(
    request: Request,
    sources: str,
    source_type: str = Query(default="auto", alias="sourceType"),
    name: str | None = None,
    mode: int = 0,
    max_items: int = Query(default=500, alias="maxItems"),
    url_mode: str = Query(default="original", alias="urlMode"),
    lang: str | None = None,
) -> Response:
    if source_type not in {"auto", "playlist", "channel"}:
        raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, or channel")
    normalized_mode = normalize_yamaplayer_mode(mode)
    normalized_max_items = normalize_max_items(max_items)
    normalized_url_mode = normalize_yamaplayer_url_mode(url_mode)
    lang = lang or settings.default_lang
    validate_lang(lang)
    base_url = str(request.base_url).rstrip("/")
    playlists = [
        await build_yamaplayer_playlist(
            source_type,
            source,
            normalized_mode,
            normalized_max_items,
            normalized_url_mode,
            lang,
            base_url,
        )
        for source in split_yamaplayer_sources(sources)
    ]
    filename = name or ("yamaplayer" if len(playlists) != 1 else playlists[0]["name"])
    return yamaplayer_export_response(playlists, filename)


@app.get("/youtube/{video_id}")
@app.get("/youtube/{video_id}/{lang}")
@app.get("/youtube/{video_id}/{lang}/{source_lang}/{translation_engine}")
async def youtube(
    video_id: str,
    request: Request,
    lang: str | None = None,
    source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Response:
    lang = lang or settings.default_lang
    validate_input(video_id, lang)
    normalized_engine = None
    if source_lang is not None or translation_engine is not None:
        if source_lang is None or translation_engine is None:
            raise HTTPException(status_code=400, detail="source language and translation engine must both be specified")
        normalized_engine = validate_translation_variant(source_lang, translation_engine)
    if source_lang is None and normalized_engine is None:
        key = default_serving_key(video_id, lang, "mp4")
    else:
        key = cache_key(video_id, lang, source_lang, normalized_engine)
    path = hot_output_path(key)
    if path is not None:
        return mp4_response(request, path)
    path = archived_output_path(key)
    if path is not None:
        return mp4_response(request, path)
    await cleanup_expired_cache_async()
    raise HTTPException(status_code=404, detail="MP4 is not prepared")


@app.get("/youtube-hls/{video_id}")
@app.get("/youtube-hls/{video_id}/{lang}")
@app.get("/youtube-hls/{video_id}/{lang}/{source_lang}/{translation_engine}")
async def youtube_hls(
    video_id: str,
    request: Request,
    lang: str | None = None,
    source_lang: str | None = None,
    translation_engine: str | None = None,
) -> Response:
    lang = lang or settings.default_lang
    validate_input(video_id, lang)
    normalized_engine = None
    if source_lang is not None or translation_engine is not None:
        if source_lang is None or translation_engine is None:
            raise HTTPException(status_code=400, detail="source language and translation engine must both be specified")
        normalized_engine = validate_translation_variant(source_lang, translation_engine)
    if source_lang is None and normalized_engine is None:
        key = default_serving_key(video_id, lang, "hls")
    else:
        key = cache_key(video_id, lang, source_lang, normalized_engine)
    playlist = hot_hls_playlist_path(key)
    if playlist is not None:
        return hls_playlist_response(request, key, playlist)
    await cleanup_expired_cache_async()
    raise HTTPException(status_code=404, detail="HLS is not prepared")


@app.post("/prepare/youtube/{video_id}/{lang}")
@app.post("/prepare/youtube/{video_id}/{lang}/{path_source_lang}/{path_translation_engine}")
async def prepare_youtube(
    video_id: str,
    lang: str,
    request: Request,
    path_source_lang: str | None = None,
    path_translation_engine: str | None = None,
    mode: str = Query("mp4"),
    discord_user_id: str | None = Query(None, alias="discordUserId"),
    subtitle_source_lang: str | None = Query(None, alias="subtitleSourceLang"),
    translation_engine: str | None = Query(None, alias="translationEngine"),
) -> JSONResponse:
    require_prepare_auth(request)
    validate_input(video_id, lang)
    if path_source_lang or path_translation_engine:
        if subtitle_source_lang or translation_engine:
            raise HTTPException(status_code=400, detail="Specify translation variant in path or query, not both")
        if path_source_lang is None or path_translation_engine is None:
            raise HTTPException(status_code=400, detail="source language and translation engine must both be specified")
        subtitle_source_lang = path_source_lang
        translation_engine = path_translation_engine
    discord_user_id = validate_discord_user_id(discord_user_id)
    if mode not in {"mp4", "hls"}:
        raise HTTPException(status_code=400, detail="mode must be mp4 or hls")
    await cleanup_expired_cache_async()
    status_code, body = await enqueue_prepare_job(
        request,
        video_id,
        lang,
        mode,
        discord_user_id,
        subtitle_source_lang=subtitle_source_lang,
        translation_engine=translation_engine,
    )
    return JSONResponse(body, status_code=status_code)


@app.get("/prepare/youtube/{video_id}/{lang}/subtitles")
async def prepare_youtube_subtitles(
    video_id: str,
    lang: str,
    request: Request,
    mode: str = Query("mp4"),
) -> JSONResponse:
    require_prepare_auth(request)
    validate_input(video_id, lang)
    if mode not in {"mp4", "hls"}:
        raise HTTPException(status_code=400, detail="mode must be mp4 or hls")
    if prepare_ready_path(video_id, lang, mode):
        return JSONResponse(
            {
                "video_id": video_id,
                "requested_language": lang,
                "requires_choice": False,
                "prepared": True,
            }
        )
    info = await fetch_video_info(video_id)
    assert_duration_allowed(info)
    return JSONResponse(subtitle_choice_body(info, lang))


@app.get("/prepare/jobs/{job_id}")
async def prepare_job_status(job_id: str, request: Request) -> JSONResponse:
    require_prepare_auth(request)
    job = _prepare_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Prepare job not found")
    return JSONResponse(job_response_body(job_id, job, request))


@app.post("/prepare/eta/reset")
async def reset_prepare_eta(request: Request) -> JSONResponse:
    require_prepare_auth(request, allow_temp_key=False)
    metrics_manager.reset()
    return JSONResponse({"message": "予想時間の学習データをリセットしました。"})


@app.post("/prepare/youtube-batch/{lang}")
async def prepare_youtube_batch(
    lang: str,
    request: Request,
    source: str = Query(...),
    source_type: str = Query("auto", alias="sourceType"),
    mode: str = Query("mp4"),
    max_items: int = Query(5000, alias="maxItems"),
    discord_user_id: str | None = Query(None, alias="discordUserId"),
) -> JSONResponse:
    require_prepare_auth(request)
    if not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid language code")
    if mode not in {"mp4", "hls"}:
        raise HTTPException(status_code=400, detail="mode must be mp4 or hls")
    if source_type not in {"auto", "playlist", "channel"}:
        raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, or channel")
    max_items = normalize_max_items(max_items)
    discord_user_id = validate_discord_user_id(discord_user_id)
    await cleanup_expired_cache_async()
    status_code, body = await enqueue_prepare_batch(
        request,
        source,
        source_type,
        lang,
        mode,
        max_items,
        discord_user_id,
    )
    return JSONResponse(body, status_code=status_code)


@app.get("/prepare/batches/{batch_id}")
async def prepare_batch_status(batch_id: str, request: Request) -> JSONResponse:
    require_prepare_auth(request)
    batch = _prepare_batches.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Prepare batch not found")
    return JSONResponse(batch_response_body(batch_id, batch, request, include_items=True))


@app.post("/prepare/youtube/clear-all")
async def clear_all_youtube(
    request: Request,
) -> JSONResponse:
    require_prepare_auth(request, allow_temp_key=False)

    # Cancel all active conversion tasks
    async with _inflight_lock:
        for key, task in list(_inflight.items()):
            if not task.done():
                task.cancel()
        _inflight.clear()
        for key, task in list(_hls_inflight.items()):
            if not task.done():
                task.cancel()
        _hls_inflight.clear()

    # Cancel/remove all queued/running prepare jobs
    async with _prepare_lock:
        _prepare_jobs.clear()
        _prepare_by_key.clear()
        _prepare_batches.clear()

    deleted_count = 0
    dirs_to_clean = []

    if settings.cache_hot_dir.exists():
        for child in settings.cache_hot_dir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                dirs_to_clean.append(child)

    if settings.cache_archive_dir and settings.cache_archive_dir.exists():
        for child in settings.cache_archive_dir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                dirs_to_clean.append(child)

    for base_dir in dirs_to_clean:
        # 1. Delete output.mp4
        out_mp4 = base_dir / "output.mp4"
        if out_mp4.exists():
            try:
                out_mp4.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"Failed to delete {out_mp4}: {e}", flush=True)

        # 2. Delete meta.json
        meta = base_dir / "meta.json"
        if meta.exists():
            try:
                meta.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"Failed to delete {meta}: {e}", flush=True)

        # 3. Delete hls directory
        hls = base_dir / "hls"
        if hls.exists() and hls.is_dir():
            try:
                shutil.rmtree(hls, ignore_errors=True)
                deleted_count += 1
            except Exception as e:
                print(f"Failed to delete {hls}: {e}", flush=True)

        # 4. Clean source directory
        source = base_dir / "source"
        if source.exists() and source.is_dir():
            # Delete translation.json
            translation_meta = source / "translation.json"
            if translation_meta.exists():
                try:
                    translation_meta.unlink()
                    deleted_count += 1
                except Exception as e:
                    print(f"Failed to delete {translation_meta}: {e}", flush=True)

            # Delete translated subtitles
            try:
                for file in source.iterdir():
                    if file.is_file() and ".translated." in file.name:
                        file.unlink()
                        deleted_count += 1
            except Exception as e:
                print(f"Failed to clear translated subtitles in {source}: {e}", flush=True)

    return JSONResponse({
        "message": f"すべての動画の初期化が完了しました。計 {deleted_count} 個のファイル/ディレクトリを削除しました。"
    })


@app.post("/prepare/youtube/{video_id}/{lang}/clear")
@app.post("/prepare/youtube/{video_id}/{lang}/{path_source_lang}/{path_translation_engine}/clear")
async def clear_youtube(
    video_id: str,
    lang: str,
    request: Request,
    path_source_lang: str | None = None,
    path_translation_engine: str | None = None,
) -> JSONResponse:
    require_prepare_auth(request, allow_temp_key=False)
    validate_input(video_id, lang)
    normalized_engine = None
    if path_source_lang or path_translation_engine:
        if path_source_lang is None or path_translation_engine is None:
            raise HTTPException(status_code=400, detail="source language and translation engine must both be specified")
        normalized_engine = validate_translation_variant(path_source_lang, path_translation_engine)

    key = cache_key(video_id, lang, path_source_lang, normalized_engine)

    # Cancel any active conversion tasks for this key
    async with _inflight_lock:
        task = _inflight.pop(key, None)
        if task and not task.done():
            task.cancel()
        hls_task = _hls_inflight.pop(key, None)
        if hls_task and not hls_task.done():
            hls_task.cancel()

    # Cancel/remove any queued/running prepare jobs for this key
    async with _prepare_lock:
        for mode in ("mp4", "hls"):
            job_key = prepare_key(video_id, lang, mode, path_source_lang, normalized_engine)
            job_id = _prepare_by_key.pop(job_key, None)
            if job_id:
                _prepare_jobs.pop(job_id, None)

    deleted_files = []

    # Directories to clean: hot directory and archive directory
    dirs_to_clean = []
    dirs_to_clean.append(entry_dir(key))
    archive_dir = archive_entry_dir(key)
    if archive_dir:
        dirs_to_clean.append(archive_dir)

    for base_dir in dirs_to_clean:
        if not base_dir.exists():
            continue

        # 1. Delete re-encoded video output.mp4
        out_mp4 = base_dir / "output.mp4"
        if out_mp4.exists():
            try:
                out_mp4.unlink()
                deleted_files.append(f"{base_dir.name}/output.mp4")
            except Exception as e:
                print(f"Failed to delete {out_mp4}: {e}", flush=True)

        # 2. Delete meta.json
        meta = base_dir / "meta.json"
        if meta.exists():
            try:
                meta.unlink()
                deleted_files.append(f"{base_dir.name}/meta.json")
            except Exception as e:
                print(f"Failed to delete {meta}: {e}", flush=True)

        # 3. Delete hls directory
        hls = base_dir / "hls"
        if hls.exists() and hls.is_dir():
            try:
                shutil.rmtree(hls, ignore_errors=True)
                deleted_files.append(f"{base_dir.name}/hls")
            except Exception as e:
                print(f"Failed to delete {hls}: {e}", flush=True)

        # 4. Clean source folder: remove translated subtitles & translation.json
        source = base_dir / "source"
        if source.exists() and source.is_dir():
            # Delete translation.json
            translation_meta = source / "translation.json"
            if translation_meta.exists():
                try:
                    translation_meta.unlink()
                    deleted_files.append(f"{base_dir.name}/source/translation.json")
                except Exception as e:
                    print(f"Failed to delete {translation_meta}: {e}", flush=True)

            # Delete translated subtitles (*.translated.*)
            try:
                for file in source.iterdir():
                    if file.is_file() and ".translated." in file.name:
                        file.unlink()
                        deleted_files.append(f"{base_dir.name}/source/{file.name}")
            except Exception as e:
                print(f"Failed to clear translated subtitles in {source}: {e}", flush=True)

    if deleted_files:
        msg = f"初期化が完了しました。削除されたファイル: {', '.join(deleted_files)}"
    else:
        msg = "初期化対象のファイルはありませんでした（ソースファイルは保持されています）。"

    return JSONResponse({"message": msg})


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
