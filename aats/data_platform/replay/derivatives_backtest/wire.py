"""Strict wire identities shared by the derivatives replay evidence boundary.

These helpers deliberately reject values that could be normalized into a
valid identity.  Formal artifacts must already contain the one canonical wire
spelling before any semantic object is constructed.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping

from .contracts import DerivativesBacktestContractError


_CANONICAL_UTC_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})\.(?P<microseconds>\d{6})Z$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def require_exact_mapping_keys(
    value: Any,
    expected: frozenset[str] | set[str],
    code: str,
) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(expected):
        raise DerivativesBacktestContractError(code)
    return value


def require_canonical_utc_timestamp(value: Any, field_name: str) -> datetime:
    """Parse the exact UTC RFC3339 microsecond spelling used in artifacts."""

    if type(value) is not str or _CANONICAL_UTC_RE.fullmatch(value) is None:
        raise DerivativesBacktestContractError(
            "timestamp_non_canonical",
            field=field_name,
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise DerivativesBacktestContractError(
            "timestamp_non_canonical",
            field=field_name,
        ) from exc
    if canonical_utc_timestamp(parsed, field_name) != value:
        raise DerivativesBacktestContractError(
            "timestamp_non_canonical",
            field=field_name,
        )
    return parsed


def require_utc_datetime(value: Any, field_name: str) -> datetime:
    """Require an already canonical UTC in-memory timestamp without coercion."""

    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
        or value.tzinfo is not timezone.utc
    ):
        raise DerivativesBacktestContractError(
            "timestamp_utc_required",
            field=field_name,
        )
    return value


def canonical_utc_timestamp(value: datetime, field_name: str = "timestamp") -> str:
    resolved = require_utc_datetime(value, field_name)
    return resolved.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def require_canonical_uuid(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise DerivativesBacktestContractError(
            "uuid_non_canonical",
            field=field_name,
        )
    try:
        resolved = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise DerivativesBacktestContractError(
            "uuid_non_canonical",
            field=field_name,
        ) from exc
    if resolved != value:
        raise DerivativesBacktestContractError(
            "uuid_non_canonical",
            field=field_name,
        )
    return value


def require_sha256(value: Any, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DerivativesBacktestContractError(
            "sha256_non_canonical",
            field=field_name,
        )
    return value


def require_exact_int(
    value: Any,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise DerivativesBacktestContractError(
            "integer_out_of_bounds",
            field=field_name,
        )
    return value


def require_identifier(value: Any, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise DerivativesBacktestContractError(
            "identifier_non_canonical",
            field=field_name,
        )
    return value


def require_safe_relative_posix_path(value: Any, field_name: str) -> str:
    """Reject every path spelling with platform-dependent interpretation."""

    if (
        type(value) is not str
        or not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or value.endswith((".", " "))
    ):
        raise DerivativesBacktestContractError(
            "artifact_relative_path_invalid",
            field=field_name,
        )
    path = PurePosixPath(value)
    parts = path.parts
    if (
        str(path) != value
        or not parts
        or any(
            part in {"", ".", ".."}
            or _PATH_SEGMENT_RE.fullmatch(part) is None
            or part.endswith((".", " "))
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_BASENAMES
            for part in parts
        )
    ):
        raise DerivativesBacktestContractError(
            "artifact_relative_path_invalid",
            field=field_name,
        )
    return value


__all__ = [
    "canonical_utc_timestamp",
    "require_canonical_utc_timestamp",
    "require_canonical_uuid",
    "require_exact_int",
    "require_exact_mapping_keys",
    "require_identifier",
    "require_safe_relative_posix_path",
    "require_sha256",
    "require_utc_datetime",
]
