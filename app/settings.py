"""Environment-backed application settings.

This module intentionally keeps the existing environment variable names and
defaults stable while isolating configuration assembly from the API module.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config_files import (
    default_translation_prompt_file,
    default_translategemma_prompt_file,
    read_text_file,
)
from app.translation_profiles import profile_labels, profile_models


class Settings:
    cache_dir = Path(os.getenv("CACHE_DIR", "/tmp/youtube-mp4-cache"))
    cache_hot_dir = Path(os.getenv("CACHE_HOT_DIR", os.getenv("CACHE_DIR", "/tmp/youtube-mp4-cache")))
    cache_archive_dir = Path(os.environ["CACHE_ARCHIVE_DIR"]) if os.getenv("CACHE_ARCHIVE_DIR") else None
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
    subtitle_font_size = int(os.getenv("SUBTITLE_FONT_SIZE", "22"))
    subtitle_margin_v = int(os.getenv("SUBTITLE_MARGIN_V", "34"))
    subtitle_margin_l = int(os.getenv("SUBTITLE_MARGIN_L", "24"))
    subtitle_margin_r = int(os.getenv("SUBTITLE_MARGIN_R", "24"))
    subtitle_primary_colour = os.getenv("SUBTITLE_PRIMARY_COLOUR", "&H00FFFFFF")
    subtitle_back_colour = os.getenv("SUBTITLE_BACK_COLOUR", "&H80000000")
    hls_segment_seconds = int(os.getenv("HLS_SEGMENT_SECONDS", "6"))
    hls_ready_timeout_seconds = int(os.getenv("HLS_READY_TIMEOUT_SECONDS", "1800"))
    ffmpeg_encode_concurrency = max(1, int(os.getenv("FFMPEG_ENCODE_CONCURRENCY", "1")))
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
    local_llm_profile_models = profile_models(os.getenv)
    local_llm_profile_labels = profile_labels(os.getenv)
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
    translation_prompt_template = read_text_file(os.getenv("TRANSLATION_PROMPT_TEMPLATE_FILE")) or read_text_file(str(default_translation_prompt_file())) or os.getenv("TRANSLATION_PROMPT_TEMPLATE", "")
    translategemma_prompt_template = read_text_file(os.getenv("TRANSLATEGEMMA_PROMPT_TEMPLATE_FILE")) or read_text_file(str(default_translategemma_prompt_file())) or os.getenv("TRANSLATEGEMMA_PROMPT_TEMPLATE", "")
    google_cloud_project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_billing_mode = os.getenv("GEMINI_BILLING_MODE", "free_tier").strip().lower()
    gemini_flash_input_price_per_million = float(os.getenv("GEMINI_FLASH_INPUT_PRICE_PER_MILLION", "0.30"))
    gemini_flash_output_price_per_million = float(os.getenv("GEMINI_FLASH_OUTPUT_PRICE_PER_MILLION", "2.50"))
    gemini_max_requests_per_job = max(0, int(os.getenv("GEMINI_MAX_REQUESTS_PER_JOB", "3")))
    gemini_fallback_profile = os.getenv("GEMINI_FALLBACK_PROFILE", "qwen3_4b_instruct").strip().lower()
    google_translate_free_chars_per_month = int(os.getenv("GOOGLE_TRANSLATE_FREE_CHARS_PER_MONTH", "500000"))
    google_translate_price_usd_per_million_chars = float(os.getenv("GOOGLE_TRANSLATE_PRICE_USD_PER_MILLION_CHARS", "20.0"))
    usd_to_jpy_rate = float(os.getenv("USD_TO_JPY_RATE", "160.0"))
    translation_provider = os.getenv("TRANSLATION_PROVIDER", "qwen3_4b_instruct").strip().lower()
    translation_failure_dir = Path(os.getenv("TRANSLATION_FAILURE_DIR", str(cache_hot_dir / ".translation-attempts")))
    translation_audit_dir = Path(os.getenv("TRANSLATION_AUDIT_DIR", str(cache_hot_dir / ".translation-audit")))
    google_translation_usage_file = Path(os.getenv("GOOGLE_TRANSLATION_USAGE_FILE", str(cache_hot_dir / "google-translation-usage.json")))
    system_metrics_enabled = os.getenv("SYSTEM_METRICS_ENABLED", "1") != "0"
    system_metrics_interval_seconds = float(os.getenv("SYSTEM_METRICS_INTERVAL_SECONDS", "5"))
    system_metrics_history_seconds = int(os.getenv("SYSTEM_METRICS_HISTORY_SECONDS", "86400"))
    system_metrics_file = Path(os.getenv("SYSTEM_METRICS_FILE", str(cache_hot_dir / "system-metrics.jsonl")))
