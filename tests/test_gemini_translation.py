from __future__ import annotations

import unittest

from app import main as app_main
from bot import main as bot_main


class GeminiTranslationTests(unittest.TestCase):
    def test_gemini_profile_uses_gemini_provider(self) -> None:
        settings = app_main.translation_settings("gemini_2_5_flash")
        self.assertEqual(settings.model_name, app_main.settings.local_llm_profile_models["gemini_2_5_flash"])
        self.assertEqual(settings.provider_name, "gemini_api")

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


if __name__ == "__main__":
    unittest.main()
