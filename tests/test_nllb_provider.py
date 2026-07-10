from __future__ import annotations

import contextlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from app import main as app_main
from app import nllb_provider as nllb


class FakeProvider(nllb.NllbTranslationProvider):
    def __init__(self) -> None:
        pass

    def _translate_once(self, texts, source_code, target_code):  # type: ignore[override]
        if len(texts) > 1:
            raise RuntimeError("CUDA out of memory")
        return [f"{text}-ja" for text in texts]


class NllbProviderTests(unittest.TestCase):
    def tearDown(self) -> None:
        nllb.NllbTranslationProvider._instance = None

    def test_language_mapping(self) -> None:
        self.assertEqual(nllb.nllb_language_code("ja"), "jpn_Jpan")
        self.assertEqual(nllb.nllb_language_code("en"), "eng_Latn")
        self.assertEqual(nllb.nllb_language_code("en-US"), "eng_Latn")
        self.assertEqual(nllb.nllb_language_code("ja-JP"), "jpn_Jpan")
        self.assertEqual(nllb.nllb_language_code("zh-Hans"), "zho_Hans")
        self.assertEqual(nllb.nllb_language_code("zh-CN"), "zho_Hans")

    def test_unsupported_language_raises(self) -> None:
        with self.assertRaises(nllb.TranslationError):
            nllb.nllb_language_code("xx")

    def test_japanese_normalization(self) -> None:
        self.assertEqual(nllb.normalize_japanese_subtitle("翻訳です."), "翻訳です。")
        self.assertEqual(nllb.normalize_japanese_subtitle("URL https://example.com"), "URL https://example.com")

    def test_recursive_oom_split(self) -> None:
        provider = FakeProvider()
        result = provider._translate_recursive(["a", "b", "c", "d"], "eng_Latn", "jpn_Jpan")
        self.assertEqual(result, ["a-ja", "b-ja", "c-ja", "d-ja"])

    def test_order_preserved(self) -> None:
        class EchoProvider(nllb.NllbTranslationProvider):
            def __init__(self) -> None:
                pass

            def _translate_once(self, texts, source_code, target_code):  # type: ignore[override]
                return [f"{index}:{text}" for index, text in enumerate(texts)]

        provider = EchoProvider()
        result = provider._translate_recursive(["x", "y"], "eng_Latn", "jpn_Jpan")
        self.assertEqual(result, ["0:x", "1:y"])

    def test_singleton_load_only_once(self) -> None:
        state = {"tokenizer": 0, "model": 0}

        class FakeInputs(dict):
            def to(self, device):
                return self

        class FakeTokenizer:
            lang_code_to_id = {"eng_Latn": 1, "jpn_Jpan": 2}

            @classmethod
            def from_pretrained(cls, model_name):
                state["tokenizer"] += 1
                return cls()

            def __call__(self, texts, return_tensors, padding, truncation, max_length):
                return FakeInputs({"input_ids": object()})

            def batch_decode(self, generated, skip_special_tokens=True):
                return list(generated)

        class FakeModel:
            @classmethod
            def from_pretrained(cls, model_name, torch_dtype, low_cpu_mem_usage):
                state["model"] += 1
                return cls()

            def to(self, device):
                return self

            def eval(self):
                return self

        fake_torch = SimpleNamespace(
            float16="float16",
            float32="float32",
            cuda=SimpleNamespace(
                is_available=lambda: False,
                empty_cache=lambda: None,
                synchronize=lambda: None,
            ),
            inference_mode=lambda: contextlib.nullcontext(),
        )

        with patch.object(nllb, "torch", fake_torch), patch.object(nllb, "AutoTokenizer", FakeTokenizer), patch.object(nllb, "AutoModelForSeq2SeqLM", FakeModel):
            config = nllb.NllbConfig(
                model_name="facebook/nllb-200-distilled-600M",
                device="cpu",
                batch_size=16,
                max_input_tokens=32,
                max_new_tokens=16,
                num_beams=1,
                keep_loaded=True,
            )

            with ThreadPoolExecutor(max_workers=5) as pool:
                instances = list(pool.map(lambda _: nllb.NllbTranslationProvider.load(config), range(5)))

        self.assertTrue(all(instance is instances[0] for instance in instances))
        self.assertEqual(state["tokenizer"], 1)
        self.assertEqual(state["model"], 1)

    def test_provider_selection(self) -> None:
        values = [item["value"] for item in app_main.translation_profile_options()]
        self.assertIn("nllb", values)
        self.assertEqual(app_main.normalize_translation_engine("nllb"), "nllb")
        self.assertEqual(app_main.translation_settings("nllb").model_name, app_main.settings.nllb_model)


if __name__ == "__main__":
    unittest.main()
