"""Shared regular-expression contracts for external identifiers."""

from __future__ import annotations

import re


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANG_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
