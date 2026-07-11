from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
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
LANG_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
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
    prepare_job_concurrency = max(1, int(os.getenv("PREPARE_JOB_CONCURRENCY", "3")))
    prepare_job_max_attempts = max(1, int(os.getenv("PREPARE_JOB_MAX_ATTEMPTS", "3")))
    prepare_job_retry_base_seconds = max(0.0, float(os.getenv("PREPARE_JOB_RETRY_BASE_SECONDS", "15")))
    default_lang = os.getenv("DEFAULT_LANG", "ja")
    max_duration_seconds = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
    max_height = int(os.getenv("MAX_HEIGHT", "720"))
    cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    job_timeout_seconds = int(os.getenv("JOB_TIMEOUT_SECONDS", "7200"))
    subtitle_font = os.getenv("SUBTITLE_FONT", "Noto Sans JP")
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
    ytdlp_min_interval_seconds = max(0.0, float(os.getenv("YTDLP_MIN_INTERVAL_SECONDS", "8")))
    ytdlp_concurrency = max(1, int(os.getenv("YTDLP_CONCURRENCY", "1")))
    youtube_data_api_key = os.getenv("YOUTUBE_DATA_API_KEY")
    discord_prepare_token = os.getenv("DISCORD_PREPARE_TOKEN")
    webui_temp_key_secret = os.getenv("WEBUI_TEMP_KEY_SECRET", os.getenv("DISCORD_PREPARE_TOKEN", ""))
    youtube_proxy_base_url = os.getenv("YOUTUBE_PROXY_BASE_URL", "").rstrip("/")
    translation_enabled = os.getenv("TRANSLATION_ENABLED", "1") != "0"
    translation_source_langs = os.getenv("TRANSLATION_SOURCE_LANGS", "en,ko,zh-Hans,zh-Hant,zh,zh-CN,zh-TW")
    local_llm_engine = os.getenv("LOCAL_LLM_ENGINE", "openai_compatible")
    local_llm_model = os.getenv("LOCAL_LLM_MODEL", "qwen3:4b-instruct")
    translation_default_profile = os.getenv("TRANSLATION_DEFAULT_PROFILE", os.getenv("TRANSLATION_PROVIDER", "google_cloud")).strip().lower()
    local_llm_profile_models = {
        "qwen3_4b_instruct": os.getenv("LOCAL_LLM_MODEL_QWEN3_4B_INSTRUCT", os.getenv("REMOTE_LLM_MODEL", "qwen3:4b-instruct")).strip(),
        "qwen3_8b": os.getenv("LOCAL_LLM_MODEL_QWEN3_8B", "qwen3:8b").strip(),
        "aya_expanse_8b": os.getenv("LOCAL_LLM_MODEL_AYA_EXPANSE_8B", "aya-expanse:8b").strip(),
        "gemini_2_5_flash": os.getenv("LOCAL_LLM_MODEL_GEMINI_2_5_FLASH", "gemini-2.5-flash").strip(),
        # Legacy aliases kept for compatibility with older settings.
        "local_llm": os.getenv("LOCAL_LLM_MODEL", "qwen3:4b-instruct").strip(),
        "remote_llm": os.getenv("REMOTE_LLM_MODEL", os.getenv("LOCAL_LLM_MODEL", "qwen3:4b-instruct")).strip(),
    }
    local_llm_profile_labels = {
        "qwen3_4b_instruct": "Qwen 3 4B Instruct",
        "qwen3_8b": "Qwen 3 8B",
        "aya_expanse_8b": "Aya Expanse 8B",
        "gemini_2_5_flash": "Gemini Flash",
        "local_llm": os.getenv("LOCAL_LLM_LABEL", "Default LLM"),
        "remote_llm": os.getenv("REMOTE_LLM_LABEL", "Remote LLM"),
    }
    local_llm_timeout_seconds = int(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "300"))
    remote_llm_endpoint = os.getenv("REMOTE_LLM_ENDPOINT", os.getenv("LOCAL_LLM_ENDPOINT", "")).strip()
    remote_llm_health_url = os.getenv("REMOTE_LLM_HEALTH_URL", "").strip()
    remote_llm_api_key = os.getenv("REMOTE_LLM_API_KEY", os.getenv("LOCAL_LLM_API_KEY", "")).strip()
    remote_llm_model = os.getenv("REMOTE_LLM_MODEL", os.getenv("LOCAL_LLM_MODEL", "qwen3:4b-instruct")).strip()
    remote_llm_health_timeout_seconds = float(os.getenv("REMOTE_LLM_HEALTH_TIMEOUT_SECONDS", "2.5"))
    local_llm_target_window_seconds = int(os.getenv("LOCAL_LLM_TARGET_WINDOW_SECONDS", "120"))
    local_llm_target_max_events = int(os.getenv("LOCAL_LLM_TARGET_MAX_EVENTS", "10"))
    local_llm_context_before_seconds = int(os.getenv("LOCAL_LLM_CONTEXT_BEFORE_SECONDS", os.getenv("LOCAL_LLM_CONTEXT_SECONDS", "120")))
    local_llm_context_before_max_events = int(os.getenv("LOCAL_LLM_CONTEXT_BEFORE_MAX_EVENTS", "25"))
    local_llm_context_after_seconds = int(os.getenv("LOCAL_LLM_CONTEXT_AFTER_SECONDS", os.getenv("LOCAL_LLM_CONTEXT_SECONDS", "120")))
    local_llm_context_after_max_events = int(os.getenv("LOCAL_LLM_CONTEXT_AFTER_MAX_EVENTS", "25"))
    translation_fallback_engine = os.getenv("TRANSLATION_FALLBACK_ENGINE", "")
    translation_topic = os.getenv("TRANSLATION_TOPIC", "")
    translation_glossary = os.getenv("TRANSLATION_GLOSSARY", "")
    google_cloud_project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_billing_mode = os.getenv("GEMINI_BILLING_MODE", "free_tier").strip().lower()
    gemini_flash_input_price_per_million = float(os.getenv("GEMINI_FLASH_INPUT_PRICE_PER_MILLION", "0.30"))
    gemini_flash_output_price_per_million = float(os.getenv("GEMINI_FLASH_OUTPUT_PRICE_PER_MILLION", "2.50"))
    usd_to_jpy_rate = float(os.getenv("USD_TO_JPY_RATE", "160.0"))
    translation_provider = os.getenv("TRANSLATION_PROVIDER", "qwen3_4b_instruct").strip().lower()
    translation_failure_dir = Path(
        os.getenv("TRANSLATION_FAILURE_DIR", str(cache_hot_dir / ".translation-attempts"))
    )
    translation_audit_dir = Path(
        os.getenv("TRANSLATION_AUDIT_DIR", str(cache_hot_dir / ".translation-audit"))
    )
    system_metrics_enabled = os.getenv("SYSTEM_METRICS_ENABLED", "1") != "0"
    system_metrics_interval_seconds = float(os.getenv("SYSTEM_METRICS_INTERVAL_SECONDS", "5"))
    system_metrics_history_seconds = int(os.getenv("SYSTEM_METRICS_HISTORY_SECONDS", "86400"))
    system_metrics_file = Path(os.getenv("SYSTEM_METRICS_FILE", str(cache_hot_dir / "system-metrics.jsonl")))


settings = Settings()
_system_metrics: deque[dict] = deque(maxlen=max(1, int(settings.system_metrics_history_seconds / max(settings.system_metrics_interval_seconds, 1)) + 60))
_last_cpu_times: tuple[int, int] | None = None
_metrics_task: asyncio.Task | None = None


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


import ctypes

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]

_last_win_cpu_times: tuple[int, int] | None = None

def read_win_cpu_percent() -> float | None:
    global _last_win_cpu_times
    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    try:
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            idle_val = (idle.dwHighDateTime << 32) + idle.dwLowDateTime
            kernel_val = (kernel.dwHighDateTime << 32) + kernel.dwLowDateTime
            user_val = (user.dwHighDateTime << 32) + user.dwLowDateTime
            total_val = kernel_val + user_val
            
            if _last_win_cpu_times is None:
                _last_win_cpu_times = (idle_val, total_val)
                return 0.0
            
            prev_idle, prev_total = _last_win_cpu_times
            _last_win_cpu_times = (idle_val, total_val)
            
            idle_delta = idle_val - prev_idle
            total_delta = total_val - prev_total
            if total_delta <= 0:
                return 0.0
            return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)
    except Exception:
        pass
    return None

def read_win_memory_metrics() -> dict:
    result = {"used_percent": None, "used_bytes": None, "total_bytes": None}
    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total = stat.ullTotalPhys
            used = total - stat.ullAvailPhys
            result.update({
                "used_percent": float(stat.dwMemoryLoad),
                "used_bytes": float(used),
                "total_bytes": float(total),
            })
    except Exception:
        pass
    return result

def read_proc_cpu_percent() -> float | None:
    if sys.platform == "win32":
        return read_win_cpu_percent()
    global _last_cpu_times
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        if first[0] != "cpu":
            return None
        idle = int(first[4]) + int(first[5])
        total = sum(int(x) for x in first[1:8])
    except Exception:
        return None
    if _last_cpu_times is None:
        _last_cpu_times = (idle, total)
        return 0.0
    previous = _last_cpu_times
    _last_cpu_times = (idle, total)
    previous_idle, previous_total = previous
    total_delta = total - previous_total
    idle_delta = idle - previous_idle
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def read_memory_metrics() -> dict:
    if sys.platform == "win32":
        return read_win_memory_metrics()
    result = {"used_percent": None, "used_bytes": None, "total_bytes": None}
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            values[key] = int(raw_value.strip().split()[0]) * 1024
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if total and available is not None:
            used = total - available
            result.update(
                {
                    "used_percent": round(used / total * 100.0, 1),
                    "used_bytes": used,
                    "total_bytes": total,
                }
            )
    except Exception:
        pass
    return result


