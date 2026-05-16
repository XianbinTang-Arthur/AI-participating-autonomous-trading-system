"""Research-only sandbox proposal schema."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

ALLOWED_SANDBOX_PROPOSAL_TYPES = frozenset(
    {
        "factor",
        "model",
        "parameter",
        "execution_policy",
        "risk_budget",
        "regime_classifier",
    }
)


@dataclass(frozen=True, slots=True)
class SandboxProposal:
    """Candidate research proposal that has not been executed or promoted."""

    proposal_id: str
    proposal_type: str
    hypothesis: str
    read_paths: Sequence[str] = field(default_factory=tuple)
    write_paths: Sequence[str] = field(default_factory=tuple)
    outputs: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_safe_identifier(self.proposal_id, "proposal_id")
        if self.proposal_type not in ALLOWED_SANDBOX_PROPOSAL_TYPES:
            allowed = ", ".join(sorted(ALLOWED_SANDBOX_PROPOSAL_TYPES))
            raise ValueError(f"proposal_type must be one of: {allowed}")
        _require_non_empty(self.hypothesis, "hypothesis")
        read_paths = _normalize_path_sequence(self.read_paths, "read_paths")
        write_paths = _normalize_path_sequence(self.write_paths, "write_paths")
        if not isinstance(self.outputs, Mapping):
            raise ValueError("outputs must be a mapping")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        object.__setattr__(self, "read_paths", read_paths)
        object.__setattr__(self, "write_paths", write_paths)
        object.__setattr__(self, "outputs", dict(self.outputs))
        object.__setattr__(self, "metadata", dict(self.metadata))


def normalize_sandbox_path(value: str, field_name: str = "path") -> str:
    """Normalize a repo-relative sandbox path and reject traversal/absolute paths."""
    value = _require_non_empty(value, field_name)
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if "\x00" in normalized:
        raise ValueError(f"{field_name} must not contain null bytes")
    if normalized.startswith(("/", "~")):
        raise ValueError(f"{field_name} must be repo-relative")
    first_part = normalized.split("/", 1)[0]
    if ":" in first_part:
        raise ValueError(f"{field_name} must be repo-relative")

    path = PurePosixPath(normalized)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts:
        raise ValueError(f"{field_name} must not be empty")
    if ".." in parts:
        raise ValueError(f"{field_name} must not contain path traversal")
    return PurePosixPath(*parts).as_posix()


def _normalize_path_sequence(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        raise ValueError(f"{field_name} must be a sequence of paths")
    return tuple(normalize_sandbox_path(value, field_name) for value in values)


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_safe_identifier(value: str, field_name: str) -> str:
    value = _require_non_empty(value, field_name)
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must not contain path traversal or separators")
    return value
