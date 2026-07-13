"""Parsing and normalization of YouTube URL and ID inputs."""

from __future__ import annotations

import re
import urllib.parse

from fastapi import HTTPException

YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_url(value: str) -> urllib.parse.ParseResult:
    value = value.strip()
    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        value = "https://" + value
    return urllib.parse.urlparse(value)


def extract_playlist_id(value: str) -> str:
    value = value.strip()
    if YOUTUBE_ID_RE.fullmatch(value):
        return value
    playlist_id = urllib.parse.parse_qs(parse_youtube_url(value).query).get("list", [""])[0]
    if YOUTUBE_ID_RE.fullmatch(playlist_id):
        return playlist_id
    raise HTTPException(status_code=400, detail="Invalid YouTube playlist id or URL")


def extract_channel_lookup(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("@"):
        return "forHandle", value
    if value.startswith("UC") and YOUTUBE_ID_RE.fullmatch(value):
        return "id", value
    parts = [part for part in parse_youtube_url(value).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and YOUTUBE_ID_RE.fullmatch(parts[1]):
        return "id", parts[1]
    if parts and parts[0].startswith("@"):
        return "forHandle", parts[0]
    if len(parts) >= 2 and parts[0] in {"c", "user"}:
        return "forUsername", parts[1]
    raise HTTPException(status_code=400, detail="Invalid YouTube channel id, handle, or URL")


def extract_video_id_from_value(value: str) -> str | None:
    value = value.strip().strip("<>")
    if VIDEO_ID_RE.fullmatch(value):
        return value
    parsed = parse_youtube_url(value)
    host = parsed.netloc.lower().replace("www.", "")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.fullmatch(candidate) else None
    candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    if VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("shorts", "embed", "live"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts) and VIDEO_ID_RE.fullmatch(parts[index + 1]):
                return parts[index + 1]
    return None


def manual_video_tracks(source: str, max_items: int) -> list[dict[str, str]]:
    tracks: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", source):
        video_id = extract_video_id_from_value(token)
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        tracks.append({"video_id": video_id, "title": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"})
        if len(tracks) >= max_items:
            break
    return tracks
