from __future__ import annotations

import unittest

from app import main as app_main
from app import opus_mt_provider as opus


class OpusMtProviderTests(unittest.TestCase):
    def test_language_pair_validation(self) -> None:
        self.assertEqual(opus.validate_opus_language_pair("en-US", "ja-JP"), ("en", "ja"))
        with self.assertRaises(opus.TranslationError):
            opus.validate_opus_language_pair("ko", "ja")
        with self.assertRaises(opus.TranslationError):
            opus.validate_opus_language_pair("en", "fr")

    def test_engine_selection(self) -> None:
        self.assertEqual(app_main.normalize_translation_engine("opus_mt_en_jap"), "opus_mt_en_jap")
        settings = app_main.translation_settings("opus_mt_en_jap")
        self.assertEqual(settings.model_name, app_main.settings.opus_mt_model)


if __name__ == "__main__":
    unittest.main()
