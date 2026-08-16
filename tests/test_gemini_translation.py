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
from app.translation import TranslationError
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

    def test_single_source_defaults_to_japanese_without_source_select(self) -> None:
        view = bot_main.SubtitleChoiceView(
            requester_id=1,
            video_id="video",
            lang="ja",
            mode="mp4",
            options_body={"candidates": [{"language": "en", "name": "英語"}]},
        )

        self.assertEqual(view.source_lang, "en")
        self.assertEqual(view.target_lang, "ja")
        self.assertNotIn(view.source_select, view.children)
        self.assertIn(view.target_select, view.children)
        self.assertTrue(next(option for option in view.target_select.options if option.value == "ja").default)

    def test_translation_select_callbacks_redraw_and_restore_engine(self) -> None:
        view = bot_main.SubtitleChoiceView(
            requester_id=1,
            video_id="video",
            lang="ja",
            mode="mp4",
            options_body={
                "candidates": [
                    {"language": "en", "name": "英語"},
                    {"language": "ko", "name": "韓国語"},
                ],
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

        settings = app_main.translation_settings("gemini_3_5_flash")
        self.assertEqual(settings.model_name, "gemini-3.5-flash")
        self.assertEqual(settings.provider_name, "gemini_api")

    def test_gemini_lite_attempts_from_minimal_to_high_then_flash(self) -> None:
        with patch.dict("os.environ", {"GEMINI_THINKING_LEVEL": "medium"}), patch.object(
            app_main.settings, "gemini_api_key", "test-key"
        ):
            plan = app_main.translation_attempt_plan(
                app_main.translation_settings("gemini_2_5_flash_lite")
            )

        self.assertEqual(
            [(setting.model_name, level) for setting, level in plan],
            [
                ("gemini-3.1-flash-lite", "minimal"),
                ("gemini-3.1-flash-lite", "low"),
                ("gemini-3.1-flash-lite", "medium"),
                ("gemini-3.5-flash", "high"),
            ],
        )

    def test_long_gemini_input_avoids_medium_and_high_thinking(self) -> None:
        with patch.object(app_main.settings, "gemini_api_key", "test-key"):
            plan = app_main.translation_attempt_plan(
                app_main.translation_settings("gemini_2_5_flash_lite"),
                input_token_estimate=8000,
            )

        self.assertEqual(
            [(setting.model_name, level) for setting, level in plan],
            [
                ("gemini-3.1-flash-lite", "minimal"),
                ("gemini-3.1-flash-lite", "low"),
                ("gemini-3.5-flash", "high"),
            ],
        )

    def test_high_thinking_reserves_output_budget(self) -> None:
        budget = translation_worker._output_token_budget(
            "x" * 24000,
            {"subtitles": [{"id": 1, "text": "x"}], "gemini_thinking_level": "high"},
        )

        self.assertEqual(budget, 32000)

    def test_translation_retry_skips_client_http_errors(self) -> None:
        self.assertFalse(app_main.is_retryable_translation_failure(RuntimeError("translation api http error 404: missing")))
        self.assertFalse(app_main.is_retryable_translation_failure(RuntimeError("translation api http error 400: bad request")))
        self.assertTrue(app_main.is_retryable_translation_failure(RuntimeError("translation api http error 503: unavailable")))
        self.assertTrue(app_main.is_retryable_translation_failure(RuntimeError("translation api returned invalid JSON")))

    def test_translation_retry_does_not_use_deprecated_gemini_model(self) -> None:
        with patch.object(app_main.settings, "remote_llm_endpoint", ""), patch.object(
            app_main.settings, "gemini_api_key", "test-key"
        ):
            fallback = app_main.translation_retry_fallback_settings(
                app_main.translation_settings("gemini_2_5_flash_lite")
            )

        self.assertIsNone(fallback)

    def test_gemini_quota_failover_uses_openai(self) -> None:
        with patch.object(app_main.settings, "gemini_api_key", "gemini-key"), patch.object(
            app_main.settings, "openai_api_key", "openai-key"
        ):
            fallback = app_main.translation_provider_failover_settings(
                app_main.translation_settings("gemini_2_5_flash_lite")
            )
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.provider_name, "openai_api")
        self.assertEqual(fallback.engine, "gpt_5_nano")

    def test_openai_quota_failover_uses_gemini(self) -> None:
        with patch.object(app_main.settings, "gemini_api_key", "gemini-key"), patch.object(
            app_main.settings, "openai_api_key", "openai-key"
        ):
            fallback = app_main.translation_provider_failover_settings(
                app_main.translation_settings("gpt_5_nano")
            )
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.provider_name, "gemini_api")
        self.assertEqual(fallback.engine, "gemini_2_5_flash_lite")

    def test_only_quota_errors_trigger_provider_failover(self) -> None:
        self.assertTrue(app_main.is_translation_provider_failover_error(RuntimeError("translation api http error 429: RESOURCE_EXHAUSTED")))
        self.assertFalse(app_main.is_translation_provider_failover_error(RuntimeError("translation api http error 400: bad schema")))

    def test_gemini_thinking_level_is_added_to_generation_config(self) -> None:
        requests: list[dict] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"candidates":[{"content":{"parts":[{"text":"{\\"subtitles\\":{\\"1\\":\\"Hi\\"}}"}]}}],"usageMetadata":{}}'

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
        subtitles_schema = requests[0]["generationConfig"]["responseSchema"]["properties"]["subtitles"]
        self.assertEqual(subtitles_schema["required"], ["1"])
        self.assertEqual(subtitles_schema["properties"], {"1": {"type": "STRING"}})
        self.assertNotIn("additionalProperties", subtitles_schema)
        self.assertNotIn("additionalProperties", requests[0]["generationConfig"]["responseSchema"])

    def test_gemini_max_tokens_is_reported_as_truncation(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"candidates":[{"content":{"parts":[{"text":"{\\\"subtitles\\\":["}]},"finishReason":"MAX_TOKENS"}],"usageMetadata":{}}'

        with patch.object(translation_worker.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "output was truncated at MAX_TOKENS"):
                translation_worker.translate_batch_gemini(
                    {
                        "model_name": "gemini-3.5-flash",
                        "llm_api_key": "test-key",
                        "source_language": "en",
                        "target_language": "ja",
                        "subtitles": [{"id": 1, "text": "Hi"}],
                    }
                )

    def test_gemini_usage_includes_thinking_tokens_in_billable_output(self) -> None:
        usage = translation_worker._usage_gemini(
            {
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 40,
                    "thoughtsTokenCount": 60,
                    "totalTokenCount": 200,
                }
            }
        )

        self.assertEqual(usage["output_tokens"], 40)
        self.assertEqual(usage["thinking_tokens"], 60)
        self.assertEqual(usage["billable_output_tokens"], 100)
        self.assertEqual(usage["total_tokens"], 200)

    def test_gemini_availability_does_not_require_remote_llm_endpoint(self) -> None:
        with patch.object(app_main.settings, "remote_llm_endpoint", ""), patch.object(
            app_main.settings, "gemini_api_key", "test-key"
        ):
            available, error, models = asyncio.run(app_main.remote_llm_status())

        self.assertTrue(available)
        self.assertIsNone(error)
        self.assertIn(app_main.settings.local_llm_profile_models["gemini_2_5_flash_lite"], models)
        self.assertIn(app_main.settings.local_llm_profile_models["gemini_3_5_flash"], models)

    def test_gemini_model_catalog_filters_to_generate_content_models(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "models": [
                            {
                                "name": "models/gemini-3.5-flash-lite",
                                "displayName": "Gemini 3.5 Flash-Lite",
                                "inputTokenLimit": 1_048_576,
                                "outputTokenLimit": 65_536,
                                "supportedGenerationMethods": ["generateContent"],
                            },
                            {
                                "name": "models/gemini-embedding-2",
                                "supportedGenerationMethods": ["embedContent"],
                            },
                        ]
                    }
                ).encode()

        with patch.object(app_main.settings, "gemini_api_key", "test-key"), patch(
            "app.main.urllib.request.urlopen", return_value=Response()
        ) as urlopen:
            models = app_main.gemini_model_catalog()

        self.assertEqual([model["model"] for model in models], ["gemini-3.5-flash-lite"])
        self.assertEqual(models[0]["input_token_limit"], 1_048_576)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://generativelanguage.googleapis.com/v1beta/models")
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")

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

    def test_enrich_translation_metadata_prices_thinking_tokens_for_flash(self) -> None:
        metadata = app_main.enrich_translation_metadata(
            {
                "translation_engine": "gemini_3_5_flash",
                "translation_input_tokens": 1000,
                "translation_output_tokens": 500,
                "translation_thinking_tokens": 1500,
                "translation_total_tokens": 3000,
            }
        )

        self.assertEqual(metadata["translation_billable_output_tokens"], 2000)
        self.assertEqual(metadata["translation_input_price_usd_per_million"], 1.50)
        self.assertEqual(metadata["translation_output_price_usd_per_million"], 9.00)
        self.assertAlmostEqual(metadata["translation_overage_estimate_usd"], 0.0195)

    def test_failed_translation_usage_estimates_response_cost(self) -> None:
        error = TranslationError("subtitle count mismatch")
        error.translation_usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
        }
        settings = app_main.translation_settings("gemini_3_5_flash")

        usage = app_main.failed_translation_usage(error, settings)

        assert usage is not None
        assert usage["estimated_usd"] > 0
        assert usage["estimated_jpy"] > 0

    def test_gemini_thinking_tokens_are_in_billable_output(self) -> None:
        error = TranslationError("subtitle count mismatch")
        error.translation_usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "thinking_tokens": 1500,
            "billable_output_tokens": 2000,
            "total_tokens": 3000,
        }
        settings = app_main.translation_settings("gemini_3_5_flash")

        usage = app_main.failed_translation_usage(error, settings)

        assert usage is not None
        self.assertEqual(usage["billable_output_tokens"], 2000)
        self.assertEqual(usage["thinking_tokens"], 1500)
        self.assertAlmostEqual(usage["estimated_usd"], 0.0195)

    def test_bot_translation_usage_text(self) -> None:
        text = bot_main.translation_usage_text(
            {
                "translation_engine": "gemini_2_5_flash",
                "translation_provider_label": "Gemini Flash",
                "translation_billing_class": "Gemini API Free Tier",
                "translation_characters": 18420,
                "translation_input_tokens": 14208,
                "translation_output_tokens": 16731,
                "translation_chunk_count": 3,
                "translation_request_count": 4,
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
        self.assertIn("翻訳チャンク: 3 / APIリクエスト: 4", text)
        self.assertIn("入力 $0.30 / 出力 $2.50", text)

    def test_bot_translation_usage_text_shows_provider_failover(self) -> None:
        text = bot_main.translation_usage_text(
            {
                "translation_engine": "gpt_5_nano",
                "translation_provider_label": "GPT-5 nano",
                "translation_fallback_used": True,
                "translation_failover_from": "gemini-3.1-flash-lite",
            }
        )
        self.assertIn("自動フォールバック: gemini-3.1-flash-lite から切替", text)

    def test_bot_translation_failure_usage_text(self) -> None:
        text = bot_main.translation_failure_usage_text(
            {
                "attempts": [{"engine": "gemini_3_5_flash"}],
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "estimated_usd": 0.00155,
                "estimated_jpy": 0.248,
                "charged_usd": 0.0,
                "charged_jpy": 0.0,
            }
        )

        self.assertIn("LLM応答分の使用量", text)
        self.assertIn("通常料金換算", text)
        self.assertIn("課金見込み", text)

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

    def test_bot_translation_status_uses_actual_engine_after_fallback(self) -> None:
        text = bot_main.subtitle_status_text(
            {
                "translated": True,
                "source_language": "en",
                "requested_language": "ja",
                "source_kind": "manual",
                "translation_engine": "gemini_3_5_flash",
                "translation_model": "Gemini 3.5 Flash",
                "translation_fallback_used": True,
            }
        )
        self.assertIn("日本語（Gemini 3.5 Flash）", text)
        self.assertNotIn("Google翻訳", text)

    def test_translation_worker_prompt_contains_full_subtitle_context(self) -> None:
        payload = {
            "video_title": "Sample title",
            "channel_name": "Sample channel",
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
        self.assertNotIn("Topic:", prompt)
        self.assertNotIn("Glossary:", prompt)
        self.assertIn("First line", prompt)
        self.assertIn("Translate me", prompt)
        self.assertIn("Last line", prompt)

        prompt_with_context = translation_worker.build_full_translation_prompt(
            {**payload, "topic": "Bridges", "glossary": "Route 66=ルート66"}
        )
        self.assertIn("Topic: Bridges", prompt_with_context)
        self.assertIn("Glossary: Route 66=ルート66", prompt_with_context)

    def test_gemini_request_and_raw_response_are_saved_without_api_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "translation.jsonl"
            raw_response = '{"candidates": [{"content": {"parts": [{"text": "{\\"subtitles\\":{\\"1\\":\\"Hi\\"}}"}]}}]}'

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
