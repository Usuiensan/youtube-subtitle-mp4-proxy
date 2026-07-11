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
