from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import main as app_main
from app import translation_worker
from bot import main as bot_main
from unittest.mock import patch


class GeminiTranslationTests(unittest.TestCase):
    def test_translation_target_selection_has_no_engine_select(self) -> None:
        view = bot_main.SubtitleChoiceView(
            requester_id=1,
            video_id="video",
            lang="ja",
            mode="mp4",
            options_body={
                "candidates": [{"language": "en", "name": "英語"}],
            },
        )
        view.target_select._values = ["ja"]

        class Response:
            def __init__(self) -> None:
                self.kwargs = None

            async def edit_message(self, **kwargs):
                self.kwargs = kwargs

        class Interaction:
            def __init__(self) -> None:
                self.response = Response()

        interaction = Interaction()
        asyncio.run(view.on_target_selected(interaction))

        self.assertFalse(hasattr(view, "engine_select"))
        self.assertIs(interaction.response.kwargs["view"], view)

    def test_translation_select_callbacks_redraw_and_restore_engine(self) -> None:
        view = bot_main.SubtitleChoiceView(
            requester_id=1,
            video_id="video",
            lang="ja",
            mode="mp4",
            options_body={
                "candidates": [{"language": "en", "name": "英語"}],
            },
        )

        class Response:
            def __init__(self) -> None:
                self.kwargs = None

            async def edit_message(self, **kwargs):
                self.kwargs = kwargs

        class Interaction:
            def __init__(self) -> None:
                self.response = Response()

        interaction = Interaction()
        view.source_select._values = ["en"]
        asyncio.run(view.on_source_selected(interaction))
        self.assertTrue(view.source_select.options[0].default)
        self.assertIs(interaction.response.kwargs["view"], view)

        view.target_select._values = ["ja"]
        asyncio.run(view.on_target_selected(interaction))

        view.target_select._values = ["same"]
        asyncio.run(view.on_target_selected(interaction))
        self.assertEqual(view.target_lang, "same")

        view.target_select._values = ["ja"]
        asyncio.run(view.on_target_selected(interaction))
        self.assertEqual(view.target_lang, "ja")

    def test_translation_audit_record_decodes_provider_and_model_json(self) -> None:
        record = app_main._decode_translation_audit_record(
            {
                "event": "provider_response",
                "request_body": '{"generationConfig":{"responseMimeType":"application/json"}}',
                "response_body": json.dumps(
                    {
                        "candidates": [
                            {"content": {"parts": [{"text": '{"subtitles":[{"id":1,"text":"こんにちは"}]}' }]} }
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        )
        self.assertEqual(record["request_json"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(record["response_json"]["candidates"][0]["content"]["parts"][0]["text"], '{"subtitles":[{"id":1,"text":"こんにちは"}]}')
        self.assertEqual(record["model_response_json"]["subtitles"][0]["text"], "こんにちは")

    def test_gemini_profile_uses_gemini_provider(self) -> None:
        settings = app_main.translation_settings("gemini_2_5_flash")
        self.assertEqual(settings.model_name, app_main.settings.local_llm_profile_models["gemini_2_5_flash"])
        self.assertEqual(settings.provider_name, "gemini_api")

    def test_gemini_thinking_level_is_added_to_generation_config(self) -> None:
        requests: list[dict] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"candidates":[{"content":{"parts":[{"text":"{\\"subtitles\\":[{\\"id\\":1,\\"text\\":\\"Hi\\"}]}"}]}}],"usageMetadata":{}}'

        def urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return Response()

        with patch.object(translation_worker.urllib.request, "urlopen", urlopen):
            translation_worker.translate_batch_gemini(
                {
                    "llm_endpoint": "https://example.invalid/generateContent",
                    "llm_api_key": "test-key",
                    "model_name": "gemini-3.1-flash-lite",
                    "source_language": "en",
                    "target_language": "ja",
                    "subtitles": [{"id": 1, "text": "Hi"}],
                    "gemini_thinking_level": "high",
                }
            )

        self.assertEqual(requests[0]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "high"})

    def test_gemini_availability_does_not_require_remote_llm_endpoint(self) -> None:
        with patch.object(app_main.settings, "remote_llm_endpoint", ""), patch.object(
            app_main.settings, "gemini_api_key", "test-key"
        ):
            available, error, models = asyncio.run(app_main.remote_llm_status())

        self.assertTrue(available)
        self.assertIsNone(error)
        self.assertIn(app_main.settings.local_llm_profile_models["gemini_2_5_flash_lite"], models)

    def test_translation_profile_options_expose_configured_profile_only(self) -> None:
        options = app_main.translation_profile_options()
        self.assertEqual([option["value"] for option in options], [app_main.configured_translation_engine()])
        self.assertTrue(options[0]["default"])

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
        self.assertIn("入力トークン: 14,208", text)
        self.assertIn("出力トークン: 16,731", text)
        self.assertIn("合計トークン: 30,939", text)
        self.assertIn("入力 $0.30 / 出力 $2.50", text)

    def test_google_cloud_translation_is_disabled(self) -> None:
        self.assertNotEqual(app_main.normalize_translation_engine(None), "google_cloud")
        with self.assertRaisesRegex(RuntimeError, "Google Cloud Translation is disabled"):
            app_main.translation_settings("google_cloud")

    def test_existing_translated_subtitle_usage_text_shows_no_extra_cost_and_estimate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "subtitle.ja.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n世界\n",
                encoding="utf-8",
            )
            metadata = app_main.enrich_existing_subtitle_usage_metadata(
                {
                    "requested_language": "ja",
                    "source_language": "ja",
                    "translated": False,
                    "source_kind": "manual",
                },
                subtitle,
            )

        text = bot_main.translation_usage_text(metadata)
        self.assertEqual(metadata["translation_characters"], 7)
        self.assertIn("翻訳エンジン: 出元字幕（翻訳なし）", text)
        self.assertIn("API料金: 追加費用なし（出元の字幕を使用）", text)
        self.assertIn("課金区分: API利用なし", text)
        self.assertNotIn("Google Cloud", text)

    def test_legacy_skipped_metadata_does_not_show_google_cloud(self) -> None:
        text = bot_main.translation_usage_text(
            {
                "translation_engine": "google_cloud",
                "translation_skipped": True,
                "translation_provider_label": "Google Cloud Translation",
                "translation_billing_class": "Cloud Translation Basic NMT",
                "translation_characters": 7,
                "translation_usage_estimate_usd": 0.0001,
                "translation_usage_estimate_jpy": 0.02,
            }
        )

        self.assertIn("翻訳エンジン: 出元字幕（翻訳なし）", text)
        self.assertIn("課金区分: API利用なし", text)
        self.assertNotIn("Google Cloud", text)
        self.assertNotIn("通常単価換算", text)

    def test_ready_status_includes_clickable_and_code_block_urls(self) -> None:
        text = bot_main.status_message(
            {
                "status": "ready",
                "url": "https://lab.usuiensan.dev/youtube/nXdVG45wveo/ja/en/google_cloud",
                "video_id": "nXdVG45wveo",
                "subtitle": {},
            }
        )
        self.assertIn("変換済み動画: https://lab.usuiensan.dev/youtube/nXdVG45wveo/ja/en/google_cloud", text)
        self.assertIn("元動画: https://www.youtube.com/watch?v=nXdVG45wveo", text)
        self.assertIn("```text\nhttps://lab.usuiensan.dev/youtube/nXdVG45wveo/ja/en/google_cloud\n```", text)

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

    def test_translation_worker_prompt_contains_full_subtitle_context(self) -> None:
        payload = {
            "video_title": "Sample title",
            "channel_name": "Sample channel",
            "description": "A sample description",
            "source_language": "en",
            "target_language": "ja",
            "subtitles": [
                {"id": 1, "text": "First line"},
                {"id": 2, "text": "Translate me"},
                {"id": 3, "text": "Last line"},
            ],
        }
        prompt = translation_worker.build_full_translation_prompt(payload)
        self.assertIn("Translate the complete subtitle list", prompt)
        self.assertIn("Default video title", prompt)
        self.assertIn("Sample title", prompt)
        self.assertIn("Sample channel", prompt)
        self.assertIn("A sample description", prompt)
        self.assertIn("First line", prompt)
        self.assertIn("Translate me", prompt)
        self.assertIn("Last line", prompt)

    def test_gemini_request_and_raw_response_are_saved_without_api_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "translation.jsonl"
            raw_response = '{"candidates": [{"content": {"parts": [{"text": "{\\"subtitles\\":[{\\"id\\":1,\\"text\\":\\"Hi\\"}]}"}]}}]}'

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return raw_response.encode("utf-8")

            with patch.object(translation_worker.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()):
                translation_worker.translate_batch_gemini(
                    {
                        "llm_api_key": "secret-key",
                        "model_name": "gemini-2.5-flash-lite",
                        "source_language": "en",
                        "target_language": "ja",
                        "subtitles": [{"id": 1, "text": "Hi"}],
                        "_translation_audit_path": str(audit_path),
                    }
                )

            events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            request_event = next(event for event in events if event["event"] == "provider_request")
            response_event = next(event for event in events if event["event"] == "provider_response")
            self.assertEqual(request_event["request_body"]["contents"][0]["role"], "user")
            self.assertEqual(response_event["response_body"], raw_response)
            self.assertNotIn("secret-key", audit_path.read_text(encoding="utf-8"))

    def test_gemini_http_error_response_is_saved(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "translation.jsonl"
            error_body = b'{"error":{"message":"quota exceeded"}}'

            def urlopen(*_args, **_kwargs):
                raise urllib.error.HTTPError(
                    "https://example.invalid", 429, "Too Many Requests", {}, io.BytesIO(error_body)
                )

            with patch.object(translation_worker.urllib.request, "urlopen", urlopen):
                with self.assertRaises(RuntimeError):
                    translation_worker.translate_batch_gemini(
                        {
                            "llm_api_key": "secret-key",
                            "model_name": "gemini-2.5-flash-lite",
                            "subtitles": [{"id": 1, "text": "Hi"}],
                            "_translation_audit_path": str(audit_path),
                        }
                    )

            events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            error_event = next(event for event in events if event["event"] == "provider_error")
            self.assertEqual(error_event["http_status"], 429)
            self.assertEqual(error_event["response_body"], error_body.decode("utf-8"))

    def test_dm_command_parser_strips_slash_prefix(self) -> None:
        command, args = bot_main.parse_dm_command("/prepare https://youtu.be/dQw4w9WgXcQ lang=ja")
        self.assertEqual(command, "prepare")
        self.assertEqual(args[0], "https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(bot_main.parse_dm_flag(args, "lang"), "ja")
        self.assertTrue(bot_main.parse_dm_bool("yes"))


if __name__ == "__main__":
    unittest.main()
