"""Niconico video URL parsing."""

from __future__ import annotations

import re
import urllib.parse

from fastapi import HTTPException


NICONICO_VIDEO_ID_RE = re.compile(r"^(?:sm|so|nm)\d+$", re.IGNORECASE)


def parse_niconico_url(value: str) -> tuple[str, str]:
    value = value.strip().strip("<>")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"nicovideo.jp", "www.nicovideo.jp", "nico.ms"}:
        raise HTTPException(status_code=400, detail="Invalid Niconico video URL")
    parts = [part for part in parsed.path.split("/") if part]
    video_id = parts[-1] if parts else ""
    if parts[:1] == ["watch"] and len(parts) >= 2:
        video_id = parts[1]
    if not NICONICO_VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid Niconico video id or URL")
    return video_id.lower(), f"https://www.nicovideo.jp/watch/{video_id.lower()}"


def validate_niconico_video_id(video_id: str) -> str:
    if not NICONICO_VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid Niconico video id")
    return video_id.lower()
