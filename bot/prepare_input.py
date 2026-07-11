from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def split_manual_video_values(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", value.strip()) if part]


def extract_video_id(value: str) -> str:
    value = value.strip().strip("<>")
    if VIDEO_ID_RE.fullmatch(value):
        return value
    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        parsed = urllib.parse.urlparse("https://" + value)
    else:
        parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower().replace("www.", "")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        if VIDEO_ID_RE.fullmatch(candidate):
            return candidate
        raise ValueError(f"無効な YouTube 動画IDです: {value}")
    query = urllib.parse.parse_qs(parsed.query)
    candidate = query.get("v", [""])[0]
    if VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("shorts", "embed", "live"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts) and VIDEO_ID_RE.fullmatch(parts[index + 1]):
                return parts[index + 1]
    raise ValueError(f"無効な YouTube 動画IDです: {value}")


def looks_like_playlist_or_channel(value: str) -> bool:
    value = value.strip()
    if value.startswith("@"):
        return True
    if value.startswith("UC") and not VIDEO_ID_RE.fullmatch(value):
        return True
    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        parsed = urllib.parse.urlparse("https://" + value)
    else:
        parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("list"):
        return True
    path_parts = [part for part in parsed.path.split("/") if part]
    return any(part in {"playlist", "channel", "c", "@"} for part in path_parts)


def is_manual_video_list(value: str) -> bool:
    parts = split_manual_video_values(value)
    if len(parts) <= 1:
        return False
    valid_count = 0
    for part in parts:
        try:
            extract_video_id(part)
            valid_count += 1
        except ValueError:
            continue
    return valid_count >= 2


def is_ambiguous_prepare_input(value: str) -> bool:
    value = value.strip()
    if not value or is_manual_video_list(value):
        return False
    try:
        extract_video_id(value)
        has_video = True
    except ValueError:
        has_video = False
    has_playlist = looks_like_playlist_or_channel(value)
    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        parsed = urllib.parse.urlparse("https://" + value)
    else:
        parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("list") and has_video:
        has_playlist = True
    return has_video and has_playlist


@dataclass(frozen=True)
class PrepareInputDecision:
    scope: str
    normalized_value: str

