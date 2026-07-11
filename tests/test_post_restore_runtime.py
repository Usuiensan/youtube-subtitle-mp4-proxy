from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as app_main


class PostRestoreRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
