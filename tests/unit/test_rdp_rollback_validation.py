"""Unit tests for A-0.1 rollback target validation.

Covers both the pure validator :func:`validate_rollback_target` and the
integration points of :func:`rollback_active_parameter_set` around it:

- 6 rule violations each get their own test (short-circuits at the right step).
- The happy path returns ``(True, "")``.
- Top-level ``rollback_active_parameter_set`` surfaces the rejection as a
  structured result with ``code='VALIDATION_FAILED'`` and does not write
  anything to the active table.

Design reference: ``docs/task/rdp_hardening_batch_a_detailed_design.md §2``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from unittest.mock import MagicMock, patch

import pytest

from aats.data_platform.decision_system.active_parameter_apply import (
    rollback_active_parameter_set,
)
from aats.data_platform.governance.active_params_db import (
    validate_rollback_target,
)


# ── Helpers ────────────────────────────────────────────────────────────────


class _StubResult:
    """Mimics SQLAlchemy ``Result`` for ``.fetchone()``/``.first()``."""

    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row

    def first(self) -> Any:
        return self._row


def _make_session(query_results: Iterable[Any]) -> MagicMock:
    """Build a mock Session whose ``execute`` returns the given rows in order.

    Each element becomes one ``_StubResult`` so both ``fetchone()`` and
    ``first()`` hand the row back unchanged. ``None`` means "no row matched".
    """
    session = MagicMock()
    session.execute.side_effect = [_StubResult(row) for row in query_results]
    return session


# ── validate_rollback_target ── 6 rule coverage ────────────────────────────


def test_validate_rejects_nonexistent_target() -> None:
    """Rule 1: parameter_sets has no row for the target."""
    session = _make_session([None])
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_missing"
    )
    assert ok is False
    assert reason == "target_not_found_or_wrong_combo"
    # Short-circuit at query 1 — subsequent queries must not run.
    assert session.execute.call_count == 1


def test_validate_rejects_target_from_different_family() -> None:
    """Rule 1+3: same row existence filter also encodes family/timeframe."""
    session = _make_session([None])
    ok, reason = validate_rollback_target(
        session, "overlay", "15m", "ps_belongs_to_independent"
    )
    assert ok is False
    assert reason == "target_not_found_or_wrong_combo"


def test_validate_rejects_target_from_different_timeframe() -> None:
    session = _make_session([None])
    ok, reason = validate_rollback_target(
        session, "independent", "1h", "ps_belongs_to_15m"
    )
    assert ok is False
    assert reason == "target_not_found_or_wrong_combo"


def test_validate_rejects_deprecated_target() -> None:
    """Rule 2: status must be in {frozen, released}."""
    session = _make_session([SimpleNamespace(status="deprecated")])
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_dep"
    )
    assert ok is False
    assert reason == "target_status_illegal:deprecated"


def test_validate_rejects_draft_target() -> None:
    session = _make_session([SimpleNamespace(status="draft")])
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_draft"
    )
    assert ok is False
    assert reason == "target_status_illegal:draft"


def test_validate_rejects_target_without_apply_history() -> None:
    """Rule 4: must have appeared as the ``to`` of an apply op for the combo."""
    session = _make_session(
        [
            SimpleNamespace(status="frozen"),  # query 1 — ok
            None,  # query 2 — no apply history
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_never_applied"
    )
    assert ok is False
    assert reason == "no_apply_history_for_target"
    # Short-circuit at query 2 — active/rec lookups must not run.
    assert session.execute.call_count == 2


def test_validate_rejects_self_rollback() -> None:
    """Rule 5: target must not equal the current active parameter_set_id."""
    session = _make_session(
        [
            SimpleNamespace(status="frozen"),  # query 1 — ok
            SimpleNamespace(value=1),  # query 2 — apply history present
            SimpleNamespace(parameter_set_id="ps_same"),  # query 3 — active=target
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_same"
    )
    assert ok is False
    assert reason == "target_is_currently_active"


def test_validate_rejects_target_without_approved_recommendation() -> None:
    """Rule 6: at least one approved/applied/rolled_back rec must lineage."""
    session = _make_session(
        [
            SimpleNamespace(status="frozen"),
            SimpleNamespace(value=1),
            SimpleNamespace(parameter_set_id="ps_current"),  # active ≠ target
            None,  # no approved recommendation pointing at target
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_orphan"
    )
    assert ok is False
    assert reason == "no_approved_recommendation_lineage"


def test_validate_accepts_valid_frozen_target_with_lineage() -> None:
    """Happy path: all 6 rules pass."""
    session = _make_session(
        [
            SimpleNamespace(status="frozen"),
            SimpleNamespace(value=1),
            SimpleNamespace(parameter_set_id="ps_current"),
            SimpleNamespace(value=1),
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_prev"
    )
    assert ok is True
    assert reason == ""
    # All 4 queries must run for the happy path.
    assert session.execute.call_count == 4


def test_validate_accepts_released_status() -> None:
    """Rule 2 allowlist includes 'released'."""
    session = _make_session(
        [
            SimpleNamespace(status="released"),
            SimpleNamespace(value=1),
            SimpleNamespace(parameter_set_id="ps_current"),
            SimpleNamespace(value=1),
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_released"
    )
    assert ok is True
    assert reason == ""


# ── rollback_active_parameter_set ── top-level integration points ──────────


class _RollbackSessionStub:
    """Session stub that returns a fixed current active row for the first
    query (FOR UPDATE active_parameter_sets) and hands subsequent queries
    to the validator — which is itself patched out in these tests."""

    def __init__(self, current_active: str | None = "ps_current") -> None:
        self._current = current_active

    def execute(self, *_args, **_kwargs) -> _StubResult:
        if self._current is None:
            return _StubResult(None)
        return _StubResult(SimpleNamespace(parameter_set_id=self._current))

    def __enter__(self) -> "_RollbackSessionStub":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False


@pytest.fixture
def patch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub env guard to always allow rollback in unit tests."""
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.get_current_environment",
        lambda: "dev",
    )
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.guard_parameter_rollback",
        lambda _env: SimpleNamespace(allowed=True, reason=""),
    )


