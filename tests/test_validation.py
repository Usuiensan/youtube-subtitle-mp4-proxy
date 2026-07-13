import pytest
from fastapi import HTTPException

from app.validation import (
    validate_discord_user_id,
    validate_input,
    validate_lang,
    validate_subtitle_font_size,
)


def test_validate_input_accepts_video_id_and_language() -> None:
    assert validate_input("dQw4w9WgXcQ", "ja") is None


@pytest.mark.parametrize(
    "video_id, language",
    [("too-short", "ja"), ("dQw4w9WgXcQ", "j")],
)
def test_validate_input_rejects_invalid_values(video_id: str, language: str) -> None:
    with pytest.raises(HTTPException) as error:
        validate_input(video_id, language)
    assert error.value.status_code == 400


def test_validate_discord_user_id_is_optional_and_strict() -> None:
    assert validate_discord_user_id(None) is None
    assert validate_discord_user_id("123456789012345678") == "123456789012345678"
    with pytest.raises(HTTPException):
        validate_discord_user_id("123")


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), ("22", 22)])
def test_validate_subtitle_font_size(value: str | None, expected: int | None) -> None:
    assert validate_subtitle_font_size(value) == expected


@pytest.mark.parametrize("value", ["11", "61", "large"])
def test_validate_subtitle_font_size_rejects_out_of_range(value: str) -> None:
    with pytest.raises(HTTPException):
        validate_subtitle_font_size(value)
