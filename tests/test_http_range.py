import pytest
from fastapi import HTTPException

from app.http_range import parse_range


def test_parse_range_supports_full_open_and_suffix_ranges() -> None:
    assert parse_range(None, 100) is None
    assert parse_range("bytes=10-20", 100) == (10, 20)
    assert parse_range("bytes=10-", 100) == (10, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)


def test_parse_range_clamps_end_to_file_size() -> None:
    assert parse_range("bytes=90-200", 100) == (90, 99)


@pytest.mark.parametrize("header", ["bytes=", "items=1-2", "bytes=100-101", "bytes=20-10"])
def test_parse_range_rejects_invalid_or_unsatisfiable_ranges(header: str) -> None:
    with pytest.raises(HTTPException) as error:
        parse_range(header, 100)
    assert error.value.status_code == 416
