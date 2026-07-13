"""Async subprocess execution with consistent timeout/error handling."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException

from app.command_errors import CommandError


async def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int,
    raise_http: bool = True,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise HTTPException(status_code=504, detail="Conversion timed out")

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        print(f"Command failed: {' '.join(args)}\n{message}", flush=True)
        if not raise_http:
            raise CommandError(args, message)
        raise HTTPException(
            status_code=502,
            detail=message[-1000:] or f"Command failed: {args[0]}",
        )
    return stdout.decode("utf-8", errors="replace")
