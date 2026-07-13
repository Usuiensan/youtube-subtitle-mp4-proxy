from pathlib import Path

from app.cache_layout import CacheLayout


def test_cache_layout_keeps_hot_entry_structure() -> None:
    layout = CacheLayout(Path("/cache/hot"))

    assert layout.entry_dir("key") == Path("/cache/hot/key")
    assert layout.output_path("key") == Path("/cache/hot/key/output.mp4")
    assert layout.hls_playlist_path("key") == Path("/cache/hot/key/hls/index.m3u8")
    assert layout.source_meta_path("key") == Path("/cache/hot/key/source.json")
    assert layout.translation_meta_path("key") == Path("/cache/hot/key/source/translation.json")
    assert layout.archive_entry_dir("key") is None


def test_cache_layout_supports_archive_entries() -> None:
    layout = CacheLayout(Path("/cache/hot"), Path("/cache/archive"))
    assert layout.archive_entry_dir("key") == Path("/cache/archive/key")
