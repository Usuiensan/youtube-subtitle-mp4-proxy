import asyncio
import sys

import pytest
from fastapi import HTTPException

from app.command_errors import CommandError
from app.command_runner import run_command


def test_run_command_returns_stdout() -> None:
    result = asyncio.run(run_command([sys.executable, "-c", "print('ok')"], timeout_seconds=5))
    assert result.strip() == "ok"


def test_run_command_maps_failures_and_timeout() -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(run_command([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(2)"], timeout_seconds=5))
    assert error.value.status_code == 502
    assert "bad" in str(error.value.detail)

    with pytest.raises(CommandError):
        asyncio.run(run_command([sys.executable, "-c", "import sys; sys.exit(3)"], timeout_seconds=5, raise_http=False))

    with pytest.raises(HTTPException) as timeout_error:
        asyncio.run(run_command([sys.executable, "-c", "import time; time.sleep(2)"], timeout_seconds=1))
    assert timeout_error.value.status_code == 504
