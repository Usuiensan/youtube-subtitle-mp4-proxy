from __future__ import annotations

import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as app_main
from bot import main as bot_main


class PostRestoreRuntimeTests(unittest.TestCase):
    def discord_not_found(self) -> bot_main.discord.NotFound:
        response = SimpleNamespace(status=404, reason="Not Found")
        return bot_main.discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})

    def test_prepare_ready_path_without_cache_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_hot_dir = app_main.settings.cache_hot_dir
            app_main.settings.cache_hot_dir = Path(tmp)
            try:
                self.assertIsNone(app_main.prepare_ready_path("dQw4w9WgXcQ", "ja", "mp4"))
                self.assertIsNone(app_main.prepare_ready_path("dQw4w9WgXcQ", "ja", "hls"))
                self.assertIsNone(app_main.prepare_ready_path("dQw4w9WgXcQ", "ja", "mp4", "en", "google_cloud"))
            finally:
                app_main.settings.cache_hot_dir = original_hot_dir

    def test_prepare_ready_path_with_cache_returns_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_hot_dir = app_main.settings.cache_hot_dir
            app_main.settings.cache_hot_dir = Path(tmp)
            try:
                key = app_main.cache_key("dQw4w9WgXcQ", "ja", "en", "google_cloud")
                app_main.output_path(key).parent.mkdir(parents=True, exist_ok=True)
                app_main.output_path(key).write_bytes(b"0")
                self.assertEqual(
                    app_main.prepare_ready_path("dQw4w9WgXcQ", "ja", "mp4", "en", "google_cloud"),
                    app_main.output_path(key),
                )

                app_main.hls_dir(key).mkdir(parents=True, exist_ok=True)
                app_main.hls_playlist_path(key).write_text("#EXTM3U\n#EXT-X-ENDLIST\n", encoding="utf-8")
                (app_main.hls_dir(key) / "segment_000.ts").write_bytes(b"0")
                self.assertEqual(
                    app_main.prepare_ready_path("dQw4w9WgXcQ", "ja", "hls", "en", "google_cloud"),
                    app_main.hls_playlist_path(key),
                )
            finally:
                app_main.settings.cache_hot_dir = original_hot_dir

    def test_index_html_compare_results_declared_once(self) -> None:
        response = TestClient(app_main.app).get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertEqual(html.count('const compareResults = document.getElementById("compareResults");'), 1)
        self.assertNotIn("compareStage", html)
        self.assertIn("comparePanel.style.setProperty", html)

    def test_index_html_has_no_obvious_duplicate_const_declarations(self) -> None:
        response = TestClient(app_main.app).get("/")
        self.assertEqual(response.status_code, 200)
        const_names = re.findall(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=", response.text)
        duplicates = {name for name in const_names if const_names.count(name) > 1}
        self.assertNotIn("compareResults", duplicates)

    def test_public_smoke_routes(self) -> None:
        client = TestClient(app_main.app)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_prepare_api_auth_failure_is_not_500(self) -> None:
        response = TestClient(app_main.app).post("/prepare/youtube/dQw4w9WgXcQ/ja?mode=mp4")
        self.assertIn(response.status_code, {401, 403, 503})

    def test_prepared_srt_download_does_not_require_prepare_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_hot_dir = app_main.settings.cache_hot_dir
            original_token = app_main.settings.discord_prepare_token
            app_main.settings.cache_hot_dir = Path(tmp)
            app_main.settings.discord_prepare_token = "token"
            try:
                key = app_main.cache_key("dQw4w9WgXcQ", "ja")
                source_dir = app_main.source_dir(key)
                source_dir.mkdir(parents=True, exist_ok=True)
                subtitle_path = source_dir / "subtitle.ja.srt"
                subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
                app_main.entry_dir(key).joinpath("source.json").write_text(
                    (
                        '{"video_id":"dQw4w9WgXcQ","lang":"ja",'
                        '"subtitle":"source/subtitle.ja.srt",'
                        '"subtitle_meta":{"requested_language":"ja","source_language":"ja"}}'
                    ),
                    encoding="utf-8",
                )

                response = TestClient(app_main.app).get(f"/prepared/{key}/source.srt")
                self.assertEqual(response.status_code, 200)
                self.assertIn("hello", response.text)
            finally:
                app_main.settings.cache_hot_dir = original_hot_dir
                app_main.settings.discord_prepare_token = original_token

    def test_prepared_source_mp4_still_requires_prepare_token(self) -> None:
        with patch.object(app_main.settings, "discord_prepare_token", "token"):
            response = TestClient(app_main.app).get("/prepared/dQw4w9WgXcQ_ja/source.mp4")
        self.assertEqual(response.status_code, 401)

    def test_translated_subtitle_uses_single_srt_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            translated = work / "translated.srt"
            translated.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\nこんにちは\n", encoding="utf-8")

            arg = app_main.ffmpeg_subtitle_arg(
                translated,
                {
                    "requested_language": "ja",
                    "source_language": "en",
                    "translated": True,
                    "translation_engine": "google_cloud",
                },
            )

        self.assertIn("subtitles=", arg)
        self.assertIn("translated.srt", arg)
        self.assertNotIn("drawtext=", arg)

    def test_unavailable_video_info_maps_to_404(self) -> None:
        import asyncio

        error = app_main.CommandError(["yt-dlp"], "ERROR: [youtube] I5e6ftNpGsU: This video is not available")
        with patch.object(app_main, "run_command", new=AsyncMock(side_effect=error)):
            with self.assertRaises(app_main.HTTPException) as raised:
                asyncio.run(app_main.fetch_video_info("I5e6ftNpGsU"))

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("この動画は利用できません", raised.exception.detail)

    def test_unavailable_video_subtitle_api_returns_404(self) -> None:
        error = app_main.CommandError(["yt-dlp"], "ERROR: [youtube] I5e6ftNpGsU: This video is not available")
        with patch.object(app_main.settings, "discord_prepare_token", "token"), patch.object(
            app_main, "run_command", new=AsyncMock(side_effect=error)
        ):
            response = TestClient(app_main.app).get(
                "/prepare/youtube/I5e6ftNpGsU/ja/subtitles?mode=mp4",
                headers={"Authorization": "Bearer token"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn("この動画は利用できません", response.json()["detail"])

    def test_discord_subtitle_options_404_message_is_user_facing(self) -> None:
        error = bot_main.PrepareApiError(404, "この動画は利用できません。")
        message = bot_main.subtitle_options_error_message(error)
        self.assertIn("字幕候補を取得できませんでした", message)
        self.assertNotIn("APIエラー", message)

    def test_discord_prepare_api_timeout_is_configurable(self) -> None:
        class Response:
            status = 200

            def read(self) -> bytes:
                return b'{"ok": true}'

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

        with patch.object(bot_main.settings, "prepare_api_timeout_seconds", 123.0), patch.object(
            bot_main.urllib.request, "urlopen", return_value=Response()
        ) as urlopen:
            status, body = bot_main.http_json("GET", "http://example.test/status")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 123.0)

    def test_discord_prepare_accepts_video_inputs_without_forced_scope_name_error(self) -> None:
        async def run_case(source: str) -> None:
            interaction = SimpleNamespace(
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
                user=SimpleNamespace(id=123456789012345678),
            )
            with patch.object(bot_main.settings, "discord_prepare_token", "token"), patch.object(
                bot_main, "prepare_video", new=AsyncMock(return_value=(202, {
                    "status": "queued",
                    "video_id": "dQw4w9WgXcQ",
                    "lang": "en",
                    "mode": "mp4",
                    "status_url": "http://example.test/prepare/jobs/job1",
                }))
            ):
                await bot_main.prepare_command.callback(interaction, source, "en", None, None, False)

            interaction.response.defer.assert_awaited_once()
            interaction.followup.send.assert_awaited()

        import asyncio

        for source in (
            "dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ):
            asyncio.run(run_case(source))

    def test_discord_clear_all_edits_original_response_and_calls_api_once(self) -> None:
        import asyncio

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
            channel=None,
            channel_id=None,
            client=SimpleNamespace(user=SimpleNamespace(id=42)),
        )
        with patch.object(bot_main.settings, "discord_prepare_token", "token"), patch.object(
            bot_main, "clear_all_videos", new=AsyncMock(return_value=(200, {"message": "done"}))
        ) as clear_all, patch.object(
            bot_main, "delete_bot_messages_in_channel", new=AsyncMock(side_effect=self.discord_not_found())
        ):
            asyncio.run(bot_main.clear_all_command.callback(interaction))

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.edit_original_response.assert_awaited_once_with(content="done")
        interaction.followup.send.assert_not_awaited()
        clear_all.assert_awaited_once()

    def test_discord_clear_all_falls_back_when_original_response_is_missing(self) -> None:
        import asyncio

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(side_effect=self.discord_not_found()),
            followup=SimpleNamespace(send=AsyncMock()),
            channel=None,
            channel_id=None,
            client=SimpleNamespace(user=SimpleNamespace(id=42)),
        )
        with patch.object(bot_main.settings, "discord_prepare_token", "token"), patch.object(
            bot_main, "clear_all_videos", new=AsyncMock(return_value=(200, {"message": "done"}))
        ) as clear_all, patch.object(
            bot_main, "delete_bot_messages_in_channel", new=AsyncMock()
        ):
            asyncio.run(bot_main.clear_all_command.callback(interaction))

        interaction.followup.send.assert_awaited_once_with("done", ephemeral=True)
        clear_all.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
