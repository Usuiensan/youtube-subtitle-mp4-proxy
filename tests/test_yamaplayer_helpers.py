import json

import pytest
from fastapi import HTTPException

from app.yamaplayer_helpers import (
    normalize_max_items,
    normalize_yamaplayer_mode,
    normalize_yamaplayer_url_mode,
    split_yamaplayer_sources,
    yamaplayer_export_response,
    yamaplayer_playlist_entry,
)


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_yamaplayer_mode_is_normalized(mode: int) -> None:
    assert normalize_yamaplayer_mode(mode) == mode


def test_yamaplayer_normalizers_reject_invalid_values() -> None:
    with pytest.raises(HTTPException):
        normalize_yamaplayer_mode(3)
    with pytest.raises(HTTPException):
        normalize_max_items(0)
    with pytest.raises(HTTPException):
        normalize_yamaplayer_url_mode("dash")


def test_playlist_entry_and_export_keep_contract() -> None:
    tracks = [{"video_id": "dQw4w9WgXcQ", "title": "Example", "url": "https://youtube.test/watch?v=dQw4w9WgXcQ"}]
    entry = yamaplayer_playlist_entry("List", "PL123", tracks, 0, "mp4", "ja", "https://proxy.test")
    assert entry["tracks"][0]["url"] == "https://proxy.test/youtube/dQw4w9WgXcQ/ja"

    response = yamaplayer_export_response([entry], "list:export")
    assert response.media_type == "application/json; charset=utf-8"
    assert json.loads(response.body)["playlists"] == [entry]


def test_split_sources_requires_nonempty_and_limits_count() -> None:
    assert split_yamaplayer_sources("one\n\ntwo") == ["one", "two"]
    with pytest.raises(HTTPException):
        split_yamaplayer_sources("\n")
    with pytest.raises(HTTPException):
        split_yamaplayer_sources("\n".join(str(i) for i in range(101)))
