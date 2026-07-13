"""Pure request/response helpers for the YamaPlayer integration."""

from __future__ import annotations

import json
import re

from fastapi import HTTPException, Response


def normalize_yamaplayer_mode(mode: int) -> int:
    if mode not in {0, 1, 2}:
        raise HTTPException(status_code=400, detail="mode must be 0, 1, or 2")
    return mode


def normalize_max_items(max_items: int) -> int:
    if max_items < 1 or max_items > 5000:
        raise HTTPException(status_code=400, detail="maxItems must be between 1 and 5000")
    return max_items


def normalize_yamaplayer_url_mode(url_mode: str) -> str:
    if url_mode not in {"original", "mp4", "hls"}:
        raise HTTPException(status_code=400, detail="urlMode must be original, mp4, or hls")
    return url_mode


def yamaplayer_track_url(
    track: dict[str, str],
    url_mode: str,
    lang: str,
    base_url: str,
) -> str:
    if url_mode == "original":
        return track["url"]
    route = "youtube-hls" if url_mode == "hls" else "youtube"
    return f"{base_url}/{route}/{track['video_id']}/{lang}"


def yamaplayer_playlist_entry(
    playlist_name: str,
    youtube_list_id: str,
    tracks: list[dict[str, str]],
    mode: int,
    url_mode: str,
    lang: str,
    base_url: str,
) -> dict:
    return {
        "active": True,
        "name": playlist_name,
        "youtubeListId": youtube_list_id,
        "tracks": [
            {
                "mode": mode,
                "title": track["title"],
                "url": yamaplayer_track_url(track, url_mode, lang, base_url),
            }
            for track in tracks
        ],
    }


def yamaplayer_export_response(playlists: list[dict], filename_base: str) -> Response:
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename_base).strip("_") or "yamaplayer"
    return Response(
        json.dumps({"playlists": playlists}, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.json"',
            "Cache-Control": "no-cache",
        },
    )


def split_yamaplayer_sources(sources: str) -> list[str]:
    values = [line.strip() for line in sources.splitlines() if line.strip()]
    if not values:
        raise HTTPException(status_code=400, detail="At least one source is required")
    if len(values) > 100:
        raise HTTPException(status_code=400, detail="sources must contain 100 items or fewer")
    return values
