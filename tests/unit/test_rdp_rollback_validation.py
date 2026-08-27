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
    clear_active_parameter_set,
    rollback_active_parameter_set,
)
from aats.data_platform.governance.active_params_db import (
    db_get_parameter_set_values,
    db_get_previous_set_id,
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


def test_previous_parameter_set_uses_deterministic_history_order() -> None:
    session = _make_session(
        [SimpleNamespace(from_parameter_set_id="ps_latest_same_timestamp")]
    )

    result = db_get_previous_set_id(session, "independent", "15m")

    assert result == "ps_latest_same_timestamp"
    sql = str(session.execute.call_args.args[0])
    assert "ORDER BY created_at DESC, id DESC" in sql


def test_parameter_set_lineage_uses_deterministic_history_order() -> None:
    session = _make_session(
        [
            SimpleNamespace(
                param_values={"entry_threshold": 0.4},
                source_round_id="round_target",
            ),
            SimpleNamespace(recommendation_id="rec_latest_same_timestamp"),
        ]
    )

    result = db_get_parameter_set_values(
        session,
        "ps_target",
        family="independent",
        timeframe="15m",
    )

    assert result is not None
    assert result["approval_recommendation_id"] == "rec_latest_same_timestamp"
    sql = str(session.execute.call_args_list[1].args[0])
    assert "ORDER BY created_at DESC, id DESC" in sql


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


def test_validate_rejects_draft_target() -> None:
    """Rule 2: draft not in allowlist."""
    session = _make_session([SimpleNamespace(status="draft", deprecated_at=None)])
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_draft"
    )
    assert ok is False
    assert reason == "target_status_illegal:draft"


def test_validate_rejects_candidate_target() -> None:
    """Rule 2: candidate not in allowlist."""
    session = _make_session([SimpleNamespace(status="candidate", deprecated_at=None)])
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_cand"
    )
    assert ok is False
    assert reason == "target_status_illegal:candidate"


def test_validate_rejects_ancient_deprecated_target() -> None:
    """Bug 8: deprecated_at > 30 days ago → 拒绝 (business context changed)."""
    from datetime import datetime, timedelta, timezone

    ancient = datetime.now(timezone.utc) - timedelta(days=31)
    session = _make_session(
        [SimpleNamespace(status="deprecated", deprecated_at=ancient)]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_old_deprecated"
    )
    assert ok is False
    assert reason.startswith("target_deprecated_too_old:")


def test_validate_rejects_deprecated_without_timestamp() -> None:
    """Bug 8: deprecated_at IS NULL → 保守视为太老，拒绝."""
    session = _make_session(
        [SimpleNamespace(status="deprecated", deprecated_at=None)]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_dep_no_ts"
    )
    assert ok is False
    assert reason == "target_deprecated_without_timestamp"


