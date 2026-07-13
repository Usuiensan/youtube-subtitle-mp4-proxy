import pytest
from fastapi import HTTPException

from app.youtube_inputs import (
    extract_channel_lookup,
    extract_playlist_id,
    extract_video_id_from_value,
    manual_video_tracks,
)


def test_extract_video_id_supports_common_url_forms() -> None:
    video_id = "dQw4w9WgXcQ"
    assert extract_video_id_from_value(video_id) == video_id
    assert extract_video_id_from_value(f"https://youtu.be/{video_id}") == video_id
    assert extract_video_id_from_value(f"https://www.youtube.com/watch?v={video_id}") == video_id
    assert extract_video_id_from_value(f"https://youtube.com/shorts/{video_id}") == video_id
    assert extract_video_id_from_value("not-a-video!") is None


def test_playlist_and_channel_inputs_are_normalized() -> None:
    assert extract_playlist_id("PL1234567890") == "PL1234567890"
    assert extract_playlist_id("https://youtube.com/playlist?list=PL1234567890") == "PL1234567890"
    assert extract_channel_lookup("@example") == ("forHandle", "@example")
    assert extract_channel_lookup("https://youtube.com/channel/UC1234567890") == ("id", "UC1234567890")
    with pytest.raises(HTTPException):
        extract_playlist_id("invalid")


def test_manual_video_tracks_deduplicates_and_limits() -> None:
    video_id = "dQw4w9WgXcQ"
    tracks = manual_video_tracks(f"{video_id} https://youtu.be/{video_id}", 10)
    assert len(tracks) == 1
    assert tracks[0]["video_id"] == video_id
