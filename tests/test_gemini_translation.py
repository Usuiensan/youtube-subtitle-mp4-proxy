from __future__ import annotations

import unittest

from app import main as app_main
from app import translation_worker
from bot import main as bot_main


class GeminiTranslationTests(unittest.TestCase):
    def test_gemini_profile_uses_gemini_provider(self) -> None:
        settings = app_main.translation_settings("gemini_2_5_flash")
        self.assertEqual(settings.model_name, app_main.settings.local_llm_profile_models["gemini_2_5_flash"])
        self.assertEqual(settings.provider_name, "gemini_api")

    def test_translation_profiles_expose_multiple_llms(self) -> None:
        options = app_main.translation_profile_options()
        values = {option["value"] for option in options}
        self.assertIn("qwen3_4b_instruct", values)
        self.assertIn("qwen3_8b", values)
        self.assertIn("aya_expanse_8b", values)
        self.assertIn("gemini_2_5_flash", values)
        qwen_option = next(option for option in options if option["value"] == "qwen3_8b")
        self.assertEqual(qwen_option["label"], "Qwen 3 8B")
        self.assertEqual(qwen_option["model"], "qwen3:8b")
        self.assertEqual(app_main.translation_settings("qwen3_8b").provider_name, "openai_compatible")

    def test_enrich_translation_metadata_adds_cost_fields(self) -> None:
        metadata = app_main.enrich_translation_metadata(
            {
                "translation_engine": "gemini_2_5_flash",
                "translation_input_tokens": 14208,
                "translation_output_tokens": 16731,
                "translation_characters": 18420,
            }
        )
        self.assertEqual(metadata["translation_provider_label"], "Gemini Flash")
        self.assertEqual(metadata["translation_billing_class"], "Gemini API Free Tier")
        self.assertGreater(metadata["translation_overage_estimate_usd"], 0.0)
        self.assertGreater(metadata["translation_overage_estimate_jpy"], 0.0)

    def test_bot_translation_usage_text(self) -> None:
        text = bot_main.translation_usage_text(
            {
                "translation_engine": "gemini_2_5_flash",
                "translation_provider_label": "Gemini Flash",
                "translation_billing_class": "Gemini API Free Tier",
                "translation_characters": 18420,
                "translation_input_tokens": 14208,
                "translation_output_tokens": 16731,
                "translation_api_cost_jpy": 0.0,
                "translation_overage_estimate_usd": 0.0461,
                "translation_overage_estimate_jpy": 7.38,
            }
        )
        self.assertIn("Gemini Flash", text)
        self.assertIn("課金区分: Gemini API Free Tier", text)
        self.assertIn("翻訳文字数: 18,420文字", text)

    def test_bot_translation_status_uses_model_label(self) -> None:
        text = bot_main.subtitle_status_text(
            {
                "translated": True,
                "source_language": "en",
                "requested_language": "ja",
                "source_kind": "manual",
                "translation_engine": "qwen3_8b",
                "translation_model": "qwen3:8b",
            }
        )
        self.assertIn("Qwen 3 8B", text)
        self.assertIn("qwen3:8b", text)

    def test_translation_worker_prompt_is_single_subtitle_and_minimal(self) -> None:
        payload = {
            "video_title": "Sample title",
            "source_language": "en",
            "target_language": "ja",
            "context_before": [
                {"id": "1", "text": "First line"},
                {"id": "2", "text": "Second line"},
            ],
            "context_after": [
                {"id": "4", "text": "Fourth line"},
            ],
        }
        prompt = translation_worker.build_single_subtitle_prompt({"id": "3", "text": "Translate me"}, payload)
        self.assertIn("Translate exactly one subtitle", prompt)
        self.assertIn("Current subtitle:", prompt)
        self.assertIn("Translate me", prompt)
        self.assertNotIn("前5つの字幕", prompt)
        self.assertNotIn("これを訳せ（字幕１つだけ）", prompt)
        self.assertNotIn("previous_japanese", prompt)


if __name__ == "__main__":
    unittest.main()
