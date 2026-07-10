from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

try:
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
except Exception:  # pragma: no cover - optional in lightweight test envs
    torch = None  # type: ignore[assignment]
    AutoModelForSeq2SeqLM = None  # type: ignore[assignment]
    AutoTokenizer = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


class TranslationError(RuntimeError):
    pass


class TranslationProvider(Protocol):
    def translate_batch(
        self,
        texts: list[str],
        source_language: str,
        target_language: str,
    ) -> list[str]:
        ...


NLLB_LANGUAGE_MAP: dict[str, str] = {
    "ja": "jpn_Jpan",
    "en": "eng_Latn",
    "ko": "kor_Hang",
    "zh": "zho_Hans",
    "zh-cn": "zho_Hans",
    "zh-hans": "zho_Hans",
    "zh-tw": "zho_Hant",
    "zh-hant": "zho_Hant",
    "es": "spa_Latn",
    "pt": "por_Latn",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "it": "ita_Latn",
    "ru": "rus_Cyrl",
    "uk": "ukr_Cyrl",
    "pl": "pol_Latn",
    "tr": "tur_Latn",
    "ar": "arb_Arab",
    "fa": "pes_Arab",
    "hi": "hin_Deva",
    "bn": "ben_Beng",
    "id": "ind_Latn",
    "vi": "vie_Latn",
    "th": "tha_Thai",
}


def normalize_language_code(code: str) -> str:
    normalized = code.strip().replace("_", "-").lower()
    if normalized in {"zh-hans", "zh-hant", "zh-cn", "zh-tw"}:
        return normalized
    parts = [part for part in normalized.split("-") if part]
    if len(parts) >= 2 and len(parts[0]) == 2:
        return parts[0]
    return normalized


def nllb_language_code(code: str) -> str:
    normalized = normalize_language_code(code)
    mapped = NLLB_LANGUAGE_MAP.get(normalized)
    if mapped:
        return mapped
    if "-" in normalized:
        mapped = NLLB_LANGUAGE_MAP.get(normalized.split("-", 1)[0])
        if mapped:
            return mapped
    raise TranslationError(f"Unsupported NLLB language code: {code}")


def normalize_japanese_subtitle(text: str) -> str:
    text = text.strip()
    if text.endswith("."):
        text = text[:-1] + "。"
    return text


def is_oom_error(error: BaseException) -> bool:
    message = str(error).lower()
    return "out of memory" in message or "cuda oom" in message or "cublas" in message and "alloc" in message


@dataclass
class NllbConfig:
    model_name: str
    device: str
    batch_size: int
    max_input_tokens: int
    max_new_tokens: int
    num_beams: int
    keep_loaded: bool


class NllbTranslationProvider:
    _load_lock = Lock()
    _instance: "NllbTranslationProvider | None" = None

    def __init__(self, *, config: NllbConfig, tokenizer: Any, model: Any, device: str, dtype: Any) -> None:
        self.config = config
        self.tokenizer = tokenizer
        self.model = model
        self.device = device
        self.dtype = dtype

    @classmethod
    def load(cls, config: NllbConfig) -> "NllbTranslationProvider":
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
                "loading NLLB model=%s device=%s dtype=%s",
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
            if device == "cuda":
                model = model.to("cuda")
            else:
                model = model.to("cpu")
            model.eval()
            cls._instance = cls(
                config=config,
                tokenizer=tokenizer,
                model=model,
                device=device,
                dtype=dtype,
            )
            logger.info(
                "loaded NLLB model=%s device=%s dtype=%s",
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
                raise TranslationError("CUDA is not available for NLLB")
            return "cuda"
        if normalized == "auto":
            if torch is not None and torch.cuda.is_available():
                return "cuda"
            return "cpu"
        raise TranslationError(f"Invalid NLLB device: {device}")

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

    def translate_batch(
        self,
        texts: list[str],
        source_language: str,
        target_language: str,
    ) -> list[str]:
        if not texts:
            return []
        source_code = nllb_language_code(source_language)
        target_code = nllb_language_code(target_language)
        cleaned = [text or "" for text in texts]
        return self._translate_recursive(cleaned, source_code, target_code)

    def _translate_recursive(
        self,
        texts: list[str],
        source_code: str,
        target_code: str,
    ) -> list[str]:
        if not texts:
            return []
        try:
            return self._translate_once(texts, source_code, target_code)
        except Exception as error:
            if not is_oom_error(error):
                raise
            if torch is not None and torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            if len(texts) <= 1:
                raise TranslationError("NLLB CUDA OOM on single-item batch") from error
            midpoint = len(texts) // 2
            left = self._translate_recursive(texts[:midpoint], source_code, target_code)
            right = self._translate_recursive(texts[midpoint:], source_code, target_code)
            return left + right

    def _translate_once(
        self,
        texts: list[str],
        source_code: str,
        target_code: str,
    ) -> list[str]:
        tokenizer = self.tokenizer
        model = self.model
        tokenizer.src_lang = source_code
        inputs = tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.config.max_input_tokens,
        )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        forced_bos_token_id = tokenizer.lang_code_to_id[target_code]
        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                forced_bos_token_id=forced_bos_token_id,
                num_beams=self.config.num_beams,
                max_new_tokens=self.config.max_new_tokens,
            )
        if self.device == "cuda" and torch is not None:
            torch.cuda.synchronize()
        outputs = tokenizer.batch_decode(generated, skip_special_tokens=True)
        if len(outputs) != len(texts):
            raise TranslationError("NLLB output count did not match input count")
        if target_code == "jpn_Jpan":
            outputs = [normalize_japanese_subtitle(text) for text in outputs]
        return outputs


async def translate_texts_async(
    provider: NllbTranslationProvider,
    texts: list[str],
    source_language: str,
    target_language: str,
) -> list[str]:
    return await asyncio.to_thread(provider.translate_batch, texts, source_language, target_language)