def read_gpu_metrics() -> dict | None:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    query = ",".join(
        [
            "utilization.gpu",
            "utilization.memory",
            "memory.used",
            "memory.total",
            "temperature.gpu",
            "power.draw",
            "encoder.stats.sessionCount",
            "encoder.stats.averageFps",
            "encoder.stats.averageLatency",
        ]
    )
    try:
        completed = subprocess.run(
            [
                nvidia_smi,
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    line = completed.stdout.strip().splitlines()[0]
    parts = [part.strip() for part in line.split(",")]
    keys = [
        "gpu_percent",
        "memory_percent",
        "memory_used_mib",
        "memory_total_mib",
        "temperature_c",
        "power_w",
        "encoder_sessions",
        "encoder_fps",
        "encoder_latency_ms",
    ]
    result: dict[str, int | float | None] = {}
    for key, value in zip(keys, parts):
        try:
            number = float(value)
        except ValueError:
            result[key] = None
            continue
        result[key] = int(number) if number.is_integer() else round(number, 1)
    return result


def read_disk_metrics() -> dict:
    def usage(path: Path | None) -> dict | None:
        if path is None:
            return None
        try:
            path.mkdir(parents=True, exist_ok=True)
            item = shutil.disk_usage(path)
            used = item.total - item.free
            return {
                "path": str(path),
                "used_percent": round(used / item.total * 100.0, 1),
                "free_bytes": item.free,
                "total_bytes": item.total,
            }
        except Exception:
            return None

    return {
        "hot": usage(settings.cache_hot_dir),
        "archive": usage(settings.cache_archive_dir),
    }


def current_job_summaries() -> list[dict]:
    jobs = []
    for job_id, job in _prepare_jobs.items():
        if job.get("status") not in {"queued", "running"}:
            continue
        jobs.append(
            {
                "job_id": job_id,
                "status": job.get("status"),
                "video_id": job.get("video_id"),
                "title": job.get("title"),
                "mode": job.get("mode"),
                "eta_seconds": job.get("eta_seconds"),
                "estimated_ready_at": job.get("estimated_ready_at"),
                "progress": job.get("progress"),
                "queue_counts": queue_counts_for_job(job_id),
            }
        )
    return jobs


def collect_system_metrics() -> dict:
    return {
        "timestamp": int(time.time()),
        "cpu": {"used_percent": read_proc_cpu_percent()},
        "memory": read_memory_metrics(),
        "gpu": read_gpu_metrics(),
        "disk": read_disk_metrics(),
        "jobs": current_job_summaries(),
    }


def append_system_metric(sample: dict) -> None:
    _system_metrics.append(sample)
    try:
        settings.system_metrics_file.parent.mkdir(parents=True, exist_ok=True)
        with settings.system_metrics_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(sample, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception as error:
        print(f"Failed to write system metrics: {error}", file=sys.stderr, flush=True)


def translation_attempt_dir(work_dir: Path, payload: dict) -> Path:
    video_id = str(payload.get("video_id") or "unknown")
    lang = str(payload.get("target_language") or payload.get("lang") or "unknown")
    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    return settings.translation_failure_dir / f"{stamp}-{video_id}-{lang}-{uuid.uuid4().hex[:8]}"


def archive_translation_failure(
    attempt_dir: Path,
    payload: dict,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    exception: str = "",
) -> None:
    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if stdout:
            (attempt_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        if stderr:
            (attempt_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        meta = {
            "created_at": int(time.time()),
            "returncode": returncode,
            "exception": exception,
        }
        (attempt_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        print(f"Failed to archive translation failure: {error}", file=sys.stderr, flush=True)


def translation_audit_path(payload: dict) -> Path:
    video_id = str(payload.get("video_id") or "unknown")
    lang = str(payload.get("target_language") or payload.get("lang") or "unknown")
    model = str(payload.get("model_name") or "unknown").replace("/", "_").replace(":", "_")
    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    return settings.translation_audit_dir / f"{stamp}-{video_id}-{lang}-{model}-{uuid.uuid4().hex[:8]}.jsonl"


def load_system_metrics_history() -> None:
    if not settings.system_metrics_file.exists():
        return
    cutoff = int(time.time()) - settings.system_metrics_history_seconds
    try:
        for line in settings.system_metrics_file.read_text(encoding="utf-8").splitlines()[-_system_metrics.maxlen:]:
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(sample.get("timestamp") or 0) >= cutoff:
                _system_metrics.append(sample)
    except Exception:
        pass


async def system_metrics_loop() -> None:
    while True:
        append_system_metric(collect_system_metrics())
        await asyncio.sleep(max(1.0, settings.system_metrics_interval_seconds))


app = FastAPI(title="YouTube subtitle burned MP4 proxy")


@app.on_event("startup")
async def start_system_metrics() -> None:
    global _metrics_task
    if not settings.system_metrics_enabled:
        return
    load_system_metrics_history()
    if _metrics_task is None or _metrics_task.done():
        _metrics_task = asyncio.create_task(system_metrics_loop())

_global_encode_lock = asyncio.Semaphore(1)
_inflight_lock = asyncio.Lock()
_inflight: dict[str, asyncio.Task[Path]] = {}
_hls_inflight: dict[str, asyncio.Task[Path]] = {}
_prepare_lock = asyncio.Lock()
_prepare_job_semaphore = asyncio.Semaphore(settings.prepare_job_concurrency)
_ytdlp_semaphore = asyncio.Semaphore(settings.ytdlp_concurrency)
_ytdlp_rate_lock = asyncio.Lock()
_last_ytdlp_started_at = 0.0
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


def estimate_download_seconds(duration: float, has_sources: bool) -> int:
    if has_sources:
        return 0
    dl_speed = metrics_manager.get_avg("download_speed", 3 * 1024 * 1024)
    est_size = duration * 1.5 * 1024 * 1024 / 8
    return max(0, int(est_size / max(dl_speed, 1.0)))


def estimate_translate_seconds(duration: float, subtitle_events_count: int | None = None) -> int:
    tr_speed = metrics_manager.get_avg("translate_speed", 1.0)
    events = subtitle_events_count if subtitle_events_count is not None else (duration / 2.0)
    return max(0, int(events / max(tr_speed, 1.0)))


def estimate_encode_seconds(duration: float) -> int:
    enc_ratio = metrics_manager.get_avg("encode_speed_ratio", 3.0)
    return max(0, int(duration / max(enc_ratio, 0.1)))


def job_needs_translation(job: dict) -> bool:
    return bool(job.get("subtitle_source_lang") and job.get("translation_engine"))


def job_has_sources(job: dict) -> bool:
    cache_id = cache_key(
        str(job.get("video_id") or ""),
        str(job.get("lang") or ""),
        job.get("subtitle_source_lang"),
        job.get("translation_engine"),
    )
    return check_existing_sources(cache_id) is not None


def job_stage_estimates(job: dict) -> dict[str, int]:
    duration = float(job.get("duration") or 0.0)
    if duration <= 0:
        return {"download": 60, "translate": 120 if job_needs_translation(job) else 0, "encode": 180}
    return {
        "download": estimate_download_seconds(duration, job_has_sources(job)),
        "translate": estimate_translate_seconds(duration) if job_needs_translation(job) else 0,
        "encode": estimate_encode_seconds(duration),
    }


def job_sort_key(job_id: str, job: dict) -> tuple[int, str]:
    return (int(job.get("created_at") or 0), job_id)


def active_jobs_ahead(job_id: str) -> list[tuple[str, dict]]:
    current = _prepare_jobs.get(job_id)
    if not current:
        return []
    current_key = job_sort_key(job_id, current)
    items: list[tuple[str, dict]] = []
    for other_id, other_job in _prepare_jobs.items():
        if other_id == job_id or other_job.get("status") not in {"queued", "running"}:
            continue
        if job_sort_key(other_id, other_job) < current_key:
            items.append((other_id, other_job))
    return items


def job_remaining_pipeline_seconds(job_id: str) -> int | None:
    job = _prepare_jobs.get(job_id)
    if not job:
        return None
    stages = job_stage_estimates(job)
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    phase = str(progress.get("phase") or "")
    phase_eta = progress.get("eta_seconds")
    if isinstance(phase_eta, (int, float)) and phase_eta >= 0:
        current_phase_remaining = int(phase_eta)
    else:
        current_phase_remaining = None

    if job.get("status") == "queued" or not phase:
        return stages["download"] + stages["translate"] + stages["encode"] + 10
    if phase == "download":
        return (current_phase_remaining if current_phase_remaining is not None else stages["download"]) + stages["translate"] + stages["encode"] + 10
    if phase == "translate":
        return (current_phase_remaining if current_phase_remaining is not None else stages["translate"]) + stages["encode"] + 10
    if phase in {"encode", "hls"}:
        return (current_phase_remaining if current_phase_remaining is not None else stages["encode"]) + 5
    return stages["download"] + stages["translate"] + stages["encode"] + 10


def estimate_job_completion_eta(job_id: str) -> int | None:
    own_remaining = job_remaining_pipeline_seconds(job_id)
    if own_remaining is None:
        return None
    queue_delay = 0
    for ahead_id, _ahead_job in active_jobs_ahead(job_id):
        ahead_remaining = job_remaining_pipeline_seconds(ahead_id)
        if ahead_remaining is not None:
            queue_delay += ahead_remaining
    return max(0, own_remaining + queue_delay)


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
    overall_eta = estimate_job_completion_eta(job_id)
    if overall_eta is not None:
        _prepare_jobs[job_id]["eta_seconds"] = overall_eta
        _prepare_jobs[job_id]["estimated_ready_at"] = int(time.time()) + overall_eta


def queue_counts_for_job(job_id: str) -> dict[str, int]:
    counts = {"download": 0, "translate": 0, "encode": 0}
    phase_order = {"download": 0, "translate": 1, "encode": 2, "hls": 2}
    for current_id, job in active_jobs_ahead(job_id):
        needs_translation = job_needs_translation(job)
        progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
        phase = str(progress.get("phase") or "")
        if job.get("status") == "queued" or phase not in phase_order:
            counts["download"] += 1
            if needs_translation:
                counts["translate"] += 1
            counts["encode"] += 1
            continue
        phase_index = phase_order[phase]
        if phase_index < 1 and needs_translation:
            counts["translate"] += 1
        if phase_index < 2:
            counts["encode"] += 1
    return counts


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


def read_json_file(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def prepared_variant_from_meta(subtitle_meta: dict) -> tuple[str | None, str | None]:
    if not subtitle_meta.get("translated"):
        return None, None
    source_lang = subtitle_meta.get("source_language")
    engine = subtitle_meta.get("translation_engine_requested") or subtitle_meta.get("translation_engine")
    if isinstance(source_lang, str) and source_lang and isinstance(engine, str) and engine:
        return source_lang, normalize_translation_engine(engine)
    return None, None


def prepared_cache_entry_body(request: Request, key: str, base_dir: Path, storage: str) -> dict | None:
    source_meta = read_json_file(base_dir / "source.json")
    meta = read_json_file(base_dir / "meta.json")
    merged = {**meta, **source_meta}
    video_id = merged.get("video_id")
    lang = merged.get("lang")
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        return None
    if not isinstance(lang, str) or not LANG_RE.fullmatch(lang):
        return None

    subtitle_meta = read_subtitle_meta(key)
    source_lang, translation_engine = prepared_variant_from_meta(subtitle_meta)
    outputs = []
    if is_usable_file(base_dir / "output.mp4"):
        outputs.append(
            {
                "mode": "mp4",
                "url": prepared_media_url(request, video_id, lang, "mp4", source_lang, translation_engine),
            }
        )
    playlist = base_dir / "hls" / "index.m3u8"
    if is_usable_file(playlist) and "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8", errors="ignore"):
        outputs.append(
            {
                "mode": "hls",
                "url": prepared_media_url(request, video_id, lang, "hls", source_lang, translation_engine),
            }
        )
    if not outputs:
        return None

    return {
        "key": key,
        "storage": storage,
        "video_id": video_id,
        "title": merged.get("title") or video_id,
        "title_variants": merged.get("title_variants") if isinstance(merged.get("title_variants"), list) else [],
        "lang": lang,
        "source_url": merged.get("webpage_url") or youtube_watch_url(video_id),
        "subtitle": subtitle_meta,
        "outputs": outputs,
        "updated_at": int(cache_entry_newest_mtime(base_dir) or base_dir.stat().st_mtime),
    }


def list_prepared_cache_entries(request: Request) -> list[dict]:
    items: dict[str, dict] = {}
    for storage, root in (("hot", settings.cache_hot_dir), ("archive", settings.cache_archive_dir)):
        if root is None or not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            body = prepared_cache_entry_body(request, child.name, child, storage)
            if body is None:
                continue
            existing = items.get(child.name)
            if existing is None or existing.get("storage") != "hot":
                items[child.name] = body
    return sorted(items.values(), key=lambda item: int(item.get("updated_at") or 0), reverse=True)


def clear_rendered_outputs_only(key: str, mode: str) -> list[str]:
    deleted: list[str] = []
    for base_dir in (entry_dir(key), archive_entry_dir(key)):
        if base_dir is None or not base_dir.exists():
            continue
        if mode in {"mp4", "both"}:
            out_mp4 = base_dir / "output.mp4"
            if out_mp4.exists():
                out_mp4.unlink()
                deleted.append(f"{base_dir.name}/output.mp4")
        if mode in {"hls", "both"}:
            hls = base_dir / "hls"
            if hls.exists() and hls.is_dir():
                shutil.rmtree(hls, ignore_errors=True)
                deleted.append(f"{base_dir.name}/hls")
        meta = base_dir / "meta.json"
        if meta.exists():
            meta.unlink()
            deleted.append(f"{base_dir.name}/meta.json")
    return deleted


def reburn_variant_for(video_id: str, lang: str, mode: str) -> tuple[str, str | None, str | None]:
    key = default_serving_key(video_id, lang, mode)
    subtitle_meta = read_subtitle_meta(key)
    source_lang, translation_engine = prepared_variant_from_meta(subtitle_meta)
    return key, source_lang, translation_engine


def reusable_source_entries() -> list[dict]:
    items: dict[str, dict] = {}
    for storage, root in (("hot", settings.cache_hot_dir), ("archive", settings.cache_archive_dir)):
        if root is None or not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith(".") or child.name in items:
                continue
            existing = check_existing_source_video(child.name)
            if existing is None:
                continue
            _video_path, source_meta, _base_dir = existing
            video_id = source_meta.get("video_id")
            lang = source_meta.get("lang")
            if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
                continue
            if not isinstance(lang, str) or not LANG_RE.fullmatch(lang):
                continue
            subtitle_meta = source_meta.get("subtitle_meta") if isinstance(source_meta.get("subtitle_meta"), dict) else {}
            items[child.name] = {
                "key": child.name,
                "storage": storage,
                "video_id": video_id,
                "title": source_meta.get("title") or video_id,
                "lang": lang,
                "subtitle": subtitle_meta,
                "updated_at": int(cache_entry_newest_mtime(child) or child.stat().st_mtime),
            }
    return sorted(items.values(), key=lambda item: int(item.get("updated_at") or 0), reverse=True)


def extract_title_variants(info: dict) -> list[dict[str, str]]:
    variants: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_variant(language: str | None, title: Any) -> None:
        if not isinstance(title, str):
            return
        clean_title = title.strip()
        if not clean_title:
            return
        clean_language = (language or "").strip() or "default"
        item = (clean_language, clean_title)
        if item in seen:
            return
        seen.add(item)
        variants.append({"language": clean_language, "title": clean_title})

    add_variant(str(info.get("language") or info.get("original_language") or "default"), info.get("title"))
    add_variant("alt", info.get("alt_title"))

    localizations = info.get("localizations")
    if isinstance(localizations, dict):
        for language, localized in localizations.items():
            if isinstance(localized, dict):
                add_variant(str(language), localized.get("title"))
            else:
                add_variant(str(language), localized)

    translated_titles = info.get("translated_titles")
    if isinstance(translated_titles, dict):
        for language, title in translated_titles.items():
            add_variant(str(language), title)

    return variants


def write_meta(key: str, video_id: str, lang: str, info: dict, mode: str) -> None:
    meta_path(key).parent.mkdir(parents=True, exist_ok=True)
    meta_path(key).write_text(
        json.dumps(
            {
                "video_id": video_id,
                "lang": lang,
                "title": info.get("title"),
                "title_variants": extract_title_variants(info),
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
                "title_variants": extract_title_variants(info),
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


def active_prepare_cache_keys() -> set[str]:
    active: set[str] = set()
    for key in _inflight.keys():
        active.add(key)
    for key in _hls_inflight.keys():
        active.add(key)
    for job in _prepare_jobs.values():
        if job.get("status") not in {"queued", "running"}:
            continue
        video_id = job.get("video_id")
        lang = job.get("lang")
        if not isinstance(video_id, str) or not isinstance(lang, str):
            continue
        active.add(cache_key(video_id, lang, job.get("subtitle_source_lang"), job.get("translation_engine")))
    return active


def archive_all_hot_entries(active_keys: set[str]) -> dict:
    if settings.cache_archive_dir is None:
        raise HTTPException(status_code=400, detail="CACHE_ARCHIVE_DIR is not configured")
    settings.cache_hot_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    skipped = 0
    failed = 0
    freed_bytes = 0
    errors: list[dict[str, str]] = []
    for child in sorted(settings.cache_hot_dir.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        key = child.name
        if key in active_keys:
            skipped += 1
            continue
        try:
            size = dir_size_bytes(child)
            if archive_cache_entry(key):
                moved += 1
                freed_bytes += size
            else:
                skipped += 1
        except Exception as error:
            failed += 1
            errors.append({"key": key, "error": str(error)[-300:]})
    return {
        "moved": moved,
        "skipped": skipped,
        "failed": failed,
        "freed_bytes": freed_bytes,
        "errors": errors[:5],
    }


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
    return archived_playlist


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
    for profile_id in settings.local_llm_profile_models:
        if f"_{profile_id}_" in key:
            return (1, key)
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


def is_yt_dlp_command(args: list[str]) -> bool:
    if not args:
        return False
    executable = Path(args[0]).name.lower()
    return executable in {"yt-dlp", "yt-dlp.exe"} or executable.startswith("yt-dlp")


async def wait_for_ytdlp_rate_limit() -> None:
    global _last_ytdlp_started_at
    min_interval = settings.ytdlp_min_interval_seconds
    if min_interval <= 0:
        return
    async with _ytdlp_rate_lock:
        now = time.monotonic()
        wait_seconds = (_last_ytdlp_started_at + min_interval) - now
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        _last_ytdlp_started_at = time.monotonic()


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


def estimate_workspace_bytes(duration_seconds: float | int | None) -> int:
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        return 2 * 1024 * 1024 * 1024
    approx_source = int(duration_seconds * 1.5 * 1024 * 1024 / 8)
    return max(2 * 1024 * 1024 * 1024, int(approx_source * 2.5))


def reclaim_hot_space(target_free_bytes: int) -> dict[str, int]:
    settings.cache_hot_dir.mkdir(parents=True, exist_ok=True)
    if settings.cache_archive_dir is None:
        return {"moved": 0, "freed_bytes": 0, "scanned": 0}

    moved = 0
    freed_bytes = 0
    scanned = 0
    candidates: list[tuple[float, str]] = []
    for child in settings.cache_hot_dir.iterdir():
        if not child.is_dir() or child.name.startswith(".work-"):
            continue
        newest = cache_entry_newest_mtime(child)
        if newest == 0:
            continue
        candidates.append((newest, child.name))

    for _newest, key in sorted(candidates):
        scanned += 1
        if hot_free_bytes() >= target_free_bytes:
            break
        entry = entry_dir(key)
        if not entry.exists():
            continue
        size = dir_size_bytes(entry)
        if archive_cache_entry(key):
            moved += 1
            freed_bytes += size
    return {"moved": moved, "freed_bytes": freed_bytes, "scanned": scanned}


async def cleanup_expired_cache_async() -> None:
    async with _cleanup_lock:
        await asyncio.to_thread(cleanup_expired_cache)


async def ensure_prepare_workspace_capacity(required_bytes: int | None = None) -> None:
    if required_bytes is None or required_bytes <= 0:
        required_bytes = 0
    minimum_required = max(512 * 1024 * 1024, required_bytes)
    if minimum_required <= 0:
        return
    current_free = hot_free_bytes()
    if current_free >= minimum_required:
        if settings.cache_hot_min_free_bytes > 0 and current_free < settings.cache_hot_min_free_bytes:
            print(
                f"Hot cache free space is below CACHE_HOT_MIN_FREE_BYTES: {current_free} < {settings.cache_hot_min_free_bytes}",
                flush=True,
            )
        return

    async with _cleanup_lock:
        await asyncio.to_thread(cleanup_expired_cache)
        current_free = hot_free_bytes()
        if current_free >= minimum_required:
            if settings.cache_hot_min_free_bytes > 0 and current_free < settings.cache_hot_min_free_bytes:
                print(
                    f"Hot cache free space is below CACHE_HOT_MIN_FREE_BYTES: {current_free} < {settings.cache_hot_min_free_bytes}",
                    flush=True,
                )
            return
        if settings.cache_archive_dir is None:
            raise HTTPException(
                status_code=507,
                detail=(
                    "Insufficient hot cache space and CACHE_ARCHIVE_DIR is not configured"
                ),
            )
        await asyncio.to_thread(reclaim_hot_space, minimum_required)
        current_free = hot_free_bytes()
        if current_free >= minimum_required:
            if settings.cache_hot_min_free_bytes > 0 and current_free < settings.cache_hot_min_free_bytes:
                print(
                    f"Hot cache free space is below CACHE_HOT_MIN_FREE_BYTES: {current_free} < {settings.cache_hot_min_free_bytes}",
                    flush=True,
                )
            return
    raise HTTPException(
        status_code=507,
        detail=(
            "Insufficient hot cache space for a new prepare job. "
            "Archive completed videos or free disk space first."
        ),
    )


async def run_command(
    args: list[str],
    cwd: Path | None = None,
    raise_http: bool = True,
) -> str:
    if is_yt_dlp_command(args):
        async with _ytdlp_semaphore:
            await wait_for_ytdlp_rate_limit()
            return await run_command_unlimited(args, cwd=cwd, raise_http=raise_http)
    return await run_command_unlimited(args, cwd=cwd, raise_http=raise_http)


async def run_command_unlimited(
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
    info = json.loads(raw)
    await enrich_video_info_titles(info, video_id)
    return info


async def enrich_video_info_titles(info: dict, video_id: str) -> None:
    if not settings.youtube_data_api_key:
        return
    try:
        data = await youtube_api_get(
            "videos",
            {
                "part": "snippet,localizations",
                "id": video_id,
                "maxResults": 1,
            },
        )
    except Exception:
        return
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return
    item = items[0] if isinstance(items[0], dict) else {}
    snippet = item.get("snippet")
    if isinstance(snippet, dict):
        localized = snippet.get("localized")
        if isinstance(localized, dict) and isinstance(localized.get("title"), str):
            info.setdefault("translated_titles", {})
            info["translated_titles"]["default"] = localized["title"]
    localizations = item.get("localizations")
    if isinstance(localizations, dict):
        merged = dict(info.get("localizations") or {})
        for language, localized in localizations.items():
            if isinstance(localized, dict) and isinstance(localized.get("title"), str):
                merged[str(language)] = {"title": localized["title"]}
        if merged:
            info["localizations"] = merged


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
    if engine in {"llm", "remote", "remote_llm"}:
        return settings.translation_default_profile if settings.translation_default_profile in settings.local_llm_profile_models else "qwen3_4b_instruct"
    if engine in {"local", "local_llm", "openai_compatible"}:
        return "google_cloud"
    if engine in {"google", "google_cloud", "google_translate"}:
        return "google_cloud"
    if engine in settings.local_llm_profile_models:
        return engine
    return "google_cloud"


def translation_profile_options() -> list[dict]:
    default_profile = normalize_translation_engine(settings.translation_default_profile)
    profiles = [
        "qwen3_4b_instruct",
        "qwen3_8b",
        "aya_expanse_8b",
        "gemini_2_5_flash",
    ]
    options = []
    for profile_id in profiles:
        model = settings.local_llm_profile_models.get(profile_id)
        label = settings.local_llm_profile_labels.get(profile_id, profile_id)
        kind = "gemini_api" if profile_id == "gemini_2_5_flash" else "openai_compatible"
        options.append(
            {
                "value": profile_id,
                "label": label,
                "model": model,
                "default": default_profile == profile_id,
                "kind": kind,
            }
        )
    return [
        *options,
        {
            "value": "google_cloud",
            "label": "Google翻訳",
            "model": None,
            "default": default_profile == "google_cloud",
            "kind": "cloud",
        },
    ]


async def remote_llm_available() -> tuple[bool, str | None]:
    if not settings.remote_llm_endpoint:
        return False, "REMOTE_LLM_ENDPOINT is not configured"
    health_url = settings.remote_llm_health_url
    if not health_url:
        parsed = urllib.parse.urlparse(settings.remote_llm_endpoint)
        if parsed.path.rstrip("/").endswith("/v1/chat/completions"):
            health_path = "/v1/models"
        else:
            base_path = parsed.path.rsplit("/", 1)[0].rstrip("/")
            health_path = f"{base_path}/models" if base_path else "/v1/models"
        health_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, health_path, "", "", ""))

    def check() -> tuple[bool, str | None]:
        headers = {"Accept": "application/json"}
        if settings.remote_llm_api_key:
            headers["Authorization"] = f"Bearer {settings.remote_llm_api_key}"
        request = urllib.request.Request(health_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=settings.remote_llm_health_timeout_seconds) as response:
                if 200 <= response.status < 300:
                    return True, None
                return False, f"remote LLM health returned HTTP {response.status}"
        except Exception as error:
            return False, str(error)

    return await asyncio.to_thread(check)


def chat_prompt_from_messages(messages: list[dict[str, Any]], system_prompt: str | None = None) -> str:
    lines: list[str] = []
    if system_prompt:
        lines.append(f"system: {system_prompt.strip()}")
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role not in {"user", "assistant", "system"}:
            role = "user"
        lines.append(f"{role}: {content}")
    lines.append("assistant:")
    return "\n".join(lines)


def chat_completion_with_provider(
    *,
    provider_name: str,
    model_name: str,
    prompt: str,
    timeout_seconds: int,
    api_key: str,
    endpoint: str,
    temperature: float = 0.4,
    max_tokens: int = 1024,
) -> tuple[str, dict[str, int]]:
    if provider_name == "gemini_api":
        gemini_endpoint = os.getenv(
            "GEMINI_API_ENDPOINT",
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        ).format(model=urllib.parse.quote(model_name, safe=""))
        url = f"{gemini_endpoint}?key={urllib.parse.quote(api_key, safe='')}"
        body = json.dumps(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    }
                ],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("gemini api returned no candidates")
        content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict) or not parts[0].get("text"):
            raise RuntimeError("gemini api returned no text part")
        reply = str(parts[0]["text"]).strip()
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        return reply, {
            "input_tokens": int(usage.get("promptTokenCount") or 0),
            "output_tokens": int(usage.get("candidatesTokenCount") or 0),
            "total_tokens": int(usage.get("totalTokenCount") or 0),
        }

    body = json.dumps(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith('"') and content.endswith('"'):
        content = content[1:-1].strip()
    elif content.startswith("'") and content.endswith("'"):
        content = content[1:-1].strip()
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return content, {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


async def run_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    profile_id = normalize_translation_engine(str(payload.get("profile") or payload.get("translation_engine") or settings.translation_default_profile))
    selected = translation_settings(profile_id)
    prompt = chat_prompt_from_messages(
        payload.get("messages") if isinstance(payload.get("messages"), list) else [],
        str(payload.get("system_prompt") or "").strip() or None,
    )
    timeout_seconds = int(payload.get("timeout_seconds") or settings.local_llm_timeout_seconds)
    max_tokens = int(payload.get("max_tokens") or os.getenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "1024"))
    temperature = float(payload.get("temperature") or os.getenv("LOCAL_LLM_TEMPERATURE", "0.4"))

    def call() -> tuple[str, dict[str, int]]:
        return chat_completion_with_provider(
            provider_name=selected.provider_name,
            model_name=selected.model_name,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            api_key=settings.remote_llm_api_key if selected.provider_name == "openai_compatible" else settings.gemini_api_key,
            endpoint=settings.remote_llm_endpoint,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    reply, usage = await asyncio.to_thread(call)
    return {
        "profile": profile_id,
        "provider": selected.provider_name,
        "model": selected.model_name,
        "reply": reply,
        "usage": usage,
    }


def translation_settings(profile_id: str = "local_llm") -> TranslationSettings:
    normalized = normalize_translation_engine(profile_id)
    model_name = settings.local_llm_profile_models.get(normalized, "")
    provider_name = "google_cloud"
    if normalized in settings.local_llm_profile_models and normalized != "gemini_2_5_flash":
        provider_name = "openai_compatible"
    elif normalized == "gemini_2_5_flash":
        provider_name = "gemini_api"
    return TranslationSettings(
        enabled=settings.translation_enabled,
        target_window_seconds=settings.local_llm_target_window_seconds,
        target_max_events=settings.local_llm_target_max_events,
        context_before_seconds=settings.local_llm_context_before_seconds,
        context_before_max_events=settings.local_llm_context_before_max_events,
        context_after_seconds=settings.local_llm_context_after_seconds,
        context_after_max_events=settings.local_llm_context_after_max_events,
        model_name=model_name,
        engine=normalized,
        fallback_engine=settings.translation_fallback_engine,
        glossary=settings.translation_glossary,
        topic=settings.translation_topic,
        google_project=settings.google_cloud_project,
        provider_name=provider_name,
    )


def chat_profile_options() -> list[dict]:
    return [option for option in translation_profile_options() if option.get("value") != "google_cloud"]


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
            "translation_engines": translation_profile_options(),
        }
    )
    if requested_lang != "ja" or not settings.translation_enabled:
        body["error"] = f"No subtitle found for language: {requested_lang}"
    elif not candidates:
        body["error"] = "No translatable manual subtitle found"
    return body


def restrict_translation_engines(body: dict, *, llm_available: bool, llm_error: str | None) -> dict:
    engines = body.get("translation_engines")
    if not isinstance(engines, list):
        return body
    if llm_available:
        body["translation_engines"] = engines
        body["llm_available"] = True
        return body
    body["translation_engines"] = [
        engine for engine in engines
        if isinstance(engine, dict) and engine.get("value") == "google_cloud"
    ]
    body["llm_available"] = False
    body["llm_unavailable_reason"] = llm_error or "remote LLM is unavailable"
    body["requires_google_confirmation"] = True
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


def subtitle_force_style(font_name: str | None = None) -> str:
    resolved_font = font_name or settings.subtitle_font
    return ",".join(
        [
            f"FontName={resolved_font}",
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
        service = subtitle_translation_service_label(subtitle_meta)
        if service:
            return f"{line1}\n{line2}\n{service}"
        return f"{line1}\n{line2}"
    elif requested_lang:
        req_ja = get_lang_name_ja(requested_lang)
        req_en = get_lang_name_en(requested_lang)
        line1 = f"[字幕]{req_ja}"
        line2 = f"[subs]{req_en}"
        return f"{line1}\n{line2}"
    return "[subs]"


def subtitle_translation_service_label(subtitle_meta: dict) -> str:
    engine = str(subtitle_meta.get("translation_engine") or "").strip()
    requested = str(subtitle_meta.get("translation_engine_requested") or "").strip()
    model = str(subtitle_meta.get("translation_model") or "").strip()
    fallback = bool(subtitle_meta.get("translation_fallback_used"))
    if fallback:
        return "[translation] Google Cloud fallback"
    if engine == "google_cloud":
        return "[translation] Google Cloud"
    if model:
        label = settings.local_llm_profile_labels.get(engine) or settings.local_llm_profile_labels.get(requested) or "LLM"
        return f"[translation] {label} {model}"
    if requested in settings.local_llm_profile_models or engine in settings.local_llm_profile_models:
        profile_id = requested if requested in settings.local_llm_profile_models else engine
        return f"[translation] {settings.local_llm_profile_labels.get(profile_id, profile_id)}"
    if engine:
        return f"[translation] {engine}"
    return ""


def gemini_overage_estimate(input_tokens: int, output_tokens: int) -> tuple[float, float]:
    usd = (
        (input_tokens / 1_000_000.0) * settings.gemini_flash_input_price_per_million
        + (output_tokens / 1_000_000.0) * settings.gemini_flash_output_price_per_million
    )
    jpy = usd * settings.usd_to_jpy_rate
    return usd, jpy


def enrich_translation_metadata(metadata: dict) -> dict:
    engine = str(metadata.get("translation_engine") or "")
    if engine != "gemini_2_5_flash":
        return metadata
    input_tokens = int(metadata.get("translation_input_tokens") or 0)
    output_tokens = int(metadata.get("translation_output_tokens") or 0)
    usd, jpy = gemini_overage_estimate(input_tokens, output_tokens)
    return {
        **metadata,
        "translation_provider_label": "Gemini Flash",
        "translation_billing_class": "Gemini API Free Tier" if settings.gemini_billing_mode == "free_tier" else "Gemini API Paid Tier",
        "translation_api_cost_jpy": 0.0 if settings.gemini_billing_mode == "free_tier" else jpy,
        "translation_overage_estimate_usd": usd,
        "translation_overage_estimate_jpy": jpy,
    }


def find_japanese_font_spec() -> tuple[str | None, str | None]:
    candidates = [
        ("/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf", "Noto Sans JP"),
        ("/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf", "Noto Sans JP"),
        ("/usr/local/share/fonts/truetype/form-udp-gothic/FORMUDPGothic-Regular.ttf", "FORM UDPGothic"),
        ("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", "Japanese Gothic"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK JP"),
        ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK JP"),
        ("/usr/share/fonts/truetype/ipaexfont/ipaexg.ttf", "IPAexGothic"),
        ("/usr/share/fonts/truetype/vlgothic/VL-PGothic-Regular.ttf", "VL PGothic"),
    ]
    for path, name in candidates:
        if os.path.exists(path):
            return path, name
    return None, None


def ffmpeg_subtitle_arg(path: Path, subtitle_meta: dict | None = None) -> str:
    value = path.as_posix()
    font_file, font_name = find_japanese_font_spec()
    subtitle_font_name = font_name or settings.subtitle_font
    filter_str = (
        f"subtitles='{escape_filter_value(value)}':"
        f"force_style='{escape_filter_value(subtitle_force_style(subtitle_font_name))}'"
    )
    if subtitle_meta:
        label = get_subtitle_overlay_label(subtitle_meta)
        lines = label.split("\n")

        if font_file:
            font_opt = f":fontfile='{font_file}'"
        else:
            font_opt = f":font='{subtitle_font_name}'" if subtitle_font_name else ""

        drawtext_filters = []
        for i, line in enumerate(lines):
            escaped_line = (
                line.replace("\\", "\\\\")
                .replace("'", "'\\\\''")
                .replace(":", "\\:")
                .replace(",", "\\,")
            )
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


async def run_translation_worker(payload: dict) -> dict:
    work_dir = Path(payload["_work_dir"])
    clean_payload = {key: value for key, value in payload.items() if key != "_work_dir"}
    attempt_dir = translation_attempt_dir(work_dir, clean_payload)
    audit_path = translation_audit_path(clean_payload)
    input_path = attempt_dir / "payload.json"
    output_path = attempt_dir / "response.json"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    clean_payload["_translation_audit_path"] = str(audit_path)
    input_path.write_text(json.dumps(clean_payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.local_llm_timeout_seconds,
        )
    except asyncio.TimeoutError as error:
        process.kill()
        stdout, stderr = await process.communicate()
        archive_translation_failure(
            attempt_dir,
            clean_payload,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exception="local llm translation timed out",
        )
        raise RuntimeError("local llm translation timed out") from error

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")
        archive_translation_failure(
            attempt_dir,
            clean_payload,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=message,
            returncode=process.returncode,
            exception=message[-1000:] or "local llm translation failed",
        )
        raise RuntimeError(message[-1000:] or "local llm translation failed")
    try:
        result = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as error:
        archive_translation_failure(
            attempt_dir,
            clean_payload,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=process.returncode,
            exception=str(error),
        )
        raise
    result["_translation_attempt_dir"] = str(attempt_dir)
    return result


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
    selected_settings = translation_settings(requested_engine)

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

    if selected_settings.provider_name == "openai_compatible":
        llm_available, llm_error = await remote_llm_available()
        if not llm_available:
            raise HTTPException(
                status_code=503,
                detail=f"Remote LLM is unavailable. Ask the user before using google_cloud. reason={llm_error}",
            )

    async def worker(payload: dict) -> dict:
        payload["_work_dir"] = str(work_dir)
        payload["llm_endpoint"] = settings.remote_llm_endpoint
        payload["llm_api_key"] = settings.remote_llm_api_key
        payload["llm_timeout_seconds"] = settings.local_llm_timeout_seconds
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
        
    metadata = enrich_translation_metadata({**selection, **result.metadata})
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


async def download_subtitle_only(
    video_id: str,
    lang: str,
    work_dir: Path,
    info: dict,
    job_id: str | None = None,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> tuple[Path, Path, dict]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    subtitle_selection = select_subtitle_language(
        info,
        lang,
        source_lang=subtitle_source_lang,
        translation_engine=translation_engine,
    )
    source_lang = subtitle_selection["source_language"]
    start_t = time.time()
    dl_args = yt_dlp_base_args() + [
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--sub-langs",
        source_lang,
        "--convert-subs",
        "srt",
        "--paths",
        str(work_dir),
        "-o",
        "%(id)s.%(ext)s",
        url,
    ]
    if job_id:
        await run_yt_dlp_with_progress(dl_args, job_id=job_id, cwd=work_dir)
    else:
        await run_command(dl_args, cwd=work_dir)
    original_subtitle = find_subtitle(work_dir, source_lang)
    metrics_manager.record_download(original_subtitle.stat().st_size, time.time() - start_t)
    subtitle, subtitle_meta = await translate_subtitle_if_needed(
        key=cache_key(video_id, lang, subtitle_source_lang, translation_engine),
        subtitle=original_subtitle,
        info=info,
        selection=subtitle_selection,
        work_dir=work_dir,
        job_id=job_id,
    )
    return original_subtitle, subtitle, subtitle_meta


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
        archive_meta_p = archive_dir / "source.json"
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


def check_existing_source_video(key: str) -> tuple[Path, dict, Path] | None:
    source_meta = get_cached_video_info(key)
    if not source_meta:
        return None
    for base_dir in (entry_dir(key), archive_entry_dir(key)):
        if not base_dir or not base_dir.exists():
            continue
        source_video_rel = source_meta.get("source_video")
        if not isinstance(source_video_rel, str) or not source_video_rel:
            continue
        video_path = base_dir / source_video_rel
        if video_path.exists() and video_path.stat().st_size > 0:
            return video_path, source_meta, base_dir
    return None


def cached_subtitle_path(key: str) -> Path | None:
    source_meta = get_cached_video_info(key)
    if not source_meta:
        return None
    for base_dir in (entry_dir(key), archive_entry_dir(key)):
        if not base_dir or not base_dir.exists():
            continue
        subtitle_rel = source_meta.get("subtitle")
        if not subtitle_rel:
            continue
        subtitle_path = base_dir / subtitle_rel
        if subtitle_path.exists() and subtitle_path.stat().st_size > 0:
            return subtitle_path
    return None


def cached_original_subtitle_path(key: str) -> Path | None:
    source_meta = get_cached_video_info(key)
    if not source_meta:
        return None
    subtitle_meta = source_meta.get("subtitle_meta") or {}
    source_lang = subtitle_meta.get("source_language")
    if not isinstance(source_lang, str) or not source_lang:
        return None
    for base_dir in (entry_dir(key), archive_entry_dir(key)):
        if not base_dir or not base_dir.exists():
            continue
        candidates = sorted((base_dir / "source").glob(f"subtitle.{source_lang}.original.*"))
        for subtitle_path in candidates:
            if subtitle_path.exists() and subtitle_path.stat().st_size > 0:
                return subtitle_path
    return None


def subtitle_events_from_path(subtitle_path: Path) -> list[dict]:
    events = []
    for item in load_srt(subtitle_path):
        events.append(
            {
                "id": str(item.index),
                "start": item.start.total_seconds(),
                "end": item.end.total_seconds(),
                "text": item.content,
            }
        )
    return events


def subtitle_events_for_key(key: str) -> list[dict]:
    subtitle_path = cached_subtitle_path(key)
    if not subtitle_path:
        raise HTTPException(status_code=404, detail="Subtitle is not prepared")
    return subtitle_events_from_path(subtitle_path)


def prepared_subtitle_bundle_for_key(key: str) -> dict:
    subtitle_meta = read_subtitle_meta(key)
    translated_path = cached_subtitle_path(key)
    original_path = cached_original_subtitle_path(key)
    translated_events = subtitle_events_from_path(translated_path) if translated_path else []
    if subtitle_meta.get("translated") and original_path and original_path != translated_path:
        source_events = subtitle_events_from_path(original_path)
    else:
        source_events = translated_events
    return {
        "key": key,
        "subtitle": subtitle_meta,
        "source_subtitle_path": str(original_path) if original_path else None,
        "translated_subtitle_path": str(translated_path) if translated_path else None,
        "source_events": source_events,
        "translated_events": translated_events,
    }


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
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
) -> tuple[Path, Path, dict]:
    existing = check_existing_sources(key)
    if existing and subtitle_source_lang:
        _saved_video, _original_subtitle, existing_subtitle_meta = existing
        existing_source_lang = existing_subtitle_meta.get("source_language")
        if normalize_lang(existing_source_lang) != normalize_lang(subtitle_source_lang):
            existing = None
    if existing:
        saved_video, original_subtitle, subtitle_meta = existing
        source_meta = get_cached_video_info(key) or {}
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

        if reuse_cached_subtitle:
            subtitle_rel = source_meta.get("subtitle")
            if isinstance(subtitle_rel, str) and subtitle_rel:
                cached_subtitle = hot_dir / subtitle_rel
                if cached_subtitle.exists() and cached_subtitle.stat().st_size > 0:
                    return saved_video, cached_subtitle, subtitle_meta

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

    if reuse_source_video:
        existing_video = check_existing_source_video(key)
        if existing_video:
            saved_video, _source_meta, base_dir = existing_video
            hot_dir = entry_dir(key)
            archive_dir = archive_entry_dir(key)
            if archive_dir and base_dir == archive_dir and not hot_dir.exists():
                shutil.copytree(archive_dir, hot_dir)
                saved_video = hot_dir / saved_video.relative_to(archive_dir)

            original_subtitle, subtitle, subtitle_meta = await download_subtitle_only(
                video_id,
                lang,
                work_dir,
                info,
                job_id=job_id,
                subtitle_source_lang=subtitle_source_lang,
                translation_engine=translation_engine,
            )
            source_dir(key).mkdir(parents=True, exist_ok=True)
            source_lang = subtitle_meta.get("source_language", lang)
            saved_original_subtitle = source_dir(key) / f"subtitle.{source_lang}.original{original_subtitle.suffix.lower()}"
            saved_subtitle = (
                source_dir(key) / f"subtitle.{lang}.translated{subtitle.suffix.lower()}"
                if subtitle_meta.get("translated")
                else source_dir(key) / f"subtitle.{lang}{subtitle.suffix.lower()}"
            )
            move_replace(original_subtitle, saved_original_subtitle)
            if subtitle != saved_original_subtitle:
                move_replace(subtitle, saved_subtitle)
            else:
                saved_subtitle = saved_original_subtitle
            if subtitle_meta.get("translated"):
                translation_meta_path(key).write_text(
                    json.dumps(subtitle_meta, ensure_ascii=True, indent=2),
                    encoding="utf-8",
                )
            write_source_meta(key, video_id, lang, info, saved_video, saved_subtitle, subtitle_meta)
            return saved_video, saved_subtitle, subtitle_meta

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
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
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
        try:
            info = await fetch_video_info(video_id)
            assert_duration_allowed(info)
            duration = float(info.get("duration") or 0.0)
            await ensure_prepare_workspace_capacity(estimate_workspace_bytes(duration))
            work_dir.mkdir(parents=True, exist_ok=True)
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
                reuse_cached_subtitle=reuse_cached_subtitle,
                reuse_source_video=reuse_source_video,
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
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
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
        try:
            info = await fetch_video_info(video_id)
            assert_duration_allowed(info)
            duration = float(info.get("duration") or 0.0)
            await ensure_prepare_workspace_capacity(estimate_workspace_bytes(duration))
            work_dir.mkdir(parents=True, exist_ok=True)
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
                reuse_cached_subtitle=reuse_cached_subtitle,
                reuse_source_video=reuse_source_video,
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
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
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
                    reuse_cached_subtitle=reuse_cached_subtitle,
                    reuse_source_video=reuse_source_video,
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
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
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
                    reuse_cached_subtitle=reuse_cached_subtitle,
                    reuse_source_video=reuse_source_video,
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
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
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
                    reuse_cached_subtitle=reuse_cached_subtitle,
                    reuse_source_video=reuse_source_video,
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
    if eta_seconds is None and job_id in _prepare_jobs:
        eta_seconds = estimate_job_completion_eta(job_id)
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


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def job_notification(job: dict) -> dict | None:
    mentions = job_mentions(job)
    if not mentions:
        return None
    prefix = " ".join(mentions)
    if job["status"] == "ready":
        return {
            "content": f"{prefix} 準備できました: {job['url']}\n元動画: {youtube_watch_url(job['video_id'])}",
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
    if job.get("attempt") is not None:
        body["attempt"] = job["attempt"]
    if job.get("max_attempts") is not None:
        body["max_attempts"] = job["max_attempts"]
    if job.get("last_error") is not None:
        body["last_error"] = job["last_error"]
    if job.get("title") is not None:
        body["title"] = job["title"]
    if job.get("title_variants") is not None:
        body["title_variants"] = job["title_variants"]
    if job.get("duration") is not None:
        body["duration"] = job["duration"]
    if job.get("subtitle") is not None:
        body["subtitle"] = job["subtitle"]
    if job.get("progress") is not None:
        body["progress"] = job["progress"]
    if job["status"] in {"queued", "running"}:
        body["queue_counts"] = queue_counts_for_job(job_id)
    if job["status"] in {"queued", "running"}:
        body["job_id"] = job_id
        body["status_url"] = prepare_status_url(request, job_id)
    if job.get("eta_seconds") is not None:
        body["eta_seconds"] = job["eta_seconds"]
    if job.get("estimated_ready_at") is not None:
        body["estimated_ready_at"] = job["estimated_ready_at"]
    if job.get("archived_immediately") is not None:
        body["archived_immediately"] = bool(job.get("archived_immediately"))
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
            if item.get("title_variants") is not None and body.get("title_variants") is None:
                body["title_variants"] = item["title_variants"]
            return body

    body = {
        "status": item.get("status", "unknown"),
        "video_id": item["video_id"],
        "lang": item["lang"],
        "mode": item["mode"],
    }
    if item.get("title") is not None:
        body["title"] = item["title"]
    if item.get("title_variants") is not None:
        body["title_variants"] = item["title_variants"]
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
    if pending_ready_at:
        latest_ready_at = int(max(pending_ready_at))
        body["estimated_ready_at"] = latest_ready_at
        body["eta_seconds"] = max(0, latest_ready_at - int(time.time()))
    elif pending_etas:
        max_eta = int(max(pending_etas))
        body["eta_seconds"] = max_eta
        body["estimated_ready_at"] = int(time.time()) + max_eta
    mentions = batch.get("mentions") or []
    if mentions:
        body["mentions"] = mentions
    failed_samples = [
        {
            "video_id": item.get("video_id"),
            "title": item.get("title"),
            "error": item.get("error"),
        }
        for item in item_bodies
        if item.get("status") == "failed" and item.get("error")
    ][:5]
    if failed_samples:
        body["failed_samples"] = failed_samples
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
        if failed_samples:
            content += "\n失敗例:"
            for sample in failed_samples[:3]:
                label = sample.get("title") or sample.get("video_id") or "unknown"
                error = sample.get("error") or "unknown error"
                content += f"\n- {label}: {error[:180]}"
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


def prepare_error_text(error: Exception) -> str:
    return str(getattr(error, "detail", None) or error)[-1000:]


def is_retryable_prepare_error(error: Exception) -> bool:
    if isinstance(error, HTTPException):
        return int(error.status_code) in {429, 500, 502, 503, 504}
    return isinstance(error, (asyncio.TimeoutError, TimeoutError, OSError, ConnectionError, json.JSONDecodeError))


async def run_prepare_job_once(
    job_id: str,
    video_id: str,
    lang: str,
    mode: str,
    url: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
    archive_immediately: bool = False,
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
) -> None:
    cache_id = cache_key(video_id, lang, subtitle_source_lang, translation_engine)
    if archived_ready_entry_exists(cache_id, mode):
        update_job_eta(job_id, estimate_archive_prepare_seconds(cache_id))
    else:
        cached_info = get_cached_video_info(cache_id)
        if cached_info and cached_info.get("duration"):
            info = cached_info
            _prepare_jobs[job_id]["title"] = info.get("title")
            _prepare_jobs[job_id]["title_variants"] = extract_title_variants(info)
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
            _prepare_jobs[job_id]["title_variants"] = extract_title_variants(info)
            _prepare_jobs[job_id]["duration"] = info.get("duration")
            eta = estimate_total_seconds(
                duration=float(info.get("duration")),
                has_sources=False,
                needs_translation=True # assume true by default if not cached
            )
            update_job_eta(job_id, eta)
    update_job_eta(job_id, None)
    if mode == "hls":
        await get_or_create_hls(
            video_id,
            lang,
            job_id=job_id,
            subtitle_source_lang=subtitle_source_lang,
            translation_engine=translation_engine,
            reuse_cached_subtitle=reuse_cached_subtitle,
            reuse_source_video=reuse_source_video,
        )
    else:
        await get_or_create_mp4(
            video_id,
            lang,
            job_id=job_id,
            subtitle_source_lang=subtitle_source_lang,
            translation_engine=translation_engine,
            reuse_cached_subtitle=reuse_cached_subtitle,
            reuse_source_video=reuse_source_video,
        )
    archived = False
    if archive_immediately:
        archived = archive_cache_entry(cache_id)
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
            "archived_immediately": archived,
            "last_error": None,
        }
    )


async def run_prepare_job(
    job_id: str,
    job_key: str,
    video_id: str,
    lang: str,
    mode: str,
    url: str,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
    archive_immediately: bool = False,
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
) -> None:
    try:
        async with _prepare_job_semaphore:
            max_attempts = settings.prepare_job_max_attempts
            for attempt in range(1, max_attempts + 1):
                now = int(time.time())
                _prepare_jobs[job_id].update(
                    {
                        "status": "running",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    }
                )
                try:
                    await run_prepare_job_once(
                        job_id,
                        video_id,
                        lang,
                        mode,
                        url,
                        subtitle_source_lang=subtitle_source_lang,
                        translation_engine=translation_engine,
                        archive_immediately=archive_immediately,
                        reuse_cached_subtitle=reuse_cached_subtitle,
                        reuse_source_video=reuse_source_video,
                    )
                    return
                except Exception as error:
                    error_text = prepare_error_text(error)
                    retryable = is_retryable_prepare_error(error)
                    is_last_attempt = attempt >= max_attempts
                    if (not retryable) or is_last_attempt:
                        raise
                    delay = settings.prepare_job_retry_base_seconds * (2 ** (attempt - 1))
                    _prepare_jobs[job_id].update(
                        {
                            "status": "queued",
                            "last_error": error_text,
                            "eta_seconds": int(delay),
                            "estimated_ready_at": now + int(delay),
                        }
                    )
                    await asyncio.sleep(delay)
    except Exception as error:
        now = int(time.time())
        _prepare_jobs[job_id].update(
            {
                "status": "failed",
                "error": prepare_error_text(error),
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
    archive_immediately: bool = False,
    reuse_cached_subtitle: bool = False,
    reuse_source_video: bool = False,
) -> tuple[int, dict]:
    if subtitle_source_lang:
        subtitle_source_lang = subtitle_source_lang.strip()
        if not subtitle_source_lang:
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
            "archived_immediately": False,
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
            "attempt": 0,
            "max_attempts": settings.prepare_job_max_attempts,
            "requesters": [],
            "subtitle_source_lang": subtitle_source_lang,
            "translation_engine": normalized_engine,
            "archive_immediately": archive_immediately,
            "reuse_cached_subtitle": reuse_cached_subtitle,
            "reuse_source_video": reuse_source_video,
        }
        if cached_info:
            if cached_info.get("title"):
                _prepare_jobs[job_id]["title"] = cached_info.get("title")
            title_variants = extract_title_variants(cached_info)
            if title_variants:
                _prepare_jobs[job_id]["title_variants"] = title_variants
            if cached_info.get("duration"):
                _prepare_jobs[job_id]["duration"] = cached_info.get("duration")
        add_job_requester(_prepare_jobs[job_id], discord_user_id)
        update_job_eta(job_id, None)
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
                archive_immediately=archive_immediately,
                reuse_cached_subtitle=reuse_cached_subtitle,
                reuse_source_video=reuse_source_video,
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


def extract_video_id_from_value(value: str) -> str | None:
    value = value.strip().strip("<>")
    if VIDEO_ID_RE.fullmatch(value):
        return value
    try:
        parsed = parse_youtube_url(value)
    except HTTPException:
        return None
    host = parsed.netloc.lower().replace("www.", "")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.fullmatch(candidate) else None
    query = urllib.parse.parse_qs(parsed.query)
    candidate = query.get("v", [""])[0]
    if VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("shorts", "embed", "live"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts) and VIDEO_ID_RE.fullmatch(parts[index + 1]):
                return parts[index + 1]
    return None


def manual_video_tracks(source: str, max_items: int) -> list[dict[str, str]]:
    tracks: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", source):
        video_id = extract_video_id_from_value(token)
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        tracks.append(
            {
                "video_id": video_id,
                "title": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
        if len(tracks) >= max_items:
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
    if source_type == "videos":
        tracks = manual_video_tracks(source, max_items)
        return yamaplayer_playlist_entry(
            name or "手動動画リスト",
            "manual",
            tracks,
            mode,
            url_mode,
            lang,
            base_url,
        )
    raise HTTPException(status_code=400, detail=f"Invalid source type for: {source}")


async def expand_prepare_source(source_type: str, source: str, max_items: int) -> tuple[str, str, str, list[dict[str, str]]]:
    if source_type == "auto":
        try:
            source_type = detect_yamaplayer_source_type(source)
        except HTTPException:
            source_type = "videos"
        if not source_type:
            source_type = "videos"
    if source_type == "playlist":
        playlist_id = extract_playlist_id(source)
        playlist_name = await fetch_playlist_title(playlist_id) or playlist_id
        tracks = await fetch_playlist_tracks(playlist_id, max_items)
        return source_type, playlist_id, playlist_name, tracks
    if source_type == "channel":
        uploads_playlist_id, channel_title = await fetch_channel_uploads_playlist(source)
        tracks = await fetch_playlist_tracks(uploads_playlist_id, max_items)
        return source_type, uploads_playlist_id, channel_title, tracks
    if source_type == "videos":
        tracks = manual_video_tracks(source, max_items)
        return source_type, "manual", "手動動画リスト", tracks
    raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, channel, or videos")


async def enqueue_prepare_batch(
    request: Request,
    source: str,
    source_type: str,
    lang: str,
    mode: str,
    max_items: int,
    discord_user_id: str | None,
    archive_immediately: bool = False,
) -> tuple[int, dict]:
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")
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
            archive_immediately=archive_immediately,
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


async def enqueue_reburn_batch(
    request: Request,
    source: str,
    source_type: str,
    lang: str,
    mode: str,
    max_items: int,
    discord_user_id: str | None,
    archive_immediately: bool = False,
) -> tuple[int, dict]:
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")
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
        variant_key, source_lang, translation_engine = reburn_variant_for(video_id, lang, mode)
        if check_existing_source_video(variant_key) is None:
            items.append(
                {
                    "video_id": video_id,
                    "title": track.get("title") or video_id,
                    "lang": lang,
                    "mode": mode,
                    "status": "failed",
                    "error": "No reusable source video found",
                    "status_code": 404,
                }
            )
            continue
        clear_rendered_outputs_only(variant_key, mode)
        status_code, body = await enqueue_prepare_job(
            request,
            video_id,
            lang,
            mode,
            discord_user_id,
            subtitle_source_lang=source_lang,
            translation_engine=translation_engine,
            archive_immediately=archive_immediately,
            reuse_cached_subtitle=True,
            reuse_source_video=True,
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
        "playlist_name": f"{playlist_name} / 再焼き込み",
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


async def enqueue_reburn_all(
    request: Request,
    lang: str | None,
    mode: str,
    max_items: int,
    discord_user_id: str | None,
    archive_immediately: bool = False,
) -> tuple[int, dict]:
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")

    candidates = []
    seen: set[str] = set()
    for prepared in reusable_source_entries():
        if lang and prepared.get("lang") != lang:
            continue
        key = str(prepared.get("key") or "")
        if not key or key in seen:
            continue
        if check_existing_source_video(key) is None:
            continue
        seen.add(key)
        candidates.append(prepared)
        if len(candidates) >= max_items:
            break

    if not candidates:
        raise HTTPException(status_code=404, detail="No reusable prepared entries found")

    mentions = [discord_mention(discord_user_id)] if discord_user_id else []
    batch_id = uuid.uuid4().hex
    items = []
    any_pending = False
    for prepared in candidates:
        video_id = str(prepared["video_id"])
        item_lang = str(prepared["lang"])
        key = str(prepared["key"])
        subtitle_meta = prepared.get("subtitle") if isinstance(prepared.get("subtitle"), dict) else {}
        source_lang, translation_engine = prepared_variant_from_meta(subtitle_meta)
        clear_rendered_outputs_only(key, mode)
        status_code, body = await enqueue_prepare_job(
            request,
            video_id,
            item_lang,
            mode,
            discord_user_id,
            subtitle_source_lang=source_lang,
            translation_engine=translation_engine,
            archive_immediately=archive_immediately,
            reuse_cached_subtitle=True,
            reuse_source_video=True,
        )
        if body.get("status") in {"queued", "running"}:
            any_pending = True
        items.append(
            {
                "video_id": video_id,
                "title": prepared.get("title") or body.get("title") or video_id,
                "lang": item_lang,
                "mode": mode,
                "status": body.get("status", "unknown"),
                "job_id": body.get("job_id"),
                "url": body.get("url"),
                "error": body.get("error"),
                "status_code": status_code,
            }
        )

    batch = {
        "source_type": "prepared",
        "source": "all",
        "playlist_id": "prepared",
        "playlist_name": "準備済み全件 / 再焼き込み",
        "lang": lang or "all",
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
    translation_options_json = json.dumps(translation_profile_options(), ensure_ascii=False)
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
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: calc(16 / 16 * 1rem);
      margin-bottom: calc(24 / 16 * 1rem);
    }}
    .metric-card {{
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      background: var(--color-gray-50);
      padding: calc(12 / 16 * 1rem) calc(16 / 16 * 1rem);
    }}
    .metric-card strong {{
      display: block;
      color: var(--color-gray-900);
      font-size: calc(14 / 16 * 1rem);
    }}
    .metric-card span {{
      font-size: calc(24 / 16 * 1rem);
      font-weight: 700;
    }}
    .chart {{
      width: 100%;
      height: 200px;
      border: 1px solid var(--color-gray-200);
      background: #fff;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: calc(16 / 16 * 1rem);
      margin-bottom: calc(24 / 16 * 1rem);
    }}
    .chart-card {{
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      background: var(--color-gray-50);
      padding: calc(12 / 16 * 1rem) calc(16 / 16 * 1rem);
      display: flex;
      flex-direction: column;
      gap: calc(8 / 16 * 1rem);
    }}
    .chart-card strong {{
      display: block;
      color: var(--color-gray-900);
      font-size: calc(14 / 16 * 1rem);
    }}
    .card-cpu {{
      border-left-color: #3b82f6;
    }}
    .card-memory {{
      border-left-color: #10b981;
    }}
    .card-gpu {{
      border-left-color: #8b5cf6;
    }}
    .job-list {{
      display: grid;
      gap: calc(12 / 16 * 1rem);
      margin-top: calc(16 / 16 * 1rem);
    }}
    .job-item {{
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      background: var(--color-gray-50);
      padding: calc(12 / 16 * 1rem) calc(16 / 16 * 1rem);
      overflow-wrap: anywhere;
    }}
    .compare-video {{
      width: 100%;
      max-height: 52vh;
      background: #000;
    }}
    .profile-grid, .compare-results {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: calc(12 / 16 * 1rem);
    }}
    .profile-grid label {{
      display: flex;
      align-items: center;
      gap: calc(8 / 16 * 1rem);
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      background: var(--color-gray-50);
      padding: calc(8 / 16 * 1rem) calc(12 / 16 * 1rem);
    }}
    select[multiple] {{
      min-height: calc(140 / 16 * 1rem);
    }}
    .profile-grid input {{
      width: auto;
      min-height: auto;
    }}
    .compare-result {{
      min-height: 128px;
      border-left: calc(8 / 16 * 1rem) solid var(--color-blue-900);
      background: var(--color-gray-50);
      padding: calc(12 / 16 * 1rem) calc(16 / 16 * 1rem);
      overflow-wrap: anywhere;
    }}
    .compare-result strong {{
      display: block;
      color: var(--color-gray-900);
      margin-bottom: calc(8 / 16 * 1rem);
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
      .profile-grid, .compare-results {{
        grid-template-columns: 1fr;
      }}
      .metric-grid,
      .chart-grid {{
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
        <button type="button" id="preparedTab" role="tab" aria-controls="preparedPanel" aria-selected="false">準備済み</button>
        <button type="button" id="monitorTab" role="tab" aria-controls="monitorPanel" aria-selected="false">監視</button>
        <button type="button" id="compareTab" role="tab" aria-controls="comparePanel" aria-selected="false">字幕比較</button>
        <button type="button" id="chatTab" role="tab" aria-controls="chatPanel" aria-selected="false">LLM</button>
        <button type="button" id="auditTab" role="tab" aria-controls="auditPanel" aria-selected="false">監査ログ</button>
      </div>
    <form id="converter" class="tool" aria-labelledby="videoTab">
      <label>
        YouTube URL / 動画ID
        <textarea id="youtubeUrl" name="youtubeUrl" placeholder="https://www.youtube.com/watch?v=...&#10;dQw4w9WgXcQ" autocomplete="off" required></textarea>
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
        <select id="translationEngine" name="translationEngine" multiple size="4">
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
            <option value="videos">Videos</option>
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
    <section id="preparedPanel" class="tool" aria-labelledby="preparedTab" hidden>
      <div class="actions">
        <button type="button" id="preparedRefreshButton">一覧を更新</button>
      </div>
      <output id="preparedMessage"></output>
      <div id="preparedList" class="job-list"></div>
    </section>
    <section id="monitorPanel" class="tool" aria-labelledby="monitorTab" hidden>
      <div class="metric-grid">
        <div class="metric-card card-cpu"><strong>CPU</strong><span id="metricCpu">--</span></div>
        <div class="metric-card card-memory"><strong>Memory</strong><span id="metricMemory">--</span></div>
        <div class="metric-card card-gpu"><strong>GPU</strong><span id="metricGpu">--</span></div>
      </div>
      <div class="chart-grid">
        <div class="chart-card card-cpu">
          <strong>CPU 使用率履歴</strong>
          <svg id="cpuChart" class="chart" viewBox="0 0 960 240" role="img" aria-label="CPU history graph"></svg>
        </div>
        <div class="chart-card card-memory">
          <strong>Memory 使用率履歴</strong>
          <svg id="memoryChart" class="chart" viewBox="0 0 960 240" role="img" aria-label="Memory history graph"></svg>
        </div>
        <div class="chart-card card-gpu">
          <strong>GPU 使用率履歴</strong>
          <svg id="gpuChart" class="chart" viewBox="0 0 960 240" role="img" aria-label="GPU history graph"></svg>
        </div>
      </div>
      <output id="metricDetails"></output>
      <h2>準備ジョブ</h2>
      <div id="monitorJobs" class="job-list"></div>
    </section>
    <section id="comparePanel" class="tool" aria-labelledby="compareTab" hidden>
      <form id="compareForm">
        <label>
          YouTube URL / 動画ID
          <input id="compareUrl" name="compareUrl" type="text" placeholder="https://www.youtube.com/watch?v=... or dQw4w9WgXcQ" autocomplete="off">
        </label>
        <div class="row">
          <label>
            翻訳元字幕
            <input id="compareSourceLang" name="compareSourceLang" value="" maxlength="64" autocomplete="off">
          </label>
          <label>
            翻訳先
            <input id="compareTargetLang" name="compareTargetLang" value="ja" maxlength="12" autocomplete="off">
          </label>
        </div>
        <div class="actions">
          <button type="button" id="compareLoadVariantsButton" class="secondary">字幕候補を読み込む</button>
          <button type="button" id="comparePrepareButton">比較用に準備</button>
        </div>
        <label>
          再生する動画
          <select id="comparePlaybackSource" name="comparePlaybackSource"></select>
        </label>
        <output id="compareStatus"></output>
      </form>
      <div id="compareVariants" class="job-list"></div>
      <video id="compareVideo" class="compare-video" controls></video>
      <div id="compareResults" class="compare-results"></div>
    </section>
    <section id="chatPanel" class="tool" aria-labelledby="chatTab" hidden>
      <div class="row">
        <label>
          モデル
          <select id="chatModel" name="chatModel"></select>
        </label>
        <label>
          生成温度
          <input id="chatTemperature" name="chatTemperature" type="number" min="0" max="2" step="0.1" value="0.4">
        </label>
      </div>
      <label>
        System prompt
        <textarea id="chatSystemPrompt" name="chatSystemPrompt" placeholder="任意"></textarea>
      </label>
      <div id="chatLog" class="job-list"></div>
      <label>
        メッセージ
        <textarea id="chatInput" name="chatInput" placeholder="質問を書く"></textarea>
      </label>
      <div class="actions">
        <button type="button" id="chatSendButton">送信</button>
        <button type="button" id="chatClearButton" class="secondary">履歴を消す</button>
      </div>
      <output id="chatStatus"></output>
    </section>
    <section id="auditPanel" class="tool" aria-labelledby="auditTab" hidden>
      <div class="actions">
        <button type="button" id="auditRefreshButton">ログを更新</button>
      </div>
      <div class="row">
        <label>
          フィルタ
          <input id="auditFilter" name="auditFilter" type="text" placeholder="video_id / model / provider" autocomplete="off">
        </label>
        <label>
          件数
          <input id="auditLimit" name="auditLimit" type="number" min="1" max="2000" step="1" value="200">
        </label>
      </div>
      <output id="auditMessage"></output>
      <div id="auditList" class="job-list"></div>
      <h2>詳細</h2>
      <output id="auditDetailTitle"></output>
      <pre id="auditDetail" style="white-space: pre-wrap; overflow-wrap: anywhere;"></pre>
    </section>
    </section>
  </main>
  <script>
    const defaultLang = {default_lang};
    const translationOptions = {translation_options_json};
    const form = document.getElementById("converter");
    const jsonForm = document.getElementById("jsonExporter");
    const monitorPanel = document.getElementById("monitorPanel");
    const comparePanel = document.getElementById("comparePanel");
    const videoTab = document.getElementById("videoTab");
    const jsonTab = document.getElementById("jsonTab");
    const preparedTab = document.getElementById("preparedTab");
    const monitorTab = document.getElementById("monitorTab");
    const compareTab = document.getElementById("compareTab");
    const chatTab = document.getElementById("chatTab");
    const auditTab = document.getElementById("auditTab");
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
    const preparedPanel = document.getElementById("preparedPanel");
    const preparedRefreshButton = document.getElementById("preparedRefreshButton");
    const preparedMessage = document.getElementById("preparedMessage");
    const preparedList = document.getElementById("preparedList");
    const metricCpu = document.getElementById("metricCpu");
    const metricMemory = document.getElementById("metricMemory");
    const metricGpu = document.getElementById("metricGpu");
    const metricDetails = document.getElementById("metricDetails");
    const monitorJobs = document.getElementById("monitorJobs");
    const compareUrl = document.getElementById("compareUrl");
    const compareSourceLang = document.getElementById("compareSourceLang");
    const compareTargetLang = document.getElementById("compareTargetLang");
    const compareLoadVariantsButton = document.getElementById("compareLoadVariantsButton");
    const comparePrepareButton = document.getElementById("comparePrepareButton");
    const comparePlaybackSource = document.getElementById("comparePlaybackSource");
    const compareStatus = document.getElementById("compareStatus");
    const compareVideo = document.getElementById("compareVideo");
    const compareResults = document.getElementById("compareResults");
    const chatPanel = document.getElementById("chatPanel");
    const chatModel = document.getElementById("chatModel");
    const chatTemperature = document.getElementById("chatTemperature");
    const chatSystemPrompt = document.getElementById("chatSystemPrompt");
    const chatLog = document.getElementById("chatLog");
    const chatInput = document.getElementById("chatInput");
    const chatSendButton = document.getElementById("chatSendButton");
    const chatClearButton = document.getElementById("chatClearButton");
    const chatStatus = document.getElementById("chatStatus");
    const compareVariants = document.getElementById("compareVariants");
    const auditPanel = document.getElementById("auditPanel");
    const auditRefreshButton = document.getElementById("auditRefreshButton");
    const auditFilter = document.getElementById("auditFilter");
    const auditLimit = document.getElementById("auditLimit");
    const auditMessage = document.getElementById("auditMessage");
    const auditList = document.getElementById("auditList");
    const auditDetailTitle = document.getElementById("auditDetailTitle");
    const auditDetail = document.getElementById("auditDetail");

    lang.value = defaultLang;
    jsonLang.value = defaultLang;
    prepareToken.value = localStorage.getItem("youtubeProxyPrepareToken") || "";

    function selectTool(tool) {{
      const jsonSelected = tool === "json";
      const preparedSelected = tool === "prepared";
      const monitorSelected = tool === "monitor";
      const compareSelected = tool === "compare";
      const chatSelected = tool === "chat";
      const auditSelected = tool === "audit";
      form.hidden = jsonSelected || preparedSelected || monitorSelected || compareSelected || chatSelected || auditSelected;
      jsonForm.hidden = !jsonSelected;
      preparedPanel.hidden = !preparedSelected;
      monitorPanel.hidden = !monitorSelected;
      comparePanel.hidden = !compareSelected;
      chatPanel.hidden = !chatSelected;
      auditPanel.hidden = !auditSelected;
      videoTab.setAttribute("aria-selected", String(!jsonSelected && !preparedSelected && !monitorSelected && !compareSelected && !chatSelected && !auditSelected));
      jsonTab.setAttribute("aria-selected", String(jsonSelected));
      preparedTab.setAttribute("aria-selected", String(preparedSelected));
      monitorTab.setAttribute("aria-selected", String(monitorSelected));
      compareTab.setAttribute("aria-selected", String(compareSelected));
      chatTab.setAttribute("aria-selected", String(chatSelected));
      auditTab.setAttribute("aria-selected", String(auditSelected));
      if (preparedSelected) loadPreparedList();
      if (monitorSelected) updateMonitor();
      if (chatSelected) renderChat();
      if (auditSelected) loadAuditList();
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

    function splitVideoValues(value) {{
      return value.split(/[\\s,]+/).map((part) => part.trim()).filter(Boolean);
    }}

    function manualVideoIds(value) {{
      const ids = [];
      const seen = new Set();
      for (const part of splitVideoValues(value)) {{
        const id = extractVideoId(part);
        if (id && !seen.has(id)) {{
          seen.add(id);
          ids.push(id);
        }}
      }}
      return ids;
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
      const videoIds = manualVideoIds(input.value);
      const videoId = videoIds[0] || "";
      const language = (lang.value || defaultLang).trim();
      if (!input.value.trim()) {{
        result.textContent = "";
        message.textContent = "";
        return;
      }}
      if (videoIds.length > 1) {{
        result.textContent = "";
        message.textContent = `${{videoIds.length}} 件の動画を一括準備します。`;
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

    function renderTranslationOptions() {{
      translationEngine.innerHTML = "";
      chatModel.innerHTML = "";
      for (const option of translationOptions) {{
        const selectOption = document.createElement("option");
        selectOption.value = option.value;
        selectOption.textContent = option.label || option.value;
        if (option.default) selectOption.selected = true;
        translationEngine.appendChild(selectOption);
        if (option.value !== "google_cloud") {{
          const chatOption = document.createElement("option");
          chatOption.value = option.value;
          chatOption.textContent = `${{option.label || option.value}}${{option.model ? ` / ${{option.model}}` : ""}}`;
          if (option.default) chatOption.selected = true;
          chatModel.appendChild(chatOption);
        }}
      }}
      if (!Array.from(translationEngine.selectedOptions).length) {{
        const fallback = translationEngine.querySelector('option[value="google_cloud"]') || translationEngine.options[0];
        if (fallback) fallback.selected = true;
      }}
    }}

    function percentText(value) {{
      return typeof value === "number" ? `${{value.toFixed(1)}}%` : "--";
    }}

    function bytesText(value) {{
      if (typeof value !== "number") return "--";
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let size = value;
      let unit = 0;
      while (size >= 1024 && unit < units.length - 1) {{
        size /= 1024;
        unit += 1;
      }}
      return `${{size.toFixed(unit ? 1 : 0)}} ${{units[unit]}}`;
    }}

    function metricValue(sample, path) {{
      let current = sample;
      for (const key of path) {{
        if (!current || typeof current !== "object") return null;
        current = current[key];
      }}
      return typeof current === "number" ? current : null;
    }}

    function drawMetricsChart(history) {{
      const width = 960;
      const height = 240;
      const padding = 28;
      const series = [
        {{ id: "cpuChart", name: "CPU", color: "#3b82f6", path: ["cpu", "used_percent"] }},
        {{ id: "memoryChart", name: "Memory", color: "#10b981", path: ["memory", "used_percent"] }},
        {{ id: "gpuChart", name: "GPU", color: "#8b5cf6", path: ["gpu", "gpu_percent"] }},
      ];
      const xFor = (index) => padding + (history.length <= 1 ? 0 : index / (history.length - 1) * (width - padding * 2));
      const yFor = (value) => height - padding - value / 100 * (height - padding * 2);

      for (const item of series) {{
        const chartSvg = document.getElementById(item.id);
        if (!chartSvg) continue;

        const points = history
          .map((sample, index) => [index, metricValue(sample, item.path)])
          .filter((point) => point[1] !== null);

        if (points.length < 2) {{
          chartSvg.innerHTML = "";
          continue;
        }}

        const svgPoints = points.map(([index, value]) => [xFor(index), yFor(value)]);
        const lineD = svgPoints.map(([x, y], i) => `${{i ? "L" : "M"}} ${{x.toFixed(1)}} ${{y.toFixed(1)}}`).join(" ");
        const yBottom = height - padding;
        const areaD = `${{lineD}} L ${{svgPoints[svgPoints.length - 1][0].toFixed(1)}} ${{yBottom.toFixed(1)}} L ${{svgPoints[0][0].toFixed(1)}} ${{yBottom.toFixed(1)}} Z`;
        const gradientId = `${{item.id}}Gradient`;

        chartSvg.innerHTML = `
          <defs>
            <linearGradient id="${{gradientId}}" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="${{item.color}}" stop-opacity="0.45"/>
              <stop offset="100%" stop-color="${{item.color}}" stop-opacity="0.0"/>
            </linearGradient>
          </defs>
          <rect x="0" y="0" width="${{width}}" height="${{height}}" fill="#fff"></rect>
          <line x1="${{padding}}" y1="${{padding}}" x2="${{padding}}" y2="${{height - padding}}" stroke="#e5e7eb" stroke-width="1.5"></line>
          <line x1="${{padding}}" y1="${{height - padding}}" x2="${{width - padding}}" y2="${{height - padding}}" stroke="#9ca3af" stroke-width="1.5"></line>
          
          <!-- grid lines -->
          <line x1="${{padding}}" y1="${{yFor(25).toFixed(1)}}" x2="${{width - padding}}" y2="${{yFor(25).toFixed(1)}}" stroke="#f3f4f6" stroke-width="1"></line>
          <line x1="${{padding}}" y1="${{yFor(50).toFixed(1)}}" x2="${{width - padding}}" y2="${{yFor(50).toFixed(1)}}" stroke="#e5e7eb" stroke-width="1"></line>
          <line x1="${{padding}}" y1="${{yFor(75).toFixed(1)}}" x2="${{width - padding}}" y2="${{yFor(75).toFixed(1)}}" stroke="#f3f4f6" stroke-width="1"></line>

          <text x="${{padding + 8}}" y="22" font-size="14" font-weight="600" fill="#9ca3af">100%</text>
          <text x="${{padding + 8}}" y="${{height - 10}}" font-size="14" font-weight="600" fill="#9ca3af">0%</text>
          
          <path d="${{areaD}}" fill="url(#${{gradientId}})"></path>
          <path d="${{lineD}}" fill="none" stroke="${{item.color}}" stroke-width="3"></path>
        `;
      }}
    }}

    function renderMonitorJobs(jobs) {{
      if (!jobs || jobs.length === 0) {{
        monitorJobs.textContent = "実行中の準備ジョブはありません。";
        return;
      }}
      monitorJobs.innerHTML = "";
      for (const job of jobs) {{
        const progress = job.progress || {{}};
        const counts = job.queue_counts || {{}};
        const item = document.createElement("div");
        item.className = "job-item";
        const percent = typeof progress.percent === "number" ? `${{progress.percent.toFixed(1)}}%` : "--";
        const eta = typeof job.estimated_ready_at === "number" ? new Date(job.estimated_ready_at * 1000).toLocaleTimeString() : "--";
        item.textContent = `${{job.mode?.toUpperCase() || "MP4"}} ${{job.status}} / ${{job.title || job.video_id}} / ${{progress.phase || "queued"}} ${{percent}} / 終了予想 ${{eta}} / 待ち: DL ${{counts.download || 0}}, 翻訳 ${{counts.translate || 0}}, Encode ${{counts.encode || 0}}`;
        monitorJobs.appendChild(item);
      }}
    }}

    async function updateMonitor() {{
      try {{
        const response = await fetch("/monitor/system?seconds=21600", {{ headers: {{ "Accept": "application/json" }} }});
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || `HTTP ${{response.status}}`);
        const current = body.current || {{}};
        metricCpu.textContent = percentText(current.cpu?.used_percent);
        metricMemory.textContent = percentText(current.memory?.used_percent);
        metricGpu.textContent = current.gpu ? percentText(current.gpu.gpu_percent) : "n/a";
        const hot = current.disk?.hot;
        const archive = current.disk?.archive;
        metricDetails.textContent = `GPU mem: ${{current.gpu ? `${{current.gpu.memory_used_mib}}/${{current.gpu.memory_total_mib}} MiB` : "n/a"}} / Hot free: ${{hot ? bytesText(hot.free_bytes) : "n/a"}} / Archive free: ${{archive ? bytesText(archive.free_bytes) : "n/a"}}`;
        drawMetricsChart(body.history || []);
        renderMonitorJobs(current.jobs || []);
      }} catch (error) {{
        metricDetails.textContent = `監視APIエラー: ${{error.message}}`;
      }}
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

    let activePrepareBody = null;
    let etaTimer = null;

    function stopEtaTimer() {{
      activePrepareBody = null;
      if (etaTimer) {{
        clearInterval(etaTimer);
        etaTimer = null;
      }}
    }}

    function setPrepareStatus(body) {{
      activePrepareBody = body;
      prepareStatus.textContent = prepareMessage(body);
      if (body.status === "queued" || body.status === "running") {{
        if (!etaTimer) {{
          etaTimer = setInterval(() => {{
            if (activePrepareBody) {{
              prepareStatus.textContent = prepareMessage(activePrepareBody);
            }}
          }}, 1000);
        }}
        return;
      }}
      stopEtaTimer();
    }}

    function etaText(body) {{
      const parts = [];
      let seconds = null;
      if (typeof body.estimated_ready_at === "number" && body.estimated_ready_at > 0) {{
        seconds = Math.ceil(body.estimated_ready_at - Date.now() / 1000);
      }} else if (typeof body.eta_seconds === "number" && body.eta_seconds > 0) {{
        seconds = Math.round(body.eta_seconds);
      }}
      if (typeof seconds === "number" && seconds > 0) {{
        seconds = Math.max(1, seconds);
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

    function subtitleSummary(meta) {{
      if (!meta || typeof meta !== "object") return "字幕: 不明";
      if (meta.translated) {{
        const source = meta.source_language || "auto";
        const engine = meta.translation_engine_requested || meta.translation_engine || "unknown";
        return `字幕: ${{source}} → ${{meta.requested_language || "ja"}} / ${{engine}}`;
      }}
      return `字幕: ${{meta.source_language || meta.requested_language || "manual"}}`;
    }}

    async function copyPreparedUrl(url, button) {{
      await navigator.clipboard.writeText(url);
      const original = button.textContent;
      button.textContent = "コピー済み";
      setTimeout(() => {{
        button.textContent = original;
      }}, 1200);
    }}

    function renderPreparedList(items) {{
      preparedList.innerHTML = "";
      if (!items.length) {{
        preparedList.textContent = "準備済み動画はありません。";
        return;
      }}
      for (const item of items) {{
        const row = document.createElement("div");
        row.className = "job-item";

        const title = document.createElement("strong");
        title.textContent = item.title || item.video_id;
        row.appendChild(title);

        const meta = document.createElement("div");
        meta.textContent = `${{item.video_id}} / 言語: ${{item.lang}} / ${{subtitleSummary(item.subtitle)}} / 保存: ${{item.storage}}`;
        row.appendChild(meta);

        const source = document.createElement("div");
        source.textContent = `元動画: ${{item.source_url || ""}}`;
        row.appendChild(source);

        const actions = document.createElement("div");
        actions.className = "actions";
        for (const output of item.outputs || []) {{
          const url = publicUrl(output.url);
          const button = document.createElement("button");
          button.type = "button";
          button.className = "secondary";
          button.textContent = `${{String(output.mode || "").toUpperCase()}} URLをコピー`;
          button.addEventListener("click", () => copyPreparedUrl(url, button));
          actions.appendChild(button);
        }}
        const sourceButton = document.createElement("button");
        sourceButton.type = "button";
        sourceButton.className = "secondary";
        sourceButton.textContent = "元動画URLをコピー";
        sourceButton.addEventListener("click", () => copyPreparedUrl(item.source_url || "", sourceButton));
        actions.appendChild(sourceButton);
        row.appendChild(actions);

        preparedList.appendChild(row);
      }}
    }}

    async function loadPreparedList() {{
      preparedMessage.textContent = "準備済み一覧を読み込み中...";
      try {{
        const body = await apiFetch("/prepared");
        renderPreparedList(Array.isArray(body.items) ? body.items : []);
        preparedMessage.textContent = `${{body.count || 0}} 件`;
      }} catch (error) {{
        preparedMessage.textContent = `一覧APIエラー: ${{error.message}}`;
      }}
    }}

    async function waitForReady(statusUrl, label) {{
      let latest = null;
      for (let i = 0; i < 720; i++) {{
        await new Promise((resolve) => setTimeout(resolve, i === 0 ? 1000 : 5000));
        const parsed = new URL(statusUrl, location.origin);
        latest = await apiFetch(parsed.pathname + parsed.search);
        compareStatus.textContent = `${{label}}: ${{prepareMessage(latest)}}`;
        if (latest.status === "ready") return latest;
        if (latest.status === "failed") throw new Error(`${{label}} failed: ${{latest.error || "unknown error"}}`);
      }}
      throw new Error(`${{label}} timeout`);
    }}

    function currentSubtitle(events, time) {{
      const event = events.find((item) => item.start <= time && time <= item.end);
      return event ? event.text : "";
    }}

    function renderCompareResults(profileBodies) {{
      compareResults.innerHTML = "";
      const updateTexts = () => {{
        const time = compareVideo.currentTime || 0;
        for (const item of profileBodies) {{
          item.sourceText.textContent = currentSubtitle(item.sourceEvents, time) || "（この時刻の原字幕なし）";
          item.translatedText.textContent = currentSubtitle(item.translatedEvents, time) || "（この時刻の翻訳字幕なし）";
        }}
      }};
      for (const item of profileBodies) {{
        const box = document.createElement("div");
        box.className = "compare-result";
        const title = document.createElement("strong");
        title.textContent = item.label;
        const sourceTitle = document.createElement("div");
        sourceTitle.textContent = "元字幕";
        sourceTitle.style.fontWeight = "700";
        const sourceText = document.createElement("div");
        sourceText.style.marginBottom = "calc(8 / 16 * 1rem)";
        const translatedTitle = document.createElement("div");
        translatedTitle.textContent = "翻訳字幕";
        translatedTitle.style.fontWeight = "700";
        const translatedText = document.createElement("div");
        item.sourceText = sourceText;
        item.translatedText = translatedText;
        box.appendChild(title);
        box.appendChild(sourceTitle);
        box.appendChild(sourceText);
        box.appendChild(translatedTitle);
        box.appendChild(translatedText);
        compareResults.appendChild(box);
      }}
      compareVideo.ontimeupdate = updateTexts;
      compareVideo.onpause = updateTexts;
      compareVideo.onseeked = updateTexts;
      updateTexts();
    }}

    const chatState = {{
      messages: [],
    }};

    function renderChat() {{
      chatLog.innerHTML = "";
      if (!chatState.messages.length) {{
        chatLog.textContent = "会話はまだありません。";
        return;
      }}
      for (const message of chatState.messages) {{
        const row = document.createElement("div");
        row.className = "job-item";
        const title = document.createElement("strong");
        title.textContent = message.role === "assistant" ? "assistant" : message.role === "system" ? "system" : "user";
        row.appendChild(title);
        const body = document.createElement("div");
        body.style.whiteSpace = "pre-wrap";
        body.style.overflowWrap = "anywhere";
        body.textContent = message.content;
        row.appendChild(body);
        chatLog.appendChild(row);
      }}
    }}

    function addChatMessage(role, content) {{
      chatState.messages.push({{ role, content }});
      renderChat();
    }}

    async function sendChatMessage() {{
      const content = chatInput.value.trim();
      if (!content) {{
        chatStatus.textContent = "メッセージを入力してください。";
        return;
      }}
      const profile = chatModel.value || (translationOptions.find((item) => item.value !== "google_cloud" && item.default)?.value || translationOptions.find((item) => item.value !== "google_cloud")?.value || "");
      if (!profile) {{
        chatStatus.textContent = "利用可能なモデルがありません。";
        return;
      }}
      addChatMessage("user", content);
      chatInput.value = "";
      chatSendButton.disabled = true;
      chatStatus.textContent = "応答を取得中...";
      try {{
        const body = await apiFetch("/chat", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            profile,
            system_prompt: chatSystemPrompt.value.trim(),
            messages: chatState.messages,
            temperature: Number(chatTemperature.value || 0.4),
            max_tokens: 1024,
          }}),
        }});
        addChatMessage("assistant", body.reply || "");
        chatStatus.textContent = `${{body.model || profile}} / ${{body.provider || "unknown"}}`;
      }} catch (error) {{
        chatStatus.textContent = `LLMエラー: ${{error.message}}`;
      }} finally {{
        chatSendButton.disabled = false;
      }}
    }}

    function renderVariantList(items) {{
      compareVariants.innerHTML = "";
      compareResults.innerHTML = "";
      comparePlaybackSource.innerHTML = "";
      if (!items.length) {{
        compareVariants.textContent = "この動画IDに対応する字幕キャッシュはありません。";
        const emptyOption = document.createElement("option");
        emptyOption.value = "";
        emptyOption.textContent = "再生可能な動画なし";
        comparePlaybackSource.appendChild(emptyOption);
        return;
      }}
      const playbackOptions = [];
      for (const item of items) {{
        const row = document.createElement("div");
        row.className = "job-item";
        const label = document.createElement("label");
        label.style.fontWeight = "400";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = true;
        checkbox.dataset.key = item.key || "";
        checkbox.dataset.source = item.source_language || "";
        checkbox.dataset.engine = item.translation_engine || "";
        label.appendChild(checkbox);
        label.append(` ${{item.label || item.key}}`);
        row.appendChild(label);
        const meta = document.createElement("div");
        meta.textContent = `${{item.title || item.video_id || "-"}} / ${{item.source_language || "-"}} → ${{item.requested_language || "-"}} / ${{item.storage || "-"}}`;
        row.appendChild(meta);
        if (item.source_ready) {{
          playbackOptions.push({{
            label: `${{item.label || item.key}} / 字幕なし`,
            url: `/prepared/${{encodeURIComponent(item.key)}}/source.mp4`,
          }});
        }}
        for (const output of item.outputs || []) {{
          if (output?.url) {{
            playbackOptions.push({{
              label: `${{item.label || item.key}} / ${{String(output.mode || "").toUpperCase()}}`,
              url: publicUrl(output.url),
            }});
          }}
        }}
        compareVariants.appendChild(row);
      }}
      if (!playbackOptions.length) {{
        const emptyOption = document.createElement("option");
        emptyOption.value = "";
        emptyOption.textContent = "再生可能な動画なし";
        comparePlaybackSource.appendChild(emptyOption);
      }} else {{
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "再生する動画を選択";
        comparePlaybackSource.appendChild(placeholder);
        for (const option of playbackOptions) {{
          const item = document.createElement("option");
          item.value = option.url;
          item.textContent = option.label;
          comparePlaybackSource.appendChild(item);
        }}
      }}
      compareVideo.src = comparePlaybackSource.value || "";
    }}

    async function loadCompareVariants() {{
      const videoId = extractVideoId(compareUrl.value || input.value);
      if (!videoId) {{
        compareStatus.textContent = "動画URLまたは動画IDを入力してください。";
        return;
      }}
      compareStatus.textContent = "字幕候補を読み込み中...";
      try {{
        const body = await apiFetch(`/prepare/youtube/${{videoId}}/variants`);
        const items = Array.isArray(body.variants) ? body.variants : [];
        renderVariantList(items);
        compareSourceLang.value = compareSourceLang.value || (items[0]?.source_language || "");
        const hasPlayable = items.some((item) => Array.isArray(item.outputs) && item.outputs.length) || items.some((item) => item.source_ready);
        compareStatus.textContent = hasPlayable
          ? `${{items.length}} 件の字幕候補を読み込みました。`
          : `${{items.length}} 件の字幕候補を読み込みましたが、再生できるMP4/HLSがありません。先に準備してください。`;
      }} catch (error) {{
        compareStatus.textContent = `字幕候補取得エラー: ${{error.message}}`;
      }}
    }}

    function renderAuditDetail(name, records) {{
      auditDetailTitle.textContent = name ? `${{name}} (${{records.length}} records)` : "";
      auditDetail.textContent = records.map((record) => JSON.stringify(record, null, 2)).join("\\n\\n");
    }}

    function renderAuditList(items) {{
      auditList.innerHTML = "";
      auditDetail.textContent = "";
      auditDetailTitle.textContent = "";
      if (!items.length) {{
        auditList.textContent = "監査ログはありません。";
        return;
      }}
      for (const item of items) {{
        const row = document.createElement("div");
        row.className = "job-item";
        const title = document.createElement("strong");
        title.textContent = item.name;
        row.appendChild(title);
        const meta = document.createElement("div");
        meta.textContent = `${{item.video_id || "-"}} / ${{item.lang || "-"}} / ${{item.model_name || "-"}} / req ${{item.request_count}} / resp ${{item.response_count}} / err ${{item.error_count}}`;
        row.appendChild(meta);
        if (item.sample_prompt) {{
          const prompt = document.createElement("div");
          prompt.textContent = `prompt: ${{item.sample_prompt.slice(0, 160)}}`;
          row.appendChild(prompt);
        }}
        if (item.sample_response) {{
          const response = document.createElement("div");
          response.textContent = `response: ${{item.sample_response.slice(0, 160)}}`;
          row.appendChild(response);
        }}
        const actions = document.createElement("div");
        actions.className = "actions";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        button.textContent = "詳細表示";
        button.addEventListener("click", async () => {{
          auditMessage.textContent = `読み込み中: ${{item.name}}`;
          try {{
            const detail = await apiFetch(`/translation-audit/${{encodeURIComponent(item.name)}}?limit=${{Number(auditLimit.value || 200)}}`);
            renderAuditDetail(detail.name, Array.isArray(detail.records) ? detail.records : []);
            auditMessage.textContent = `${{detail.count || 0}} records`;
          }} catch (error) {{
            auditMessage.textContent = `詳細取得エラー: ${{error.message}}`;
          }}
        }});
        actions.appendChild(button);
        row.appendChild(actions);
        auditList.appendChild(row);
      }}
    }}

    async function loadAuditList() {{
      auditMessage.textContent = "監査ログを読み込み中...";
      try {{
        const body = await apiFetch("/translation-audit");
        let items = Array.isArray(body.items) ? body.items : [];
        const filter = (auditFilter.value || "").trim().toLowerCase();
        if (filter) {{
          items = items.filter((item) => [
            item.name,
            item.video_id,
            item.model_name,
            item.provider,
            item.source_language,
            item.lang,
          ].some((value) => String(value || "").toLowerCase().includes(filter)));
        }}
        renderAuditList(items);
        auditMessage.textContent = `${{items.length}} 件`;
      }} catch (error) {{
        auditMessage.textContent = `監査APIエラー: ${{error.message}}`;
      }}
    }}

    async function prepareCompare() {{
      const videoId = extractVideoId(compareUrl.value || input.value);
      const target = compareTargetLang.value.trim() || "ja";
      const selectedVariants = Array.from(compareVariants.querySelectorAll("input[data-key]:checked"));
      if (!videoId || selectedVariants.length === 0) {{
        compareStatus.textContent = "動画URLと字幕候補を確認してください。";
        return;
      }}
      comparePrepareButton.disabled = true;
      try {{
        const profileBodies = [];
        for (const checkbox of selectedVariants) {{
          const key = checkbox.dataset.key || "";
          const sourceLang = checkbox.dataset.source || compareSourceLang.value.trim();
          const engine = checkbox.dataset.engine || "";
          compareStatus.textContent = `${{key}} を読み込んでいます。`;
          const bundleBody = await apiFetch(`/prepare/subtitle-bundle/${{encodeURIComponent(key)}}`);
          profileBodies.push({{
            label: checkbox.parentElement?.textContent?.trim() || key,
            sourceEvents: bundleBody.source_events || [],
            translatedEvents: bundleBody.translated_events || [],
          }});
          if (engine) {{
            const body = await apiFetch(`/prepare/youtube/${{videoId}}/${{target}}/${{encodeURIComponent(sourceLang)}}/${{encodeURIComponent(engine)}}?mode=mp4`, {{ method: "POST" }});
            if (!comparePlaybackSource.value && body.url) comparePlaybackSource.value = publicUrl(body.url);
          }}
        }}
        renderCompareResults(profileBodies);
        compareStatus.textContent = "比較用字幕を読み込みました。動画を一時停止またはシークして確認できます。";
      }} catch (error) {{
        compareStatus.textContent = `比較準備エラー: ${{error.message}}`;
      }} finally {{
        comparePrepareButton.disabled = false;
      }}
    }}

    async function pollPrepare(statusUrl) {{
      let latest = null;
      for (let i = 0; i < 720; i++) {{
        await new Promise((resolve) => setTimeout(resolve, i === 0 ? 2000 : 10000));
        const parsed = new URL(statusUrl, location.origin);
        const body = await apiFetch(parsed.pathname + parsed.search);
        latest = body;
        setPrepareStatus(body);
        if (body.status === "ready") {{
          const url = publicUrl(body.url);
          result.textContent = url;
          notify("YouTube準備完了", url);
          return;
        }}
        if (body.status === "failed") {{
          notify("YouTube準備失敗", body.error || "unknown error");
          return body;
        }}
      }}
      stopEtaTimer();
      notify("YouTube準備確認タイムアウト", latest ? prepareMessage(latest) : "status polling timeout");
      return latest;
    }}

    async function loadSubtitleChoices(videoId, language, selectedMode) {{
      const params = new URLSearchParams({{ mode: selectedMode }});
      const body = await apiFetch(`/prepare/youtube/${{videoId}}/${{language}}/subtitles?${{params.toString()}}`);
      if (!body.requires_choice) return false;
      stopEtaTimer();
      subtitleSource.innerHTML = "";
      for (const candidate of body.candidates || []) {{
        const option = document.createElement("option");
        option.value = candidate.language;
        option.textContent = `${{candidate.language}} / ${{candidate.name || candidate.name_en || candidate.language}}`;
        subtitleSource.appendChild(option);
      }}
      if (Array.isArray(body.translation_engines)) {{
        translationEngine.innerHTML = "";
        for (const engine of body.translation_engines) {{
          const option = document.createElement("option");
          option.value = engine.value;
          option.textContent = engine.label || engine.value;
          if (engine.default) option.selected = true;
          translationEngine.appendChild(option);
        }}
      }}
      prepareOptions.hidden = false;
      prepareStatus.textContent = "日本語字幕が見つかりませんでした。翻訳元字幕と翻訳方式を選んで、もう一度 Prepare を押してください。";
      return true;
    }}

    async function prepareCurrentVideo() {{
      stopEtaTimer();
      update();
      const videoIds = manualVideoIds(input.value);
      const videoId = videoIds[0] || "";
      const language = (lang.value || defaultLang).trim();
      const token = prepareToken.value.trim();
      const selectedMode = prepareMode();
      const selectedEngines = Array.from(translationEngine.selectedOptions).map((option) => option.value).filter(Boolean);
      if (!token) {{
        prepareStatus.textContent = "Prepare token を入力してください。";
        return;
      }}
      if ((!videoIds.length || !/^[A-Za-z0-9_-]{{11}}$/.test(videoId)) || !/^[A-Za-z0-9_-]{{2,12}}$/.test(language)) {{
        prepareStatus.textContent = "YouTube URLと言語を確認してください。";
        return;
      }}
      prepareButton.disabled = true;
      try {{
        if (videoIds.length > 1) {{
          const params = new URLSearchParams({{
            source: videoIds.join("\\n"),
            sourceType: "videos",
            mode: selectedMode,
            maxItems: String(videoIds.length),
          }});
          const body = await apiFetch(`/prepare/youtube-batch/${{language}}?${{params.toString()}}`, {{ method: "POST" }});
          setPrepareStatus(body);
          if (body.status_url) await pollPrepare(body.status_url);
          return;
        }}
        let path = `/prepare/youtube/${{videoId}}/${{language}}`;
        if (language === "ja" && prepareOptions.hidden) {{
          const needsChoice = await loadSubtitleChoices(videoId, language, selectedMode);
          if (needsChoice) return;
        }}
        if (!prepareOptions.hidden && subtitleSource.value) {{
          const sourceLang = subtitleSource.value;
          const engines = selectedEngines.length ? selectedEngines : [translationEngine.value || "google_cloud"];
          const results = [];
          for (let index = 0; index < engines.length; index += 1) {{
            const engine = engines[index];
            const variantPath = `${{path}}/${{encodeURIComponent(sourceLang)}}/${{encodeURIComponent(engine)}}`;
            const params = new URLSearchParams({{ mode: selectedMode }});
            prepareStatus.textContent = `準備中 ${{index + 1}}/${{engines.length}}: ${{sourceLang}} → ${{engine}}`;
            const body = await apiFetch(`${{variantPath}}?${{params.toString()}}`, {{ method: "POST" }});
            results.push(body);
            setPrepareStatus(body);
            if (body.status === "ready" && !result.textContent) {{
              result.textContent = publicUrl(body.url);
              notify("YouTube準備完了", result.textContent);
            }} else if (body.status_url) {{
              const latest = await pollPrepare(body.status_url);
              results[results.length - 1] = latest || body;
              if (latest?.status === "ready" && !result.textContent) {{
                result.textContent = publicUrl(latest.url);
                notify("YouTube準備完了", result.textContent);
              }}
            }}
          }}
          prepareStatus.textContent = `${{results.length}} 件の翻訳パターンを準備しました。`;
          return;
        }}
        const params = new URLSearchParams({{ mode: selectedMode }});
        const body = await apiFetch(`${{path}}?${{params.toString()}}`, {{ method: "POST" }});
        setPrepareStatus(body);
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
        stopEtaTimer();
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
    preparedTab.addEventListener("click", () => selectTool("prepared"));
    monitorTab.addEventListener("click", () => selectTool("monitor"));
    compareTab.addEventListener("click", () => selectTool("compare"));
    chatTab.addEventListener("click", () => selectTool("chat"));
    auditTab.addEventListener("click", () => selectTool("audit"));
    setInterval(() => {{
      if (!monitorPanel.hidden) updateMonitor();
    }}, 5000);
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
    compareLoadVariantsButton.addEventListener("click", loadCompareVariants);
    comparePrepareButton.addEventListener("click", prepareCompare);
    comparePlaybackSource.addEventListener("change", () => {{
      compareVideo.src = comparePlaybackSource.value || "";
    }});
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
    preparedRefreshButton.addEventListener("click", loadPreparedList);
    chatSendButton.addEventListener("click", sendChatMessage);
    chatClearButton.addEventListener("click", () => {{
      chatState.messages = [];
      renderChat();
      chatStatus.textContent = "";
    }});
    chatInput.addEventListener("keydown", (event) => {{
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {{
        event.preventDefault();
        sendChatMessage();
      }}
    }});
    auditRefreshButton.addEventListener("click", loadAuditList);
    auditFilter.addEventListener("input", () => {{
      if (!auditPanel.hidden) loadAuditList();
    }});
    auditLimit.addEventListener("change", () => {{
      if (!auditPanel.hidden) loadAuditList();
    }});
    renderTranslationOptions();
  </script>
</body>
</html>"""


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/prepared")
async def prepared_list(request: Request) -> JSONResponse:
    require_prepare_auth(request)
    items = list_prepared_cache_entries(request)
    return JSONResponse({"count": len(items), "items": items})


@app.get("/monitor/system")
async def monitor_system(seconds: int = Query(3600, ge=60, le=86400)) -> JSONResponse:
    cutoff = int(time.time()) - seconds
    history = [sample for sample in _system_metrics if int(sample.get("timestamp") or 0) >= cutoff]
    current = history[-1] if history else collect_system_metrics()
    return JSONResponse(
        {
            "interval_seconds": settings.system_metrics_interval_seconds,
            "history_seconds": settings.system_metrics_history_seconds,
            "current": current,
            "history": history,
        }
    )


def _translation_audit_files() -> list[Path]:
    root = settings.translation_audit_dir
    if not root.exists():
        return []
    files = [path for path in root.glob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files


def _read_jsonl_lines(path: Path, limit: int | None = None) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    records: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _list_cached_variants_for_video(request: Request, video_id: str) -> list[dict]:
    roots = [settings.cache_hot_dir]
    if settings.cache_archive_dir is not None:
        roots.append(settings.cache_archive_dir)
    by_key: dict[str, dict] = {}
    for root in roots:
        if root is None or not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            source_meta = read_json_file(child / "source.json")
            if source_meta.get("video_id") != video_id:
                continue
            subtitle_meta = source_meta.get("subtitle_meta") if isinstance(source_meta.get("subtitle_meta"), dict) else {}
            key = child.name
            source_lang = subtitle_meta.get("source_language")
            requested_lang = subtitle_meta.get("requested_language")
            translated = bool(subtitle_meta.get("translated"))
            engine = str(subtitle_meta.get("translation_engine_requested") or subtitle_meta.get("translation_engine") or "").strip()
            model = str(subtitle_meta.get("translation_model") or "").strip()
            label = f"{source_lang or 'unknown'} → {requested_lang or 'unknown'}"
            if translated:
                label += f" / {subtitle_translation_service_label(subtitle_meta) or engine or 'translation'}"
            outputs = []
            source_playable = False
            source_video_rel = source_meta.get("source_video")
            if isinstance(source_video_rel, str) and source_video_rel:
                source_video_path = child / source_video_rel
                source_playable = is_usable_file(source_video_path)
            if is_usable_file(child / "output.mp4"):
                outputs.append({"mode": "mp4", "url": prepared_media_url(request, video_id, str(source_meta.get("lang") or "ja"), "mp4", source_lang, engine)})
            playlist = child / "hls" / "index.m3u8"
            if is_usable_file(playlist) and "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8", errors="ignore"):
                outputs.append({"mode": "hls", "url": prepared_media_url(request, video_id, str(source_meta.get("lang") or "ja"), "hls", source_lang, engine)})
            entry = {
                "key": key,
                "video_id": video_id,
                "lang": source_meta.get("lang"),
                "storage": "hot" if root == settings.cache_hot_dir else "archive",
                "title": source_meta.get("title") or video_id,
                "source_url": source_meta.get("webpage_url") or youtube_watch_url(video_id),
                "source_language": source_lang,
                "requested_language": requested_lang,
                "translated": translated,
                "translation_engine": engine,
                "translation_model": model,
                "label": label,
                "outputs": outputs,
                "source_ready": source_playable,
            }
            existing = by_key.get(key)
            if existing is None or existing.get("storage") != "hot":
                by_key[key] = entry
    variants = list(by_key.values())
    variants.sort(key=lambda item: (str(item.get("requested_language") or ""), str(item.get("source_language") or ""), str(item.get("translation_engine") or "")))
    return variants


@app.get("/translation-audit")
async def translation_audit_index() -> JSONResponse:
    items = []
    for path in _translation_audit_files():
        records = _read_jsonl_lines(path)
        first = records[0] if records else {}
        last = records[-1] if records else {}
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "updated_at": int(path.stat().st_mtime),
                "request_count": sum(1 for rec in records if str(rec.get("event") or "") == "request"),
                "response_count": sum(1 for rec in records if str(rec.get("event") or "") == "response"),
                "error_count": sum(1 for rec in records if str(rec.get("event") or "") == "error"),
                "video_id": first.get("video_id") or last.get("video_id"),
                "lang": first.get("target_language") or last.get("target_language"),
                "model_name": first.get("model_name") or last.get("model_name"),
                "provider": first.get("provider") or last.get("provider"),
                "source_language": first.get("source_language") or last.get("source_language"),
                "sample_prompt": first.get("prompt") or "",
                "sample_response": last.get("response") or "",
            }
        )
    return JSONResponse({"count": len(items), "items": items})


@app.get("/translation-audit/{name}")
async def translation_audit_detail(name: str, limit: int = Query(200, ge=1, le=2000)) -> JSONResponse:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise HTTPException(status_code=400, detail="Invalid audit file name")
    path = settings.translation_audit_dir / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Audit file not found")
    records = _read_jsonl_lines(path, limit=limit)
    return JSONResponse({"name": name, "path": str(path), "count": len(records), "records": records})


@app.get("/prepare/youtube/{video_id}/variants")
async def prepare_youtube_variants(video_id: str, request: Request) -> JSONResponse:
    require_prepare_auth(request)
    validate_input(video_id, "ja")
    return JSONResponse({"video_id": video_id, "variants": _list_cached_variants_for_video(request, video_id)})


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    require_prepare_auth(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")
    result = await run_chat_completion(payload)
    return JSONResponse(result)


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
    if source_type not in {"auto", "playlist", "channel", "videos"}:
        raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, channel, or videos")
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
    playlist = prepared_hls_playlist_path(key)
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
    archive_immediately: bool = Query(False, alias="archiveImmediately"),
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
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")
    await cleanup_expired_cache_async()
    status_code, body = await enqueue_prepare_job(
        request,
        video_id,
        lang,
        mode,
        discord_user_id,
        subtitle_source_lang=subtitle_source_lang,
        translation_engine=translation_engine,
        archive_immediately=archive_immediately,
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
    body = subtitle_choice_body(info, lang)
    if body.get("requires_choice"):
        llm_available, llm_error = await remote_llm_available()
        body = restrict_translation_engines(body, llm_available=llm_available, llm_error=llm_error)
    return JSONResponse(body)


@app.get("/prepare/youtube/{video_id}/{lang}/{source_lang}/{translation_engine}/subtitle-events")
async def prepared_subtitle_events(
    video_id: str,
    lang: str,
    source_lang: str,
    translation_engine: str,
    request: Request,
) -> JSONResponse:
    require_prepare_auth(request)
    validate_input(video_id, lang)
    normalized_engine = validate_translation_variant(source_lang, translation_engine)
    key = cache_key(video_id, lang, source_lang, normalized_engine)
    return JSONResponse(
        {
            "video_id": video_id,
            "lang": lang,
            "source_lang": source_lang,
            "translation_engine": normalized_engine,
            "subtitle": read_subtitle_meta(key),
            "events": subtitle_events_for_key(key),
        }
    )


@app.get("/prepare/subtitle-events/{key}")
async def prepared_subtitle_events_by_key(key: str, request: Request) -> JSONResponse:
    require_prepare_auth(request)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        raise HTTPException(status_code=400, detail="Invalid cache key")
    subtitle_meta = read_subtitle_meta(key)
    source_lang, translation_engine = prepared_variant_from_meta(subtitle_meta)
    return JSONResponse(
        {
            "key": key,
            "subtitle": subtitle_meta,
            "source_lang": source_lang,
            "translation_engine": translation_engine,
            "events": subtitle_events_for_key(key),
        }
    )


@app.get("/prepare/subtitle-bundle/{key}")
async def prepared_subtitle_bundle_by_key(key: str, request: Request) -> JSONResponse:
    require_prepare_auth(request)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        raise HTTPException(status_code=400, detail="Invalid cache key")
    return JSONResponse(prepared_subtitle_bundle_for_key(key))


@app.get("/prepare/jobs/{job_id}")
async def prepare_job_status(job_id: str, request: Request) -> JSONResponse:
    require_prepare_auth(request)
    job = _prepare_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Prepare job not found")
    return JSONResponse(job_response_body(job_id, job, request))


@app.get("/prepared/{key}/source.mp4")
async def prepared_source_video(key: str, request: Request) -> Response:
    require_prepare_auth(request)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        raise HTTPException(status_code=400, detail="Invalid cache key")
    existing = check_existing_source_video(key)
    if not existing:
        raise HTTPException(status_code=404, detail="Source video is not prepared")
    video_path, _source_meta, _base_dir = existing
    return mp4_response(request, video_path)


@app.post("/prepare/eta/reset")
async def reset_prepare_eta(request: Request) -> JSONResponse:
    require_prepare_auth(request, allow_temp_key=False)
    metrics_manager.reset()
    return JSONResponse({"message": "予想時間の学習データをリセットしました。"})


@app.post("/prepare/archive-all")
async def archive_all_youtube(request: Request) -> JSONResponse:
    require_prepare_auth(request, allow_temp_key=False)
    async with _inflight_lock:
        async with _prepare_lock:
            active_keys = active_prepare_cache_keys()
    async with _cleanup_lock:
        result = await asyncio.to_thread(archive_all_hot_entries, active_keys)
    mib = result["freed_bytes"] / (1024 * 1024)
    message = (
        f"HDDへ移動しました: moved {result['moved']} 件、"
        f"skipped {result['skipped']} 件、failed {result['failed']} 件、"
        f"SSD解放 約{mib:,.1f} MiB"
    )
    return JSONResponse({"status": "ok", "message": message, **result})


@app.post("/prepare/youtube-batch/{lang}")
async def prepare_youtube_batch(
    lang: str,
    request: Request,
    source: str = Query(...),
    source_type: str = Query("auto", alias="sourceType"),
    mode: str = Query("mp4"),
    max_items: int = Query(5000, alias="maxItems"),
    discord_user_id: str | None = Query(None, alias="discordUserId"),
    archive_immediately: bool = Query(False, alias="archiveImmediately"),
) -> JSONResponse:
    require_prepare_auth(request)
    if not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid language code")
    if mode not in {"mp4", "hls"}:
        raise HTTPException(status_code=400, detail="mode must be mp4 or hls")
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")
    if source_type not in {"auto", "playlist", "channel", "videos"}:
        raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, channel, or videos")
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
        archive_immediately,
    )
    return JSONResponse(body, status_code=status_code)


@app.post("/prepare/youtube-reburn-batch/{lang}")
async def prepare_youtube_reburn_batch(
    lang: str,
    request: Request,
    source: str = Query(...),
    source_type: str = Query("auto", alias="sourceType"),
    mode: str = Query("mp4"),
    max_items: int = Query(5000, alias="maxItems"),
    discord_user_id: str | None = Query(None, alias="discordUserId"),
    archive_immediately: bool = Query(False, alias="archiveImmediately"),
) -> JSONResponse:
    require_prepare_auth(request)
    if not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid language code")
    if mode not in {"mp4", "hls"}:
        raise HTTPException(status_code=400, detail="mode must be mp4 or hls")
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")
    if source_type not in {"auto", "playlist", "channel", "videos"}:
        raise HTTPException(status_code=400, detail="sourceType must be auto, playlist, channel, or videos")
    max_items = normalize_max_items(max_items)
    discord_user_id = validate_discord_user_id(discord_user_id)
    await cleanup_expired_cache_async()
    status_code, body = await enqueue_reburn_batch(
        request,
        source,
        source_type,
        lang,
        mode,
        max_items,
        discord_user_id,
        archive_immediately,
    )
    return JSONResponse(body, status_code=status_code)


@app.post("/prepare/youtube-reburn-all")
async def prepare_youtube_reburn_all(
    request: Request,
    lang: str | None = Query(None),
    mode: str = Query("mp4"),
    max_items: int = Query(5000, alias="maxItems"),
    discord_user_id: str | None = Query(None, alias="discordUserId"),
    archive_immediately: bool = Query(False, alias="archiveImmediately"),
) -> JSONResponse:
    require_prepare_auth(request)
    if lang in {"", "all", "*"}:
        lang = None
    if lang is not None and not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid language code")
    if mode not in {"mp4", "hls"}:
        raise HTTPException(status_code=400, detail="mode must be mp4 or hls")
    if archive_immediately and mode != "mp4":
        raise HTTPException(status_code=400, detail="archiveImmediately is supported only for mp4")
    max_items = normalize_max_items(max_items)
    discord_user_id = validate_discord_user_id(discord_user_id)
    await cleanup_expired_cache_async()
    status_code, body = await enqueue_reburn_all(
        request,
        lang,
        mode,
        max_items,
        discord_user_id,
        archive_immediately,
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
        archive_dir = archive_entry_dir(key)
        archive_path = archive_dir / "hls" / filename if archive_dir else None
        if archive_path is None or not archive_path.exists():
            raise HTTPException(status_code=404, detail="HLS asset not found")
        path = archive_path

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
