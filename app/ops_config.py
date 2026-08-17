"""Allowlisted, non-secret production configuration storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.translation_profiles import profile_models


CONFIG_FILE = Path("/etc/youtube-mp4-proxy/config.env")

CONFIG_SCHEMA: dict[str, tuple[str, float, float]] = {
    "LOCAL_LLM_MAX_OUTPUT_TOKENS": ("int", 1, 1_000_000),
    "TRANSLATION_CHUNK_INPUT_TOKENS": ("int", 512, 100_000),
    "TRANSLATION_API_RETRY_MAX_ATTEMPTS": ("int", 1, 10),
    "TRANSLATION_API_RETRY_BASE_SECONDS": ("float", 0, 300),
    "SYSTEM_METRICS_INTERVAL_SECONDS": ("float", 1, 3600),
    "SYSTEM_METRICS_HISTORY_SECONDS": ("int", 60, 31_536_000),
    "DISCORD_OPERATOR_USER_ID": ("int", 1, 9_223_372_036_854_775_807),
    "DISCORD_URL_INTAKE_CHANNEL_ID": ("int", 1, 9_223_372_036_854_775_807),
    "CACHE_ARCHIVE_MOUNT_POINT": ("path", 0, 0),
}
ENUM_VALUES = {
    "GEMINI_THINKING_LEVEL": {"minimal", "low", "medium", "high"},
    "TRANSLATION_DEFAULT_PROFILE": set(profile_models(os.getenv)),
}


class ConfigValidationError(ValueError):
    pass


def config_file() -> Path:
    return Path(os.getenv("TRANSLATION_CONFIG_FILE", str(CONFIG_FILE)))


def config_audit_file() -> Path:
    return Path(os.getenv("TRANSLATION_CONFIG_AUDIT_FILE", "/var/lib/youtube-mp4-proxy/config-audit.jsonl"))


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _raw_config(path: Path | None = None) -> bytes:
    path = path or config_file()
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def revision(path: Path | None = None) -> str:
    return _revision(_raw_config(path))


def _file_values(raw: bytes) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in CONFIG_SCHEMA or key in ENUM_VALUES:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _typed_value(key: str, value: object) -> int | float | str:
    if key in ENUM_VALUES:
        if not isinstance(value, str) or value.strip().lower() not in ENUM_VALUES[key]:
            raise ConfigValidationError(f"{key} must be one of: {', '.join(sorted(ENUM_VALUES[key]))}")
        return value.strip().lower()
    if key not in CONFIG_SCHEMA:
        raise ConfigValidationError(f"Unknown configuration key: {key}")
    kind, minimum, maximum = CONFIG_SCHEMA[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigValidationError(f"{key} has an invalid type")
    text = str(value).strip()
    if kind == "path":
        if not re.fullmatch(r"/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+", text):
            raise ConfigValidationError(f"{key} must be an absolute path")
        return text
    try:
        parsed: int | float = int(text) if kind == "int" and re.fullmatch(r"[+-]?\d+", text) else float(text)
    except ValueError as error:
        raise ConfigValidationError(f"{key} has an invalid value") from error
    if kind == "int" and not isinstance(parsed, int):
        raise ConfigValidationError(f"{key} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ConfigValidationError(f"{key} is outside the allowed range")
    return parsed


def allowed_values() -> dict[str, int | float | str]:
    values = _file_values(_raw_config())
    result: dict[str, int | float | str] = {}
    for key in (*CONFIG_SCHEMA, *ENUM_VALUES):
        raw = values.get(key, os.getenv(key))
        if raw is not None:
            try:
                result[key] = _typed_value(key, raw)
            except ConfigValidationError:
                # Keep a malformed existing non-secret value observable so an
                # operator can replace it through the same revision-checked API.
                result[key] = str(raw).strip()
    return result


def validate_values(values: object) -> dict[str, int | float | str]:
    if not isinstance(values, dict) or not values:
        raise ConfigValidationError("values must be a non-empty object")
    result: dict[str, int | float | str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or key not in (*CONFIG_SCHEMA, *ENUM_VALUES):
            raise ConfigValidationError(f"Configuration key is not allowed: {key}")
        result[key] = _typed_value(key, value)
    return result


def _format_value(value: int | float | str) -> str:
    return str(value)


def write_values(values: dict[str, int | float | str], expected_revision: str) -> tuple[dict[str, int | float | str], str]:
    path = config_file()
    raw = _raw_config(path)
    current_revision = _revision(raw)
    if expected_revision != current_revision:
        raise RuntimeError(f"configuration revision conflict: {current_revision}")
    old_values = allowed_values()
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    replaced: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=).*(\r?\n)?$", line)
        key = match.group(2) if match else ""
        if key in values:
            ending = match.group(4) or "\n"
            output.append(f"{match.group(1)}{key}{match.group(3)}{_format_value(values[key])}{ending}")
            replaced.add(key)
        else:
            output.append(line)
    if output and not output[-1].endswith(("\n", "\r")):
        output.append("\n")
    output.extend(f"{key}={_format_value(values[key])}\n" for key in values if key not in replaced)
    new_raw = "".join(output).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = path.stat() if path.exists() else None
    mode = stat.S_IMODE(metadata.st_mode) if metadata else 0o640
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if metadata and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.fchown(fd, metadata.st_uid, metadata.st_gid)
        with os.fdopen(fd, "wb") as file:
            file.write(new_raw)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return old_values, _revision(new_raw)


def append_audit(key: str, old_value: object, new_value: object, operator: str, result: str) -> None:
    path = config_audit_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_operator = operator if operator in {"api", "agent", "human", "root-wrapper"} else "api"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "operator": safe_operator,
        "result": result,
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
