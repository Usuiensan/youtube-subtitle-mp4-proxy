"""Streaming helpers for local media files."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path


async def file_iterator(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    """Yield an inclusive byte range without loading the whole file."""
    with path.open("rb") as file:
        file.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