def test_rollback_rejects_when_validator_fails(
    tmp_path: Path, patch_environment: None
) -> None:
    """VALIDATION_FAILED short-circuits before upsert/history writes."""
    with (
        patch(
            "aats.data_platform.db.get_session",
            side_effect=lambda: _RollbackSessionStub(current_active="ps_current"),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.validate_rollback_target",
            return_value=(False, "target_status_illegal:deprecated"),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
        ) as mock_upsert,
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
        ) as mock_history,
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_deprecated",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "VALIDATION_FAILED"
    assert result["reason"] == "target_status_illegal:deprecated"
    mock_upsert.assert_not_called()
    mock_history.assert_not_called()


def test_rollback_returns_no_previous_target_when_history_empty(
    tmp_path: Path, patch_environment: None
) -> None:
    with (
        patch(
            "aats.data_platform.db.get_session",
            side_effect=lambda: _RollbackSessionStub(current_active="ps_current"),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_get_previous_set_id",
            return_value=None,
        ),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id=None,
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "NO_PREVIOUS_TARGET"


def test_rollback_returns_no_active_set_when_nothing_active(
    tmp_path: Path, patch_environment: None
) -> None:
    with patch(
        "aats.data_platform.db.get_session",
        side_effect=lambda: _RollbackSessionStub(current_active=None),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_anything",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "NO_ACTIVE_SET"


def test_rollback_returns_environment_blocked_when_guard_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.get_current_environment",
        lambda: "prod",
    )
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.guard_parameter_rollback",
        lambda _env: SimpleNamespace(
            allowed=False, reason="prod 环境禁止回滚"
        ),
    )
    result = rollback_active_parameter_set(
        tmp_path,
        family="independent",
        timeframe="15m",
        to_parameter_set_id="ps_anything",
        actor="operator",
    )
    assert result["ok"] is False
    assert result["code"] == "ENVIRONMENT_BLOCKED"
    assert result["environment"] == "prod"


def test_rollback_rejects_self_rollback_without_writes(
    tmp_path: Path, patch_environment: None
) -> None:
    """Target == current active short-circuits even before validator."""
    with (
        patch(
            "aats.data_platform.db.get_session",
            side_effect=lambda: _RollbackSessionStub(current_active="ps_same"),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.validate_rollback_target",
        ) as mock_validate,
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
        ) as mock_upsert,
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_same",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "VALIDATION_FAILED"
    assert result["reason"] == "target_is_currently_active"
    # Short-circuit skips the validator entirely — no DB validation work, no write
    mock_validate.assert_not_called()
    mock_upsert.assert_not_called()
