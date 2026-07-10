from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import dataclass
from threading import Lock
from typing import Any

try:
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
except Exception:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    AutoModelForSeq2SeqLM = None  # type: ignore[assignment]
    AutoTokenizer = None  # type: ignore[assignment]

from app.nllb_provider import TranslationError, is_oom_error, normalize_japanese_subtitle


logger = logging.getLogger(__name__)


def normalize_opus_language(code: str) -> str:
    normalized = code.strip().replace("_", "-").lower()
    parts = [part for part in normalized.split("-") if part]
    if len(parts) >= 2 and len(parts[0]) == 2:
        return parts[0]
    return normalized


def validate_opus_language_pair(source_language: str, target_language: str) -> tuple[str, str]:
    source = normalize_opus_language(source_language)
    target = normalize_opus_language(target_language)
    if source != "en" or target != "ja":
        raise TranslationError(
            f"Helsinki-NLP/opus-mt-en-jap supports only English to Japanese: source={source_language} target={target_language}"
        )
    return source, target


@dataclass
class OpusMtConfig:
    model_name: str
    device: str
    batch_size: int
    max_input_tokens: int
    max_new_tokens: int
    num_beams: int
    keep_loaded: bool


class OpusMtTranslationProvider:
    _load_lock = Lock()
    _instance: "OpusMtTranslationProvider | None" = None

    def __init__(self, *, config: OpusMtConfig, tokenizer: Any, model: Any, device: str, dtype: Any) -> None:
        self.config = config
        self.tokenizer = tokenizer
        self.model = model
        self.device = device
        self.dtype = dtype

    @classmethod
    def load(cls, config: OpusMtConfig) -> "OpusMtTranslationProvider":
        if cls._instance is not None:
            return cls._instance
        with cls._load_lock:
            if cls._instance is not None:
                return cls._instance
            if torch is None or AutoTokenizer is None or AutoModelForSeq2SeqLM is None:
                raise TranslationError("transformers/torch are not available")
            device = cls._resolve_device(config.device)
            dtype = torch.float16 if device == "cuda" else torch.float32
            logger.info(
                "loading Opus MT model=%s device=%s dtype=%s",
                config.model_name,
                device,
                str(dtype).split(".")[-1],
            )
            tokenizer = AutoTokenizer.from_pretrained(config.model_name)
            model = AutoModelForSeq2SeqLM.from_pretrained(
                config.model_name,
                torch_dtype=dtype,
                low_cpu_mem_usage=True,
            )
            model = model.to(device)
            model.eval()
            cls._instance = cls(config=config, tokenizer=tokenizer, model=model, device=device, dtype=dtype)
            logger.info(
                "loaded Opus MT model=%s device=%s dtype=%s",
                config.model_name,
                device,
                str(dtype).split(".")[-1],
            )
            return cls._instance

    @classmethod
    def _resolve_device(cls, device: str) -> str:
        normalized = device.strip().lower()
        if normalized == "cpu":
            return "cpu"
        if normalized == "cuda":
            if torch is None or not torch.cuda.is_available():
                raise TranslationError("CUDA is not available for Opus MT")
            return "cuda"
        if normalized == "auto":
            if torch is not None and torch.cuda.is_available():
                return "cuda"
            return "cpu"
        raise TranslationError(f"Invalid Opus MT device: {device}")

    @classmethod
    def unload(cls) -> None:
        with cls._load_lock:
            instance = cls._instance
            cls._instance = None
        if instance is None:
            return
        try:
            del instance.model
            del instance.tokenizer
        except Exception:
            pass
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        if not texts:
            return []
        validate_opus_language_pair(source_language, target_language)
        return self._translate_recursive([text or "" for text in texts])

    def _translate_recursive(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        try:
            return self._translate_once(texts)
        except Exception as error:
            if not is_oom_error(error):
                raise
            if torch is not None and torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            if len(texts) <= 1:
                raise TranslationError("Opus MT CUDA OOM on single-item batch") from error
            midpoint = len(texts) // 2
            return self._translate_recursive(texts[:midpoint]) + self._translate_recursive(texts[midpoint:])

    def _translate_once(self, texts: list[str]) -> list[str]:
        tokenizer = self.tokenizer
        model = self.model
        inputs = tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.config.max_input_tokens,
        )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                num_beams=self.config.num_beams,
                max_new_tokens=self.config.max_new_tokens,
            )
        if self.device == "cuda" and torch is not None:
            torch.cuda.synchronize()
        outputs = tokenizer.batch_decode(generated, skip_special_tokens=True)
        if len(outputs) != len(texts):
            raise TranslationError("Opus MT output count did not match input count")
        return [normalize_japanese_subtitle(text) for text in outputs]


async def translate_texts_async(
    provider: OpusMtTranslationProvider,
    texts: list[str],
    source_language: str,
    target_language: str,
) -> list[str]:
    return await asyncio.to_thread(provider.translate_batch, texts, source_language, target_language)
