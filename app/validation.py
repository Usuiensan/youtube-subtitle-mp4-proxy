"""Validation helpers shared by HTTP endpoints and background jobs.

Keeping request validation in one small module makes the API layer easier to
read and gives future endpoints a single place to reuse the same rules.
"""

from __future__ import annotations

import re

from fastapi import HTTPException


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANG_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


def validate_input(video_id: str, lang: str) -> None:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video id")
    validate_lang(lang)


def validate_lang(lang: str) -> None:
    if not LANG_RE.fullmatch(lang):
        raise HTTPException(status_code=400, detail="Invalid subtitle language")


def validate_discord_user_id(discord_user_id: str | None) -> str | None:
    if discord_user_id is None or discord_user_id == "":
        return None
    if not re.fullmatch(r"\d{17,20}", discord_user_id):
        raise HTTPException(status_code=400, detail="Invalid Discord user id")
    return discord_user_id


def validate_subtitle_font_size(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        size = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid subtitle font size")
    if size < 12 or size > 60:
        raise HTTPException(status_code=400, detail="subtitleFontSize must be between 12 and 60")
    return size
