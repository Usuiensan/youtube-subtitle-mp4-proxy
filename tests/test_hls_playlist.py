from app.hls_playlist import rewrite_playlist


def test_rewrite_playlist_only_rewrites_relative_segment_urls() -> None:
    body = rewrite_playlist(
        ["#EXTM3U", "#EXTINF:6,", "segment_00000.ts", "https://cdn.test/other.ts", ""],
        "https://proxy.test",
        "video_ja_key",
    )
    assert body == (
        "#EXTM3U\n"
        "#EXTINF:6,\n"
        "https://proxy.test/hls/video_ja_key/segment_00000.ts\n"
        "https://cdn.test/other.ts\n\n"
    )
