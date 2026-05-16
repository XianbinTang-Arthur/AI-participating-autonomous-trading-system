"""Centralized Research Factory status contract."""

from __future__ import annotations

VALID_RESEARCH_STATUSES = frozenset(
    {
        "draft",
        "pending",
        "running",
        "succeeded",
        "partial_success",
        "failed",
        "cancelled",
    }
)

TERMINAL_RESEARCH_STATUSES = frozenset(
    {
        "succeeded",
        "partial_success",
        "failed",
        "cancelled",
    }
)


def require_valid_status(status: str) -> str:
    """Return a valid research status or raise a clear validation error."""
    if status not in VALID_RESEARCH_STATUSES:
        allowed = ", ".join(sorted(VALID_RESEARCH_STATUSES))
        raise ValueError(f"invalid research status {status!r}; expected one of: {allowed}")
    return status


def is_terminal_status(status: str) -> bool:
    """Return whether a research status represents a completed lifecycle state."""
    require_valid_status(status)
    return status in TERMINAL_RESEARCH_STATUSES