def test_validate_accepts_recent_deprecated_target() -> None:
    """Bug 8: deprecated_at <= 30 days ago → 通过 (经规则 4/5/6 验证).

    模拟 Bug 9 机制下的典型场景：apply 新参数时把同 combo 旧 released 降级为
    deprecated，下次 observation 判定回滚时 target 是 deprecated but 刚发生。
    """
    from datetime import datetime, timedelta, timezone

    recent = datetime.now(timezone.utc) - timedelta(hours=6)
    session = _make_session(
        [
            SimpleNamespace(status="deprecated", deprecated_at=recent),  # rule 1+2
            SimpleNamespace(recommendation_id="rec_target"),  # rule 4
            SimpleNamespace(parameter_set_id="ps_current_active"),  # rule 5
            SimpleNamespace(),  # rule 6: approval chain
            None,  # rule 7: no known-bad effectiveness
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_recent_deprecated"
    )
    assert ok is True
    assert reason == ""


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
            SimpleNamespace(recommendation_id="rec_target"),
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
            SimpleNamespace(recommendation_id="rec_target"),
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
    """Happy path: all 7 rules pass."""
    session = _make_session(
        [
            SimpleNamespace(status="frozen"),
            SimpleNamespace(recommendation_id="rec_target"),
            SimpleNamespace(parameter_set_id="ps_current"),
            SimpleNamespace(recommendation_id="rec_target"),
            None,
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_prev"
    )
    assert ok is True
    assert reason == ""
    # All 5 queries must run for the happy path.
    assert session.execute.call_count == 5


def test_validate_accepts_released_status() -> None:
    """Rule 2 allowlist includes 'released'."""
    session = _make_session(
        [
            SimpleNamespace(status="released"),
            SimpleNamespace(recommendation_id="rec_target"),
            SimpleNamespace(parameter_set_id="ps_current"),
            SimpleNamespace(recommendation_id="rec_target"),
            None,
        ]
    )
    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_released"
    )
    assert ok is True
    assert reason == ""


def test_validate_accepts_superseded_recommendation_from_successful_apply_lineage() -> None:
    """rec1→ps1 then rec2 supersedes rec1 must still permit ps2→ps1 rollback."""
    # A recently deprecated predecessor is the real second-release rollback
    # shape; supply a timezone-aware timestamp so the age gate passes.
    from datetime import datetime, timezone

    session = _make_session([
        SimpleNamespace(
            status="deprecated",
            deprecated_at=datetime.now(timezone.utc),
        ),
        SimpleNamespace(recommendation_id="rec_first_release"),
        SimpleNamespace(parameter_set_id="ps_second_release"),
        SimpleNamespace(value=1),  # SQL row for status='superseded'
        None,
    ])

    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_first_release"
    )

    assert ok is True
    assert reason == ""
    lineage_sql = str(session.execute.call_args_list[3].args[0])
    lineage_params = session.execute.call_args_list[3].args[1]
    assert "'superseded'" in lineage_sql
    assert lineage_params["recommendation_id"] == "rec_first_release"


def test_validate_rejects_parameter_set_with_known_bad_effectiveness() -> None:
    """A cancelled old action does not make its immutable bad target safe again."""
    session = _make_session(
        [
            SimpleNamespace(status="frozen"),
            SimpleNamespace(recommendation_id="rec_target"),
            SimpleNamespace(parameter_set_id="ps_current"),
            SimpleNamespace(value=1),
            SimpleNamespace(release_id="rel_known_bad"),
        ]
    )

    ok, reason = validate_rollback_target(
        session, "independent", "15m", "ps_known_bad"
    )

    assert ok is False
    assert reason == "target_has_known_bad_effectiveness:rel_known_bad"


def test_rollback_cannot_reactivate_cancelled_but_known_bad_parameter_set(
    tmp_path: Path,
    patch_environment: None,
) -> None:
    """R1 bad→manual R0→cancel intent must not permit a later rollback to R1."""
    session = _make_session([
        # rollback transaction reads the current active row first
        SimpleNamespace(parameter_set_id="ps_safe_current"),
        # validator rules 1/4/5/6 then the known-bad lineage veto
        SimpleNamespace(status="frozen", deprecated_at=None),
        SimpleNamespace(recommendation_id="rec_bad"),
        SimpleNamespace(parameter_set_id="ps_safe_current"),
        SimpleNamespace(value=1),
        SimpleNamespace(release_id="rel_bad_cancelled_action"),
    ])
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    upsert = MagicMock()
    history = MagicMock()
    with (
        patch("aats.data_platform.db.get_session", return_value=session),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            upsert,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
            history,
        ),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_known_bad",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "VALIDATION_FAILED"
    assert result["reason"] == (
        "target_has_known_bad_effectiveness:rel_bad_cancelled_action"
    )
    upsert.assert_not_called()
    history.assert_not_called()


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
    monkeypatch.setattr(
        "aats.data_platform.governance.active_params_db."
        "db_try_acquire_parameter_apply_lock",
        lambda *_args, **_kwargs: True,
    )


def test_rollback_fails_closed_when_combo_mutation_lock_is_busy(
    tmp_path: Path,
    patch_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aats.data_platform.governance.active_params_db."
        "db_try_acquire_parameter_apply_lock",
        lambda *_args, **_kwargs: False,
    )
    with patch(
        "aats.data_platform.db.get_session",
        side_effect=lambda: _RollbackSessionStub(current_active="ps_current"),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_previous",
        )

    assert result["ok"] is False
    assert result["code"] == "parameter_mutation_conflict"


