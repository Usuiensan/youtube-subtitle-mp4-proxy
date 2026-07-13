"""HTTP byte-range parsing for media responses."""

from __future__ import annotations

import re

from fastapi import HTTPException


def parse_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Return an inclusive byte range, or ``None`` when no range is requested."""
    if not range_header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Invalid byte range")

    start_raw, end_raw = match.groups()
    if start_raw == "" and end_raw == "":
        raise HTTPException(status_code=416, detail="Invalid byte range")
    if start_raw == "":
        suffix_length = int(end_raw)
        start = max(file_size - suffix_length, 0)
        end = file_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1

    if start >= file_size or end < start:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable")
    return start, min(end, file_size - 1)
