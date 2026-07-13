"""Helpers for rewriting local HLS playlists into proxy URLs."""

from __future__ import annotations


def rewrite_playlist(lines: list[str], base_url: str, key: str) -> str:
    rewritten: list[str] = []
    for line in lines:
        if not line or line.startswith("#") or "://" in line:
            rewritten.append(line)
        else:
            rewritten.append(f"{base_url}/hls/{key}/{line}")
    return "\n".join(rewritten) + "\n"
