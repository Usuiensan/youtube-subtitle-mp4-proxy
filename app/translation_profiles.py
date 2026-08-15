"""Translation model profiles and their environment-backed defaults."""

from __future__ import annotations

from collections.abc import Callable


EnvGetter = Callable[[str, str | None], str | None]


def profile_models(getenv: EnvGetter) -> dict[str, str]:
    return {
        "qwen3_4b_instruct": (getenv("LOCAL_LLM_MODEL_QWEN3_4B_INSTRUCT", getenv("REMOTE_LLM_MODEL", "qwen3:4b-instruct")) or "").strip(),
        "qwen3_8b": (getenv("LOCAL_LLM_MODEL_QWEN3_8B", "qwen3:8b") or "").strip(),
        "qwen3_14b": (getenv("LOCAL_LLM_MODEL_QWEN3_14B", "qwen3:14b") or "").strip(),
        "aya_expanse_8b": (getenv("LOCAL_LLM_MODEL_AYA_EXPANSE_8B", "aya-expanse:8b") or "").strip(),
        "gemma3_12b": (getenv("LOCAL_LLM_MODEL_GEMMA3_12B", "gemma3:12b") or "").strip(),
        "translategemma_12b": (getenv("LOCAL_LLM_MODEL_TRANSLATEGEMMA_12B", "translategemma:12b") or "").strip(),
        "gemini_2_5_flash": (getenv("LOCAL_LLM_MODEL_GEMINI_2_5_FLASH", "gemini-2.5-flash") or "").strip(),
        "gemini_2_5_flash_lite": "gemini-3.1-flash-lite",
        "gpt_5_nano": (getenv("OPENAI_MODEL", "gpt-5-nano") or "").strip(),
        "groq_gpt_oss_20b": (getenv("GROQ_MODEL", "openai/gpt-oss-20b") or "").strip(),
        "local_llm": (getenv("LOCAL_LLM_MODEL", "qwen3:4b-instruct") or "").strip(),
        "remote_llm": (getenv("REMOTE_LLM_MODEL", getenv("LOCAL_LLM_MODEL", "qwen3:4b-instruct")) or "").strip(),
    }


def profile_labels(getenv: EnvGetter) -> dict[str, str]:
    return {
        "qwen3_4b_instruct": "Qwen 3 4B Instruct",
        "qwen3_8b": "Qwen 3 8B",
        "qwen3_14b": "Qwen 3 14B",
        "aya_expanse_8b": "Aya Expanse 8B",
        "gemma3_12b": "Gemma 3 12B",
        "translategemma_12b": "TranslateGemma 12B",
        "gemini_2_5_flash": "Gemini Flash",
        "gemini_2_5_flash_lite": "Gemini 3.1 Flash-Lite",
        "gpt_5_nano": "GPT-5 nano",
        "groq_gpt_oss_20b": "Groq GPT-OSS 20B",
        "local_llm": getenv("LOCAL_LLM_LABEL", "Default LLM") or "Default LLM",
        "remote_llm": getenv("REMOTE_LLM_LABEL", "Remote LLM") or "Remote LLM",
    }