def test_clear_fails_closed_when_combo_mutation_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aats.data_platform.governance.active_params_db."
        "db_try_acquire_parameter_apply_lock",
        lambda *_args, **_kwargs: False,
    )
    with patch(
        "aats.data_platform.db.get_session",
        side_effect=lambda: _RollbackSessionStub(current_active="ps_current"),
    ):
        result = clear_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
        )

    assert result["ok"] is False
    assert result["code"] == "parameter_mutation_conflict"


def test_clear_executes_normally_after_acquiring_combo_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clear path remains independent from rollback-only CAS arguments."""
    clear_mock = MagicMock()
    history_mock = MagicMock()
    monkeypatch.setattr(
        "aats.data_platform.governance.active_params_db."
        "db_try_acquire_parameter_apply_lock",
        lambda *_args, **_kwargs: True,
    )
    with (
        patch(
            "aats.data_platform.db.get_session",
            side_effect=lambda: _RollbackSessionStub(current_active="ps_current"),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_clear_active_set",
            clear_mock,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
            history_mock,
        ),
    ):
        result = clear_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
        )

    assert result["ok"] is True
    clear_mock.assert_called_once()
    history_mock.assert_called_once()


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


def test_auto_rollback_rejects_same_parameter_set_with_newer_release_lineage(
    tmp_path: Path,
    patch_environment: None,
) -> None:
    session = _make_session([
        SimpleNamespace(
            parameter_set_id="ps_shared",
            approval_recommendation_id="rec_new",
        ),
    ])
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    with (
        patch("aats.data_platform.db.get_session", return_value=session),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set"
        ) as upsert,
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history"
        ) as history,
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_safe",
            expected_from_parameter_set_id="ps_shared",
            expected_from_recommendation_id="rec_old",
            expected_previous_parameter_set_id="ps_safe",
            trigger_release_id="rel_old",
        )

    assert result["ok"] is False
    assert result["code"] == "ACTIVE_SET_CHANGED"
    assert result["reason"] == "expected_current_recommendation_mismatch"
    upsert.assert_not_called()
    history.assert_not_called()


def test_auto_rollback_missing_active_recommendation_requires_reconciliation(
    tmp_path: Path,
    patch_environment: None,
) -> None:
    session = _make_session([
        SimpleNamespace(
            parameter_set_id="ps_bad",
            approval_recommendation_id=None,
        ),
    ])
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    with (
        patch("aats.data_platform.db.get_session", return_value=session),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set"
        ) as upsert,
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history"
        ) as history,
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_safe",
            expected_from_parameter_set_id="ps_bad",
            expected_from_recommendation_id="rec_bad",
            expected_previous_parameter_set_id="ps_safe",
            trigger_release_id="rel_bad",
        )

    assert result["ok"] is False
    assert result["code"] == "RELEASE_LINEAGE_INVALID"
    assert result["reason"] == "current_active_recommendation_lineage_missing"
    upsert.assert_not_called()
    history.assert_not_called()


@pytest.mark.parametrize("apply_result", ["failed", "pending", "success"])
def test_auto_rollback_rejects_non_success_or_stale_predecessor_lineage(
    tmp_path: Path,
    patch_environment: None,
    apply_result: str,
) -> None:
    session = _make_session([
        SimpleNamespace(
            parameter_set_id="ps_bad",
            approval_recommendation_id="rec_bad",
        ),
        SimpleNamespace(
            release_id="rel_bad",
            family="independent",
            timeframe="15m",
            combo_key="independent_15m",
            recommendation_id="rec_bad",
            parameter_set_id="ps_bad",
            previous_parameter_set_id="ps_stale",
            apply_result=apply_result,
            lineage_count=1,
            history_family="independent",
            history_timeframe="15m",
            history_from_parameter_set_id="ps_actual",
            history_to_parameter_set_id="ps_bad",
        ),
    ])
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    with (
        patch("aats.data_platform.db.get_session", return_value=session),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set"
        ) as upsert,
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history"
        ) as history,
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_stale",
            expected_from_parameter_set_id="ps_bad",
            expected_from_recommendation_id="rec_bad",
            expected_previous_parameter_set_id="ps_stale",
            trigger_release_id="rel_bad",
        )

    assert result["ok"] is False
    assert result["code"] == "RELEASE_LINEAGE_INVALID"
    assert result["reason"] == "release_apply_history_lineage_mismatch"
    upsert.assert_not_called()
    history.assert_not_called()
