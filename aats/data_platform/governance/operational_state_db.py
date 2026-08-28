"""DB helpers for RDP operational state.

These tables back the operational control plane state that used to be
stored only in artifact JSON files. Callers should keep their existing
registry-like payload shapes; this module only persists and restores them.
"""

from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import ADVISORY_LOCK_KEYS, json_dumps, parse_dt
from ._exceptions import DBConflictError
from ._time_util import parse_iso_datetime_utc
from .typed_json_identity import canonical_typed_json_bytes, typed_json_sha256

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _strict_evidence_timestamp(value: Any, *, context: str) -> datetime:
    """Parse an exact UTC, timezone-aware evidence timestamp."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{context} must be timezone-aware")
    elif isinstance(value, str):
        token = value.strip()
        if not token or not (token.endswith("Z") or token.endswith("+00:00")):
            raise ValueError(f"{context} must use canonical UTC offset")
    else:
        raise ValueError(f"{context} is required")
    parsed = parse_iso_datetime_utc(value, context=context)
    if parsed is None:
        raise ValueError(f"{context} is required")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > _utcnow() + timedelta(minutes=5):
        raise ValueError(f"{context} is implausibly in the future")
    return parsed


def _evidence_fingerprint(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("evidence_fingerprint", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_combo_fields(
    data: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any]]:
    """归一化 timeframe / combo_key，并返回同步后的 payload 数据。

    M5 修复统一版：历史上 parameter_releases 做了 column + payload 同归一，
    但 observation_results / rollback_recommendations / release_effectiveness
    三张表只把 column lower()、payload JSON 原封 json_dumps(result)，于是 caller
    传 "1H" 时列是 "1h"、payload.timeframe 仍是 "1H"，读者从 payload 反序列化
    会读到与列不一致的值。把 4 个 upsert 路径收敛到同一个 helper，避免以后
    新增表又漏修。

    Returns
    -------
    (timeframe_norm, combo_key_norm_or_None, data_for_payload)
      * ``timeframe_norm`` 永远是 str；caller 没传时为 ``""``.
      * ``combo_key_norm`` 为 ``None`` 表示 caller 未传且 family+timeframe 不足以
        合成；caller 可以直接把它绑到 column（列允许 NULL）.
      * ``data_for_payload`` 是浅拷贝；timeframe / combo_key 已就地归一，其余字段原样保留.
    """
    timeframe_norm = str(data.get("timeframe") or "").lower()
    family_norm = str(data.get("family") or "")
    combo_key_raw = data.get("combo_key")
    if combo_key_raw:
        combo_key_norm: str | None = str(combo_key_raw).lower()
    elif family_norm and timeframe_norm:
        combo_key_norm = f"{family_norm}_{timeframe_norm}".lower()
    else:
        combo_key_norm = None

    data_for_payload = dict(data)
    data_for_payload["timeframe"] = timeframe_norm
    if combo_key_norm is not None:
        data_for_payload["combo_key"] = combo_key_norm
    return timeframe_norm, combo_key_norm, data_for_payload


# Advisory lock keys 统一走 _db_util.ADVISORY_LOCK_KEYS 注册表：历史上
# magic number 散落在各模块，一旦出现第三条调度路径要加锁就可能撞上同一
# key 却无感。现在新增 lock 请在 _db_util 里登记，本地只保留 alias。
_SCHEDULER_ADVISORY_LOCK_KEY = ADVISORY_LOCK_KEYS["governance_scheduler_singleton"]
_RELEASE_CYCLE_ADVISORY_LOCK_KEY = ADVISORY_LOCK_KEYS["release_cycle_per_release"]


def try_acquire_scheduler_lock(session: Session) -> bool:
    """尝试获取 scheduler 的 session 级 advisory lock。

    返回 True 表示当前 Postgres session（连接）独占调度权；返回 False 表示
    已有其他 scheduler 在运行，调用方应跳过本轮 enqueue。
    使用 pg_try_advisory_lock（非 xact 版本）——锁绑定在连接上，跨事务有效，
    必须显式调用 release_scheduler_lock 或关闭 session 才会释放。
    因为 scheduler 的 load→compute→save 跨多个事务，必须用 session 级锁。
    """
    row = session.execute(
        text("SELECT pg_try_advisory_lock(:key) AS acquired"),
        {"key": _SCHEDULER_ADVISORY_LOCK_KEY},
    ).fetchone()
    acquired = bool(row and row.acquired)
    if acquired:
        # 必须在持锁 session 上显式 commit，否则锁的绑定关系
        # 会在连接归还 pool 时失效。
        session.commit()
    return acquired


def release_scheduler_lock(session: Session) -> None:
    """释放由 try_acquire_scheduler_lock 获取的 session 级 advisory lock。

    调用失败不抛异常——释放是 best-effort，session 关闭时 Postgres 也会自动释放。
    M-A3-3 修复：历史 ``except Exception: pass`` 静默吞异常，当 DB 抖动 /
    transaction 已 abort / 连接被 reset 时释放失败的信号就彻底丢了，但这种
    场景又恰是 scheduler 互斥状态最容易混乱的时候（多个 scheduler 同时去
    抢同一把锁、锁被谁持有变得不可观测）。改为 warning 级别打印异常类型，
    依然不向上抛，保持 best-effort 语义。
    """
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": _SCHEDULER_ADVISORY_LOCK_KEY},
        )
        session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("advisory lock release failed (scheduler): %s", exc)


def try_acquire_release_cycle_lock(session: Session) -> bool:
    """尝试获取 release_cycle 的 session 级 advisory lock。

    Release cycle 的 candidate 选取和 create_parameter_release 跨多个事务，
    若两个 run_release_cycle 并发运行可能对同一条 approved recommendation
    重复发布。锁定整个 release_cycle 调用是最简单且正确的防护方式。
    """
    row = session.execute(
        text("SELECT pg_try_advisory_lock(:key) AS acquired"),
        {"key": _RELEASE_CYCLE_ADVISORY_LOCK_KEY},
    ).fetchone()
    acquired = bool(row and row.acquired)
    if acquired:
        session.commit()
    return acquired


def release_release_cycle_lock(session: Session) -> None:
    """释放 release_cycle advisory lock。与 release_scheduler_lock 同理，
    把静默吞异常改成 warning 级可观测。"""
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": _RELEASE_CYCLE_ADVISORY_LOCK_KEY},
        )
        session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("advisory lock release failed (release_cycle): %s", exc)


def _with_payload(payload: Any, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        result.update(payload)
    result.update({key: value for key, value in fields.items() if value is not None})
    return result


def _with_authoritative_columns(payload: Any, **columns: Any) -> dict[str, Any]:
    """Overlay normalized DB columns on an embedded JSON payload.

    Operational tables intentionally retain the original report/result JSON for
    audit detail, but their promoted scalar columns are the queryable state
    truth.  Unlike :func:`_with_payload`, this helper must also copy ``None``:
    a nullable canonical column (for example ``release_id``) must be able to
    clear a stale value that remains in an older payload.
    """
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        result.update(payload)
    result.update(columns)
    return result


def _isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


_RELEASE_APPLY_STATUSES = {
    "pending",
    "blocked_by_gate",
    "success",
    "failed",
}
_RELEASE_APPLY_TERMINAL = {"blocked_by_gate", "success", "failed"}
_RELEASE_OBSERVATION_RANK = {
    "pending": 0,
    "observing": 1,
    "completed": 2,
    "rollback_recommended": 3,
    "rolled_back": 4,
}
_RELEASE_IDENTITY_FIELDS = (
    "release_id",
    "family",
    "timeframe",
    "combo_key",
    "recommendation_id",
    "parameter_set_id",
    "actor",
    "gate_result_ref",
    "created_at",
)
_RELEASE_ROLLBACK_AUDIT_FIELDS = (
    "rolled_back_at",
    "rollback_to_parameter_set_id",
    "rollback_operation_id",
    "rollback_capital_proof_version",
    "rollback_capital_proof_verified",
)
_RELEASE_ROLLBACK_CAPITAL_PROOF_VERSION = (
    "rdp-release-rollback-capital-proof/v1"
)
_RELEASE_APPLY_RECONCILIATION_FIELDS = (
    "apply_reconciliation_required",
    "apply_reconciliation_reason",
)
_RELEASE_APPLY_AUDIT_FIELDS = ("apply_operation_id", "applied_at")


def _merge_parameter_release_state(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge one release under a row lock without allowing state regression.

    Release/history files are audit snapshots and may be stale.  This helper is
    deliberately conservative: identity is immutable, apply terminal states do
    not change, observation state can only move forward, and the first rollback
    audit anchor wins.
    """
    for label, record in (("stored", existing), ("incoming", incoming)):
        if "apply_reconciliation_required" in record and type(
            record.get("apply_reconciliation_required")
        ) is not bool:
            raise ValueError(
                f"{label} apply_reconciliation_required must be exact boolean"
            )
        if (
            record.get("apply_reconciliation_reason") is not None
            and record.get("apply_reconciliation_required") is not True
        ):
            raise ValueError(
                f"{label} reconciliation reason requires exact true flag"
            )
        if (
            str(record.get("apply_result") or "pending") == "pending"
            and record.get("apply_operation_id") is not None
        ):
            raise ValueError(
                f"{label} pending release cannot already have apply_operation_id"
            )
    for field in _RELEASE_IDENTITY_FIELDS:
        old = existing.get(field)
        new = incoming.get(field)
        if old is not None and new is not None and old != new:
            raise ValueError(
                f"parameter release immutable field changed: {field}"
            )

    current_apply = str(existing.get("apply_result") or "pending")
    next_apply = str(incoming.get("apply_result") or current_apply)
    if current_apply not in _RELEASE_APPLY_STATUSES:
        raise ValueError(f"invalid stored release apply_result: {current_apply}")
    if next_apply not in _RELEASE_APPLY_STATUSES:
        raise ValueError(f"invalid incoming release apply_result: {next_apply}")
    if current_apply == "success":
        operation_id = existing.get("apply_operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError(
                "stored successful release is missing apply_operation_id"
            )
        applied_at = _strict_evidence_timestamp(
            existing.get("applied_at"),
            context="stored_parameter_release.applied_at",
        )
        created_at = _strict_evidence_timestamp(
            existing.get("created_at"),
            context="stored_parameter_release.created_at",
        )
        if applied_at < created_at:
            raise ValueError(
                "stored successful release applied_at precedes created_at"
            )

    current_observation = str(existing.get("observation_status") or "pending")
    next_observation = str(
        incoming.get("observation_status") or current_observation
    )
    if current_observation not in _RELEASE_OBSERVATION_RANK:
        raise ValueError(
            "invalid stored release observation_status: "
            f"{current_observation}"
        )
    if next_observation not in _RELEASE_OBSERVATION_RANK:
        raise ValueError(
            "invalid incoming release observation_status: "
            f"{next_observation}"
        )

    merged = dict(existing)
    for field in _RELEASE_IDENTITY_FIELDS:
        if merged.get(field) is None and incoming.get(field) is not None:
            merged[field] = incoming[field]

    reconciliation_locked = (
        existing.get("apply_reconciliation_required") is True
    )
    if current_apply in _RELEASE_APPLY_TERMINAL or reconciliation_locked:
        resolved_apply = current_apply
    else:
        resolved_apply = next_apply
    merged["apply_result"] = resolved_apply

    if current_apply == "pending" and not reconciliation_locked:
        if resolved_apply == "success":
            # Only the combo-locked apply transaction may supply this value.
            operation_id = incoming.get("apply_operation_id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise ValueError(
                    "successful release transition requires apply_operation_id"
                )
            applied_at = _strict_evidence_timestamp(
                incoming.get("applied_at"),
                context="parameter_release.applied_at",
            )
            created_at = _strict_evidence_timestamp(
                existing.get("created_at"),
                context="parameter_release.created_at",
            )
            if applied_at < created_at:
                raise ValueError(
                    "parameter release applied_at cannot precede created_at"
                )
            merged["previous_parameter_set_id"] = incoming.get(
                "previous_parameter_set_id"
            )
            merged["apply_operation_id"] = operation_id
            merged["applied_at"] = applied_at.isoformat()
        for field in _RELEASE_APPLY_RECONCILIATION_FIELDS:
            if field in incoming:
                merged[field] = incoming[field]
    if reconciliation_locked:
        merged["apply_reconciliation_required"] = True
        if existing.get("apply_reconciliation_reason") is not None:
            merged["apply_reconciliation_reason"] = existing[
                "apply_reconciliation_reason"
            ]
    if current_apply == "success":
        for field in _RELEASE_APPLY_AUDIT_FIELDS:
            old = existing.get(field)
            new = incoming.get(field)
            if old is not None and new is not None and old != new:
                raise ValueError(
                    f"parameter release immutable apply audit field changed: {field}"
                )
            if old is not None:
                merged[field] = old

    if (
        _RELEASE_OBSERVATION_RANK[next_observation]
        >= _RELEASE_OBSERVATION_RANK[current_observation]
    ):
        resolved_observation = next_observation
    else:
        resolved_observation = current_observation
    merged["observation_status"] = resolved_observation

    if (
        resolved_observation == "rolled_back"
        and current_observation != "rolled_back"
    ):
        for field in _RELEASE_ROLLBACK_AUDIT_FIELDS:
            if incoming.get(field) is not None:
                merged[field] = incoming[field]
    elif current_observation == "rolled_back":
        for field in _RELEASE_ROLLBACK_AUDIT_FIELDS:
            if existing.get(field) is not None:
                merged[field] = existing[field]

    # Non-state metadata is first-write-wins.  Mutable lifecycle data must use
    # the explicit transitions above instead of replaying an arbitrary payload.
    for field in ("gate_status", "observation_window_hours", "notes"):
        if merged.get(field) is None and incoming.get(field) is not None:
            merged[field] = incoming[field]
    return merged


_EFFECTIVENESS_ACTION_STATUSES = {
    "pending",
    "in_progress",
    "enforced",
    "cancelled",
    "reconciliation_required",
}
_EFFECTIVENESS_ACTION_TERMINAL = {
    "enforced",
    "cancelled",
    "reconciliation_required",
}
_EFFECTIVENESS_CAPITAL_PROOF_VERSION = "rdp-rollback-capital-proof/v1"
_EFFECTIVENESS_ACTION_FIELDS = (
    "rollback_enforced",
    "rollback_enforced_at",
    "rollback_to_parameter_set_id",
    "rollback_cancelled",
    "rollback_cancelled_at",
    "rollback_cancelled_reason",
    "rollback_enforcement_status",
    "rollback_enforcement_attempt_id",
    "rollback_enforcement_started_at",
    "rollback_enforcement_finished_at",
    "rollback_reconciliation_reason",
    "rollback_attempts",
    "last_rollback_error",
    "rollback_soft_pause_applied",
    "rollback_capital_proof_version",
    "rollback_capital_proof_kind",
    "rollback_capital_operation_id",
    "rollback_capital_proof_active_parameter_set_id",
    "rollback_capital_proof_decision_status",
    "rollback_capital_proof_verified",
)
_EFFECTIVENESS_PRIOR_ACTION_ANCHORS = (
    "rollback_enforcement_attempt_id",
    "rollback_enforcement_started_at",
    "rollback_enforcement_finished_at",
    "rollback_enforced_at",
    "rollback_cancelled_at",
    "rollback_cancelled_reason",
    "rollback_to_parameter_set_id",
    "last_rollback_error",
    "rollback_reconciliation_reason",
    "rollback_capital_proof_version",
    "rollback_capital_proof_kind",
    "rollback_capital_operation_id",
    "rollback_capital_proof_active_parameter_set_id",
    "rollback_capital_proof_decision_status",
)


def _effectiveness_has_prior_action_anchor(evaluation: dict[str, Any]) -> bool:
    if "rollback_attempts" in evaluation:
        attempts = evaluation.get("rollback_attempts")
        if type(attempts) is not int or attempts != 0:
            return True
    if evaluation.get("rollback_soft_pause_applied", False) is True:
        return True
    return any(
        evaluation.get(key) is not None and evaluation.get(key) != ""
        for key in _EFFECTIVENESS_PRIOR_ACTION_ANCHORS
    )


def _effectiveness_action_status(evaluation: dict[str, Any]) -> str:
    invalid_flags = [
        key
        for key in (
            "rollback_enforced",
            "rollback_cancelled",
            "rollback_soft_pause_applied",
        )
        if key in evaluation and type(evaluation[key]) is not bool
    ]
    if invalid_flags:
        raise ValueError(
            "effectiveness rollback flag must be an exact bool: "
            + ",".join(sorted(invalid_flags))
        )
    raw = evaluation.get("rollback_enforcement_status")
    if raw is not None and type(raw) is not str:
        raise ValueError(
            "rollback_enforcement_status must be a string or null"
        )
    enforced = evaluation.get("rollback_enforced", False)
    cancelled = evaluation.get("rollback_cancelled", False)
    soft_pause_applied = evaluation.get("rollback_soft_pause_applied", False)
    cancelled_reason = evaluation.get("rollback_cancelled_reason")
    soft_pause_reason = (
        isinstance(cancelled_reason, str)
        and cancelled_reason.startswith("soft_paused_")
    )
    if enforced and cancelled:
        raise ValueError("effectiveness action cannot be enforced and cancelled")
    enforced_is_consistent = (
        enforced is True
        and cancelled is False
        and soft_pause_applied is False
        and not soft_pause_reason
    )
    cancelled_is_consistent = (
        cancelled is True
        and enforced is False
        and (
            (soft_pause_applied is True and soft_pause_reason)
            or (soft_pause_applied is False and not soft_pause_reason)
        )
    )
    if raw is None:
        # A pre-contract row that carries only a terminal boolean has no
        # attempt/timestamp/capital lineage.  It is an unresolved legacy claim,
        # never a compatibility terminal: treating it as resolved can reopen
        # the combo for a new capital mutation without proving the old action.
        if enforced:
            if not enforced_is_consistent:
                raise ValueError("enforced action has inconsistent soft-pause audit")
            return "reconciliation_required"
        if cancelled:
            if not cancelled_is_consistent:
                raise ValueError("cancelled action has unproven soft-pause audit")
            return "reconciliation_required"
        return "pending"
    status = str(raw).strip().lower()
    if status not in _EFFECTIVENESS_ACTION_STATUSES:
        raise ValueError(f"invalid rollback_enforcement_status: {status}")
    if status == "enforced" and not enforced_is_consistent:
        raise ValueError("enforced action requires only rollback_enforced=true")
    if status == "cancelled" and not cancelled_is_consistent:
        raise ValueError("cancelled action requires only rollback_cancelled=true")
    if status not in {"enforced", "cancelled"} and (enforced or cancelled):
        raise ValueError(
            f"{status} action cannot carry resolved rollback boolean flags"
        )
    return status


def validate_effectiveness_terminal_proof_shape(
    evaluation: dict[str, Any],
    *,
    status: str,
    require_db_verified: bool,
) -> None:
    """Validate the versioned terminal-action audit contract.

    This is the structural half of the proof.  The DB writer additionally
    re-derives the claimed capital outcome from canonical tables while holding
    the combo mutation lock, and only that writer may add
    ``rollback_capital_proof_verified=true``.
    """

    if status not in {"enforced", "cancelled"}:
        raise ValueError("capital proof is only valid for terminal actions")
    if (
        evaluation.get("rollback_capital_proof_version")
        != _EFFECTIVENESS_CAPITAL_PROOF_VERSION
    ):
        raise ValueError("terminal rollback is missing the capital proof contract")
    if require_db_verified and evaluation.get(
        "rollback_capital_proof_verified"
    ) is not True:
        raise ValueError("terminal rollback lacks canonical DB verification")

    attempt_id = evaluation.get("rollback_enforcement_attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("terminal rollback is missing attempt id")
    started_at = _effectiveness_action_timestamp(
        evaluation, "rollback_enforcement_started_at"
    )
    finished_at = _effectiveness_action_timestamp(
        evaluation, "rollback_enforcement_finished_at"
    )
    if finished_at < started_at:
        raise ValueError("terminal rollback finished before it started")

    proof_kind = evaluation.get("rollback_capital_proof_kind")
    operation_id = evaluation.get("rollback_capital_operation_id")
    active_parameter_set_id = evaluation.get(
        "rollback_capital_proof_active_parameter_set_id"
    )
    decision_status = evaluation.get("rollback_capital_proof_decision_status")
    if status == "enforced":
        enforced_at = _effectiveness_action_timestamp(
            evaluation, "rollback_enforced_at"
        )
        target = evaluation.get("rollback_to_parameter_set_id")
        if enforced_at != finished_at:
            raise ValueError("enforced rollback audit time must equal finished_at")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("enforced rollback is missing target parameter set")
        if proof_kind != "rollback":
            raise ValueError("enforced rollback has invalid capital proof kind")
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("enforced rollback is missing capital operation id")
        if evaluation.get("rollback_soft_pause_applied") is not False:
            raise ValueError("enforced rollback requires explicit soft-pause=false")
        if active_parameter_set_id is not None or decision_status is not None:
            raise ValueError("enforced rollback carries incompatible proof fields")
        return

    cancelled_at = _effectiveness_action_timestamp(
        evaluation, "rollback_cancelled_at"
    )
    reason = evaluation.get("rollback_cancelled_reason")
    if cancelled_at != finished_at:
        raise ValueError("cancelled rollback audit time must equal finished_at")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("cancelled rollback is missing reason")
    if operation_id is not None:
        raise ValueError("cancelled rollback cannot claim a capital operation")
    if reason.startswith("active_parameter_set_changed_before_rollback:"):
        if proof_kind != "active_parameter_changed":
            raise ValueError("active-change cancellation has invalid proof kind")
        if (
            not isinstance(active_parameter_set_id, str)
            or not active_parameter_set_id.strip()
        ):
            raise ValueError("active-change cancellation lacks observed active set")
        if evaluation.get("rollback_soft_pause_applied") is not False:
            raise ValueError(
                "active-change cancellation requires explicit soft-pause=false"
            )
        if decision_status is not None:
            raise ValueError("active-change cancellation carries decision proof")
        return
    if reason.startswith("soft_paused_no_valid_rollback_target:"):
        if proof_kind != "soft_pause":
            raise ValueError("soft-pause cancellation has invalid proof kind")
        if evaluation.get("rollback_soft_pause_applied") is not True:
            raise ValueError("soft-pause cancellation lacks applied=true")
        if decision_status != "pause":
            raise ValueError("soft-pause cancellation lacks pause decision proof")
        if active_parameter_set_id is not None:
            raise ValueError("soft-pause cancellation carries active-change proof")
        return
    raise ValueError("cancelled rollback reason has no canonical proof semantics")


def _effectiveness_action_timestamp(
    record: dict[str, Any],
    field: str,
) -> datetime:
    return _strict_evidence_timestamp(
        record.get(field),
        context=f"release_effectiveness.{field}",
    )


def _validate_effectiveness_action_transition(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    current_status: str,
    incoming_status: str,
) -> None:
    """Validate proof-bearing rollback-action state transitions.

    Effectiveness evaluation is a general evidence writer; it must never be
    able to fabricate a terminal capital outcome.  A terminal state is valid
    only after the same persisted ``in_progress`` attempt resolves with a
    complete, ordered audit trail.
    """

    current_attempt = existing.get("rollback_enforcement_attempt_id")
    incoming_attempt = incoming.get("rollback_enforcement_attempt_id")

    if not existing:
        if incoming_status in {"in_progress", "enforced", "cancelled"}:
            raise ValueError(
                "new effectiveness row cannot start with an action outcome"
            )
        if incoming_status == "reconciliation_required" and not str(
            incoming.get("rollback_reconciliation_reason") or ""
        ).strip():
            raise ValueError(
                "reconciliation_required effectiveness needs a reason"
            )
        return

    if current_status == "pending":
        if incoming_status in {"enforced", "cancelled"}:
            raise ValueError(
                "pending rollback action cannot transition directly to terminal"
            )
        if incoming_status == "in_progress":
            if (
                existing.get("conclusion") != "rollback_triggered"
                or not isinstance(incoming_attempt, str)
                or not incoming_attempt.strip()
            ):
                raise ValueError(
                    "rollback claim requires an open obligation and attempt id"
                )
            _effectiveness_action_timestamp(
                incoming, "rollback_enforcement_started_at"
            )
            if incoming.get("rollback_enforcement_finished_at") is not None:
                raise ValueError("new rollback claim cannot already be finished")
        if incoming_status == "reconciliation_required" and not str(
            incoming.get("rollback_reconciliation_reason") or ""
        ).strip():
            raise ValueError(
                "reconciliation_required effectiveness needs a reason"
            )
        return

    if current_status == "in_progress":
        if not isinstance(current_attempt, str) or not current_attempt.strip():
            raise ValueError("stored in-progress rollback is missing attempt id")
        started_at = _effectiveness_action_timestamp(
            existing, "rollback_enforcement_started_at"
        )
        if incoming_status == "pending":
            return
        if incoming_attempt != current_attempt:
            raise ValueError(
                "rollback action transition must retain the claimed attempt id"
            )
        incoming_started_at = _effectiveness_action_timestamp(
            incoming, "rollback_enforcement_started_at"
        )
        if incoming_started_at != started_at:
            raise ValueError("rollback action started_at is immutable")
        if incoming_status == "in_progress":
            return
        if incoming_status == "reconciliation_required":
            if not str(
                incoming.get("rollback_reconciliation_reason") or ""
            ).strip():
                raise ValueError(
                    "reconciliation_required effectiveness needs a reason"
                )
            finished_at = _effectiveness_action_timestamp(
                incoming, "rollback_enforcement_finished_at"
            )
            if finished_at < started_at:
                raise ValueError("rollback action finished before it started")
            return
        if incoming_status not in {"enforced", "cancelled"}:
            raise ValueError(
                f"unsupported rollback action transition: {incoming_status}"
            )
        finished_at = _effectiveness_action_timestamp(
            incoming, "rollback_enforcement_finished_at"
        )
        if finished_at < started_at:
            raise ValueError("rollback action finished before it started")
        if incoming_status == "enforced":
            enforced_at = _effectiveness_action_timestamp(
                incoming, "rollback_enforced_at"
            )
            target = incoming.get("rollback_to_parameter_set_id")
            if (
                enforced_at != finished_at
                or not isinstance(target, str)
                or not target.strip()
            ):
                raise ValueError(
                    "enforced rollback requires matching finish time and target"
                )
        else:
            cancelled_at = _effectiveness_action_timestamp(
                incoming, "rollback_cancelled_at"
            )
            reason = incoming.get("rollback_cancelled_reason")
            if (
                cancelled_at != finished_at
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                raise ValueError(
                    "cancelled rollback requires matching finish time and reason"
                )
        validate_effectiveness_terminal_proof_shape(
            incoming,
            status=incoming_status,
            require_db_verified=False,
        )
        return

    # Existing terminal state is immutable, but it must itself be provable.
    if current_status in {"enforced", "cancelled"}:
        if not isinstance(current_attempt, str) or not current_attempt.strip():
            raise ValueError("stored terminal rollback is missing attempt id")
        started_at = _effectiveness_action_timestamp(
            existing, "rollback_enforcement_started_at"
        )
        finished_at = _effectiveness_action_timestamp(
            existing, "rollback_enforcement_finished_at"
        )
        if finished_at < started_at:
            raise ValueError("stored rollback action finished before it started")
        audit_field = (
            "rollback_enforced_at"
            if current_status == "enforced"
            else "rollback_cancelled_at"
        )
        if _effectiveness_action_timestamp(existing, audit_field) != finished_at:
            raise ValueError("stored terminal rollback audit time is inconsistent")
        if current_status == "enforced" and not str(
            existing.get("rollback_to_parameter_set_id") or ""
        ).strip():
            raise ValueError("stored enforced rollback is missing target")
        if current_status == "cancelled" and not str(
            existing.get("rollback_cancelled_reason") or ""
        ).strip():
            raise ValueError("stored cancelled rollback is missing reason")
        validate_effectiveness_terminal_proof_shape(
            existing,
            status=current_status,
            require_db_verified=True,
        )


def _merge_release_effectiveness_state(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge an effectiveness row with monotonic rollback-action semantics."""
    for field in ("release_id", "family", "timeframe"):
        old = existing.get(field)
        new = incoming.get(field)
        if old is not None and new is not None and old != new:
            raise ValueError(
                f"release effectiveness immutable field changed: {field}"
            )

    current_status = _effectiveness_action_status(existing)
    incoming_status = _effectiveness_action_status(incoming)
    _validate_effectiveness_action_transition(
        existing,
        incoming,
        current_status=current_status,
        incoming_status=incoming_status,
    )
    current_attempt = existing.get("rollback_enforcement_attempt_id")
    incoming_attempt = incoming.get("rollback_enforcement_attempt_id")
    same_attempt = bool(
        current_attempt
        and incoming_attempt
        and current_attempt == incoming_attempt
    )
    if current_status in _EFFECTIVENESS_ACTION_TERMINAL:
        resolved_status = current_status
    elif current_status == "in_progress":
        # A persisted in-progress intent is an idempotency anchor.  Even its
        # owner cannot move it back to pending: doing so would let the enforcer
        # allocate a new attempt and replay a capital action whose outcome is
        # unknown.  The same attempt may only retain ownership or resolve.
        resolved_status = (
            incoming_status
            if same_attempt and incoming_status != "pending"
            else current_status
        )
    else:
        resolved_status = incoming_status

    current_evaluated_at = parse_dt(existing.get("evaluated_at"))
    incoming_evaluated_at = parse_dt(incoming.get("evaluated_at"))
    incoming_is_newer = (
        current_evaluated_at is None
        or (
            incoming_evaluated_at is not None
            and incoming_evaluated_at >= current_evaluated_at
        )
    )

    # A fresh evaluation may replace a still-pending conclusion.  Once a
    # rollback action starts, the original rollback-triggered evidence remains
    # pinned and only action-owned fields may advance.
    rollback_obligation_open = (
        existing.get("conclusion") == "rollback_triggered"
    )
    if (
        current_status == "pending"
        and incoming_is_newer
        and not rollback_obligation_open
    ):
        merged = dict(existing)
        merged.update(incoming)
    else:
        merged = dict(existing)

    # Legacy rows may predate promoted family/timeframe columns.  Identity is
    # immutable once known, but a canonical incoming evaluation is allowed to
    # backfill a missing value under this row lock.
    for field in ("release_id", "family", "timeframe"):
        if merged.get(field) is None and incoming.get(field) is not None:
            merged[field] = incoming[field]

    action_transition_owned = (
        current_status == "pending"
        or (
            current_status == "in_progress"
            and same_attempt
            and incoming_status != "pending"
        )
    )
    if action_transition_owned:
        for field in _EFFECTIVENESS_ACTION_FIELDS:
            if field in incoming:
                merged[field] = incoming[field]
    else:
        for field in _EFFECTIVENESS_ACTION_FIELDS:
            if field in existing:
                merged[field] = existing[field]

    if (
        merged.get("conclusion") == "rollback_triggered"
        and resolved_status == "pending"
        and _effectiveness_has_prior_action_anchor(merged)
    ):
        resolved_status = "reconciliation_required"
        merged["rollback_reconciliation_reason"] = (
            "rollback_prior_action_anchor_present"
        )

    if (
        merged.get("conclusion") == "rollback_triggered"
        and resolved_status not in {"enforced", "cancelled"}
        and (not merged.get("family") or not merged.get("timeframe"))
    ):
        # An unresolved rollback with no combo identity cannot be safely scoped
        # to one apply.  Quarantine it for reconciliation; the pending lookup
        # also treats such orphan rows as a global veto.
        resolved_status = "reconciliation_required"
        merged["rollback_reconciliation_reason"] = "rollback_identity_missing"

    merged["rollback_enforcement_status"] = resolved_status
    if (
        current_status == "reconciliation_required"
        and existing.get("rollback_enforcement_status") is None
        and (
            existing.get("rollback_enforced") is True
            or existing.get("rollback_cancelled") is True
        )
        and not str(merged.get("rollback_reconciliation_reason") or "").strip()
    ):
        merged["rollback_reconciliation_reason"] = (
            "legacy_terminal_without_canonical_capital_proof"
        )
    if current_status != "pending" or rollback_obligation_open:
        merged["conclusion"] = existing.get("conclusion")
        merged["evaluation_id"] = existing.get("evaluation_id")
        merged["evaluated_at"] = existing.get("evaluated_at")
    return merged


def _workflow_run_from_row(row: Any) -> dict[str, Any]:
    return _with_authoritative_columns(
        row.report,
        run_id=row.run_id,
        workflow=row.workflow,
        overall_status=row.overall_status,
        description=row.description,
        started_at=_isoformat_or_none(row.started_at),
        finished_at=_isoformat_or_none(row.finished_at),
        created_at=_isoformat_or_none(row.created_at),
        updated_at=_isoformat_or_none(row.updated_at),
    )


def _pre_apply_gate_result_from_row(row: Any) -> dict[str, Any]:
    return _with_authoritative_columns(
        row.payload,
        gate_run_id=row.gate_run_id,
        recommendation_id=row.recommendation_id,
        release_id=row.release_id,
        allow_apply=row.allow_apply,
        gate_status=row.gate_status,
        total_checks=row.total_checks,
        passed_checks=row.passed_checks,
        created_at=_isoformat_or_none(row.created_at),
        updated_at=_isoformat_or_none(row.updated_at),
    )


def _parameter_release_from_row(row: Any) -> dict[str, Any]:
    return _with_authoritative_columns(
        row.payload,
        release_id=row.release_id,
        family=row.family,
        timeframe=row.timeframe,
        combo_key=row.combo_key,
        recommendation_id=row.recommendation_id,
        parameter_set_id=row.parameter_set_id,
        previous_parameter_set_id=row.previous_parameter_set_id,
        actor=row.actor,
        gate_result_ref=row.gate_result_ref,
        gate_status=row.gate_status,
        apply_result=row.apply_result,
        observation_status=row.observation_status,
        observation_window_hours=row.observation_window_hours,
        notes=row.notes,
        created_at=_isoformat_or_none(row.created_at),
        updated_at=_isoformat_or_none(row.updated_at),
    )


def _observation_result_from_row(row: Any) -> dict[str, Any]:
    return _with_authoritative_columns(
        row.payload,
        release_id=row.release_id,
        family=row.family,
        timeframe=row.timeframe,
        combo_key=row.combo_key,
        status=row.status,
        recommendation=row.recommendation,
        observation_window_hours=row.observation_window_hours,
        window_active=row.window_active,
        started_at=_isoformat_or_none(row.started_at),
        evaluated_at=_isoformat_or_none(row.evaluated_at),
        updated_at=_isoformat_or_none(row.updated_at),
    )


def _rollback_recommendation_from_row(row: Any) -> dict[str, Any]:
    return _with_authoritative_columns(
        row.payload,
        release_id=row.release_id,
        family=row.family,
        timeframe=row.timeframe,
        combo_key=row.combo_key,
        rollback_recommended=row.rollback_recommended,
        severity=row.severity,
        suggested_target_parameter_set_id=row.suggested_target_parameter_set_id,
        evaluated_at=_isoformat_or_none(row.evaluated_at),
        updated_at=_isoformat_or_none(row.updated_at),
    )


def _release_effectiveness_from_row(row: Any) -> dict[str, Any]:
    return _with_authoritative_columns(
        row.payload,
        evaluation_id=row.evaluation_id,
        release_id=row.release_id,
        family=row.family,
        timeframe=row.timeframe,
        conclusion=row.conclusion,
        evaluated_at=_isoformat_or_none(row.evaluated_at),
        updated_at=_isoformat_or_none(row.updated_at),
    )


def db_upsert_workflow_run_report(session: Session, report: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO governance.workflow_run_reports
                (run_id, workflow, overall_status, description, report,
                 started_at, finished_at, updated_at)
            VALUES
                (:run_id, :workflow, :overall_status, :description, CAST(:report AS jsonb),
                 :started_at, :finished_at, :updated_at)
            ON CONFLICT (run_id) DO UPDATE SET
                workflow = EXCLUDED.workflow,
                overall_status = EXCLUDED.overall_status,
                description = EXCLUDED.description,
                report = EXCLUDED.report,
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "run_id": report.get("run_id"),
            "workflow": report.get("workflow"),
            "overall_status": report.get("overall_status", "unknown"),
            "description": report.get("description"),
            "report": json_dumps(report),
            "started_at": parse_dt(report.get("started_at")),
            "finished_at": parse_dt(report.get("finished_at")),
            "updated_at": _utcnow(),
        },
    )


def db_load_latest_workflow_runs(session: Session) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (workflow)
                run_id, workflow, overall_status, description, report,
                started_at, finished_at, created_at, updated_at
            FROM governance.workflow_run_reports
            ORDER BY workflow, finished_at DESC NULLS LAST, started_at DESC NULLS LAST
            """
        ),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _workflow_run_from_row(row)
        latest[str(row.workflow)] = payload
    return latest


def db_list_workflow_runs(
    session: Session,
    *,
    started_after: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {}
    if started_after is not None:
        clauses.append("COALESCE(started_at, finished_at, created_at) >= :started_after")
        params["started_after"] = started_after
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = "LIMIT :limit" if limit is not None else ""
    if limit is not None:
        params["limit"] = limit
    rows = session.execute(
        text(
            f"""
            SELECT run_id, workflow, overall_status, description, report,
                   started_at, finished_at, created_at, updated_at
            FROM governance.workflow_run_reports
            {where_sql}
            ORDER BY COALESCE(started_at, finished_at, created_at) DESC
            {limit_sql}
            """
        ),
        params,
    ).fetchall()
    return [_workflow_run_from_row(row) for row in rows]


# sentinel workflow 名，用于在 workflow_scheduler_state 表里存放根级
# scheduler meta（bootstrap_stage / bootstrap_completed_at）。
# 选带双下划线前后缀的名字避免与真实 workflow 冲突。
_SCHEDULER_META_WORKFLOW = "__scheduler_meta__"


def db_load_scheduler_state(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT workflow, initialized_at, last_processed_slot, last_action,
                   last_checked_at, last_task_id, last_reason, schedule, state_payload
            FROM governance.workflow_scheduler_state
            ORDER BY workflow
            """
        ),
    ).fetchall()
    workflows: dict[str, Any] = {}
    initialized_at: str | None = None
    bootstrap_stage: str | None = None
    bootstrap_completed_at: str | None = None
    for row in rows:
        workflow_name = str(row.workflow)
        if workflow_name == _SCHEDULER_META_WORKFLOW:
            # meta 行：bootstrap 字段提到根级，不参与 workflows 归类，也不参与
            # initialized_at 聚合。state_payload 里可能是 dict，容错成 {}。
            payload = row.state_payload if isinstance(row.state_payload, dict) else {}
            stage_value = payload.get("bootstrap_stage")
            completed_value = payload.get("bootstrap_completed_at")
            if isinstance(stage_value, str) and stage_value:
                bootstrap_stage = stage_value
            if isinstance(completed_value, str) and completed_value:
                bootstrap_completed_at = completed_value
            continue
        workflows[workflow_name] = _with_payload(
            row.state_payload,
            last_processed_slot=(
                row.last_processed_slot.isoformat() if row.last_processed_slot else None
            ),
            last_action=row.last_action,
            last_checked_at=row.last_checked_at.isoformat() if row.last_checked_at else None,
            last_task_id=row.last_task_id,
            last_reason=row.last_reason,
            schedule=row.schedule,
        )
        if row.initialized_at:
            iso = row.initialized_at.isoformat()
            if initialized_at is None or iso < initialized_at:
                initialized_at = iso
    return {
        "generated_at": _utcnow().isoformat(),
        "initialized_at": initialized_at,
        "bootstrap_stage": bootstrap_stage,
        "bootstrap_completed_at": bootstrap_completed_at,
        "workflows": workflows,
    }


def db_save_scheduler_state(session: Session, state: dict[str, Any]) -> None:
    initialized_at = parse_dt(state.get("initialized_at"))
    for workflow, workflow_state in (state.get("workflows") or {}).items():
        workflow_state = workflow_state or {}
        session.execute(
            text(
                """
                INSERT INTO governance.workflow_scheduler_state
                    (workflow, initialized_at, last_processed_slot, last_action,
                     last_checked_at, last_task_id, last_reason, schedule,
                     state_payload, updated_at)
                VALUES
                    (:workflow, :initialized_at, :last_processed_slot, :last_action,
                     :last_checked_at, :last_task_id, :last_reason, :schedule,
                     CAST(:state_payload AS jsonb), :updated_at)
                ON CONFLICT (workflow) DO UPDATE SET
                    initialized_at = EXCLUDED.initialized_at,
                    last_processed_slot = EXCLUDED.last_processed_slot,
                    last_action = EXCLUDED.last_action,
                    last_checked_at = EXCLUDED.last_checked_at,
                    last_task_id = EXCLUDED.last_task_id,
                    last_reason = EXCLUDED.last_reason,
                    schedule = EXCLUDED.schedule,
                    state_payload = EXCLUDED.state_payload,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "workflow": workflow,
                "initialized_at": initialized_at,
                "last_processed_slot": parse_dt(workflow_state.get("last_processed_slot")),
                "last_action": workflow_state.get("last_action"),
                "last_checked_at": parse_dt(workflow_state.get("last_checked_at")),
                "last_task_id": workflow_state.get("last_task_id"),
                "last_reason": workflow_state.get("last_reason"),
                "schedule": workflow_state.get("schedule"),
                "state_payload": json_dumps(workflow_state),
                "updated_at": _utcnow(),
            },
        )

    # 持久化根级 scheduler meta（bootstrap 状态）。即使两个字段都是 None 也要
    # upsert，否则"已完成 bootstrap 后状态被清理"的语义无法表达。
    meta_payload = {
        "bootstrap_stage": state.get("bootstrap_stage"),
        "bootstrap_completed_at": state.get("bootstrap_completed_at"),
    }
    session.execute(
        text(
            """
            INSERT INTO governance.workflow_scheduler_state
                (workflow, initialized_at, last_processed_slot, last_action,
                 last_checked_at, last_task_id, last_reason, schedule,
                 state_payload, updated_at)
            VALUES
                (:workflow, NULL, NULL, NULL,
                 NULL, NULL, NULL, NULL,
                 CAST(:state_payload AS jsonb), :updated_at)
            ON CONFLICT (workflow) DO UPDATE SET
                state_payload = EXCLUDED.state_payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "workflow": _SCHEDULER_META_WORKFLOW,
            "state_payload": json_dumps(meta_payload),
            "updated_at": _utcnow(),
        },
    )


def db_upsert_pre_apply_gate_result(session: Session, result: dict[str, Any]) -> None:
    # release_id 在 gate 跑完时通常为 None（gate 是 apply 的前置），由 apply
    # 流程成功创建 release 后通过 db_set_gate_result_release_id 回填。upsert
    # 保留回填语义：如果 payload 里带了 release_id，覆盖原值；没带则不破坏
    # 已回填的值（见 ON CONFLICT 分支用 COALESCE 保留已有值）。
    session.execute(
        text(
            """
            INSERT INTO governance.pre_apply_gate_results
                (gate_run_id, recommendation_id, release_id, allow_apply, gate_status,
                 total_checks, passed_checks, payload, created_at, updated_at)
            VALUES
                (:gate_run_id, :recommendation_id, :release_id, :allow_apply, :gate_status,
                 :total_checks, :passed_checks, CAST(:payload AS jsonb), :created_at, :updated_at)
            ON CONFLICT (gate_run_id) DO UPDATE SET
                recommendation_id = EXCLUDED.recommendation_id,
                release_id = COALESCE(EXCLUDED.release_id, governance.pre_apply_gate_results.release_id),
                allow_apply = EXCLUDED.allow_apply,
                gate_status = EXCLUDED.gate_status,
                total_checks = EXCLUDED.total_checks,
                passed_checks = EXCLUDED.passed_checks,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "gate_run_id": result.get("gate_run_id"),
            "recommendation_id": result.get("recommendation_id"),
            "release_id": result.get("release_id"),
            "allow_apply": bool(result.get("allow_apply")),
            "gate_status": result.get("gate_status", "unknown"),
            "total_checks": int(result.get("total_checks") or 0),
            "passed_checks": int(result.get("passed_checks") or 0),
            "payload": json_dumps(result),
            "created_at": parse_dt(result.get("created_at")) or _utcnow(),
            "updated_at": _utcnow(),
        },
    )


def db_set_gate_result_release_id(
    session: Session,
    *,
    gate_run_id: str,
    release_id: str,
) -> bool:
    """在 release 创建成功后，把 release_id 回填到对应 gate_run 行。

    由 apply_active_parameter_set 在 release upsert 成功后调用。返回值含义：
      * True  = gate_run 行存在（无论是否真的发生写入）
      * False = gate_run 行不存在，需要 caller 打 warning

    行存在但 ``release_id`` 已经等于目标值时跳过 UPDATE（M4：避免 release 回填
    每次 save_release_history 都触发 UPDATE + updated_at bump）。两条 SQL：先
    SELECT 查现值，仅在需要变更时才 UPDATE；重放 / 手工改回旧值的场景仍然
    会命中 UPDATE 分支。两条都在同一 Session/transaction 里，原子性由外层
    session.begin() 保证。
    """
    current = session.execute(
        text(
            """
            SELECT release_id
              FROM governance.pre_apply_gate_results
             WHERE gate_run_id = :gate_run_id
            """
        ),
        {"gate_run_id": gate_run_id},
    ).fetchone()
    if current is None:
        return False
    if current.release_id == release_id:
        # 已经是目标值，避免无谓的 UPDATE / updated_at bump / WAL 追加
        return True
    session.execute(
        text(
            """
            UPDATE governance.pre_apply_gate_results
               SET release_id = :release_id,
                   updated_at = :updated_at
             WHERE gate_run_id = :gate_run_id
            """
        ),
        {
            "gate_run_id": gate_run_id,
            "release_id": release_id,
            "updated_at": _utcnow(),
        },
    )
    return True


def db_list_pre_apply_gate_results(session: Session, *, limit: int = 8) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT gate_run_id, recommendation_id, release_id, allow_apply,
                   gate_status, total_checks, passed_checks, payload,
                   created_at, updated_at
            FROM governance.pre_apply_gate_results
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).fetchall()
    return [_pre_apply_gate_result_from_row(row) for row in rows]


# ── P0-2 新增：按业务维度查询 pre-apply gate 结果 ──────────────────
# 命名对齐业务动作；与现有 db_upsert_pre_apply_gate_result 共用同一张表。
# 历史上 gate 结果的唯一读路径是 db_list_pre_apply_gate_results(limit=N)，
# 想看"某个 recommendation 的最近一次 gate"或"某个 release 的 gate 链路"
# 只能在应用层 filter，导致 rdp_control_summary / operator query 各自拼逻辑。
# 本阶段把查询下沉到 DB，后续读路径统一走这组 API。


def db_record_gate_result(session: Session, result: dict[str, Any]) -> None:
    """语义化封装：记录一次 gate 运行结果。

    语义与 db_upsert_pre_apply_gate_result 完全相同；保留它作为"业务动作"API
    的入口，避免调用方直接触达 upsert 的实现细节（比如字段展开规则）。
    gate 运行的 gate_run_id 在业务上是幂等键，重复 record 会覆盖 payload。
    """
    db_upsert_pre_apply_gate_result(session, result)


def db_get_gate_result_by_run_id(
    session: Session,
    gate_run_id: str,
) -> dict[str, Any] | None:
    """按 gate_run_id 精确查询单次 gate 结果。"""
    row = session.execute(
        text(
            """
            SELECT gate_run_id, recommendation_id, release_id, allow_apply,
                   gate_status, total_checks, passed_checks, payload,
                   created_at, updated_at
            FROM governance.pre_apply_gate_results
            WHERE gate_run_id = :gate_run_id
            """
        ),
        {"gate_run_id": gate_run_id},
    ).fetchone()
    if row is None:
        return None
    return _pre_apply_gate_result_from_row(row)


def db_get_latest_gate_result(
    session: Session,
    *,
    recommendation_id: str,
) -> dict[str, Any] | None:
    """按 recommendation 取最近一次 gate 结果。

    apply 链路只在乎"最新一次 gate 是否 allow"，历史记录由 list API 负责。
    """
    row = session.execute(
        text(
            """
            SELECT gate_run_id, recommendation_id, release_id, allow_apply,
                   gate_status, total_checks, passed_checks, payload,
                   created_at, updated_at
            FROM governance.pre_apply_gate_results
            WHERE recommendation_id = :rec_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"rec_id": recommendation_id},
    ).fetchone()
    if row is None:
        return None
    return _pre_apply_gate_result_from_row(row)


def db_list_gate_results_for_recommendation(
    session: Session,
    *,
    recommendation_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按 recommendation 维度列出历次 gate 结果（用于审计回溯）。"""
    rows = session.execute(
        text(
            """
            SELECT gate_run_id, recommendation_id, release_id, allow_apply,
                   gate_status, total_checks, passed_checks, payload,
                   created_at, updated_at
            FROM governance.pre_apply_gate_results
            WHERE recommendation_id = :rec_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"rec_id": recommendation_id, "limit": limit},
    ).fetchall()
    return [_pre_apply_gate_result_from_row(row) for row in rows]


def db_list_gate_results_for_release(
    session: Session,
    *,
    release_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按 release 维度列出历次 gate 结果。

    阶段 B：直接走 pre_apply_gate_results.release_id 索引列。回填由
    save_release_history 在 release upsert 同事务里通过 db_set_gate_result_release_id
    完成；没有回填的 legacy gate 行在这里查不到，属于预期行为（调用方应
    回落到 db_get_latest_gate_result(recommendation_id=...) 等维度）。
    """
    rows = session.execute(
        text(
            """
            SELECT gate_run_id, recommendation_id, release_id, allow_apply,
                   gate_status, total_checks, passed_checks, payload,
                   created_at, updated_at
            FROM governance.pre_apply_gate_results
            WHERE release_id = :release_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"release_id": release_id, "limit": limit},
    ).fetchall()
    return [_pre_apply_gate_result_from_row(row) for row in rows]


def db_upsert_parameter_release(
    session: Session,
    release: dict[str, Any],
    *,
    allow_apply_success_transition: bool = False,
    allow_rollback_transition: bool = False,
) -> dict[str, Any]:
    # M5：column 与 payload JSON 必须归一到同一份 timeframe / combo_key。
    # 统一走 ``_normalize_combo_fields`` 与 observation_results /
    # rollback_recommendations / release_effectiveness 三张表共享同一份归一化路径。
    release_id = str(release.get("release_id") or "").strip()
    if not release_id:
        raise ValueError("parameter release requires release_id")
    timeframe_norm, combo_key_norm, release_for_payload = _normalize_combo_fields(
        release
    )
    release_for_payload["release_id"] = release_id
    # These fields attest to checks performed by this DB writer and cannot be
    # supplied by a registry/file caller.
    release_for_payload.pop("rollback_capital_proof_version", None)
    release_for_payload.pop("rollback_capital_proof_verified", None)
    release_for_payload.setdefault("apply_result", "pending")
    release_for_payload.setdefault("observation_status", "pending")

    row = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, recommendation_id,
                   parameter_set_id, previous_parameter_set_id, actor,
                   gate_result_ref, gate_status, apply_result,
                   observation_status, observation_window_hours, notes,
                   payload, created_at, updated_at
            FROM governance.parameter_releases
            WHERE release_id = :release_id
            FOR UPDATE
            """
        ),
        {"release_id": release_id},
    ).fetchone()

    if row is None:
        # A new row is only an authorization anchor.  Every later lifecycle
        # state must be a row-locked transition with capital/evidence proof;
        # accepting terminal/audit fields here would permit two-step forgery.
        if (
            str(release_for_payload.get("apply_result") or "pending")
            != "pending"
            or str(
                release_for_payload.get("observation_status") or "pending"
            )
            != "pending"
        ):
            raise ValueError(
                "new parameter release must be a pending authorization anchor"
            )
        forbidden_anchor_fields = (
            *_RELEASE_APPLY_AUDIT_FIELDS,
            *_RELEASE_ROLLBACK_AUDIT_FIELDS,
            *_RELEASE_APPLY_RECONCILIATION_FIELDS,
        )
        if any(
            release_for_payload.get(field) is not None
            for field in forbidden_anchor_fields
        ):
            raise ValueError(
                "new pending release cannot contain lifecycle audit fields"
            )
        merged = release_for_payload
        statement = """
            INSERT INTO governance.parameter_releases
                (release_id, family, timeframe, combo_key, recommendation_id,
                 parameter_set_id, previous_parameter_set_id, actor, gate_result_ref,
                 gate_status, apply_result, observation_status, observation_window_hours,
                 notes, payload, created_at, updated_at)
            VALUES
                (:release_id, :family, :timeframe, :combo_key, :recommendation_id,
                 :parameter_set_id, :previous_parameter_set_id, :actor, :gate_result_ref,
                 :gate_status, :apply_result, :observation_status, :observation_window_hours,
                 :notes, CAST(:payload AS jsonb), :created_at, :updated_at)
        """
    else:
        stored_release = _parameter_release_from_row(row)
        apply_success_transition = (
            str(stored_release.get("apply_result") or "pending") == "pending"
            and str(release_for_payload.get("apply_result") or "pending")
            == "success"
        )
        rollback_transition = (
            stored_release.get("observation_status") != "rolled_back"
            and release_for_payload.get("observation_status") == "rolled_back"
        )
        if apply_success_transition and not allow_apply_success_transition:
            raise ValueError(
                "apply success transition is restricted to the capital transaction"
            )
        if rollback_transition and not allow_rollback_transition:
            raise ValueError(
                "rolled_back transition is restricted to the capital transaction"
            )
        merged = _merge_parameter_release_state(
            stored_release,
            release_for_payload,
        )
        if apply_success_transition:
            proof = session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) = 1
                         FROM governance.active_parameter_sets AS a
                         WHERE lower(btrim(a.family)) = lower(btrim(:family))
                           AND lower(btrim(a.timeframe)) = lower(btrim(:timeframe))
                           AND a.parameter_set_id = :parameter_set_id
                           AND a.approval_recommendation_id = :recommendation_id
                        ) AS active_matches,
                        (SELECT COUNT(*) = 1
                         FROM governance.parameter_apply_history AS h
                         WHERE h.operation_id = :operation_id
                           AND h.operation_type = 'apply'
                           AND lower(btrim(h.family)) = lower(btrim(:family))
                           AND lower(btrim(h.timeframe)) = lower(btrim(:timeframe))
                           AND h.to_parameter_set_id = :parameter_set_id
                           AND h.recommendation_id = :recommendation_id
                        ) AS history_matches,
                        (SELECT COUNT(*) = 1
                         FROM governance.parameter_sets AS p
                         WHERE p.parameter_set_id = :parameter_set_id
                           AND p.status = 'released'
                        ) AS lifecycle_matches
                    """
                ),
                {
                    "family": merged.get("family"),
                    "timeframe": merged.get("timeframe"),
                    "parameter_set_id": merged.get("parameter_set_id"),
                    "recommendation_id": merged.get("recommendation_id"),
                    "operation_id": merged.get("apply_operation_id"),
                },
            ).fetchone()
            if not (
                proof is not None
                and getattr(proof, "active_matches", None) is True
                and getattr(proof, "history_matches", None) is True
                and getattr(proof, "lifecycle_matches", None) is True
            ):
                raise ValueError(
                    "apply success transition lacks exact canonical capital lineage"
                )
        if rollback_transition:
            rollback_proof = session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) = 1
                         FROM governance.active_parameter_sets AS a
                         WHERE lower(btrim(a.family)) = lower(btrim(:family))
                           AND lower(btrim(a.timeframe)) = lower(btrim(:timeframe))
                           AND a.parameter_set_id = :rollback_target
                        ) AS active_matches,
                        (SELECT COUNT(*) = 1
                         FROM governance.parameter_apply_history AS h
                         WHERE h.operation_id = :rollback_operation_id
                           AND h.operation_type = 'rollback'
                           AND lower(btrim(h.family)) = lower(btrim(:family))
                           AND lower(btrim(h.timeframe)) = lower(btrim(:timeframe))
                           AND h.from_parameter_set_id = :parameter_set_id
                           AND h.to_parameter_set_id = :rollback_target
                        ) AS history_matches
                    """
                ),
                {
                    "family": merged.get("family"),
                    "timeframe": merged.get("timeframe"),
                    "parameter_set_id": merged.get("parameter_set_id"),
                    "rollback_target": merged.get(
                        "rollback_to_parameter_set_id"
                    ),
                    "rollback_operation_id": merged.get(
                        "rollback_operation_id"
                    ),
                },
            ).fetchone()
            if not (
                rollback_proof is not None
                and getattr(rollback_proof, "active_matches", None) is True
                and getattr(rollback_proof, "history_matches", None) is True
            ):
                raise ValueError(
                    "rolled_back transition lacks exact canonical capital lineage"
                )
            merged["rollback_capital_proof_version"] = (
                _RELEASE_ROLLBACK_CAPITAL_PROOF_VERSION
            )
            merged["rollback_capital_proof_verified"] = True
        statement = """
            UPDATE governance.parameter_releases
            SET family = :family,
                timeframe = :timeframe,
                combo_key = :combo_key,
                recommendation_id = :recommendation_id,
                parameter_set_id = :parameter_set_id,
                previous_parameter_set_id = :previous_parameter_set_id,
                actor = :actor,
                gate_result_ref = :gate_result_ref,
                gate_status = :gate_status,
                apply_result = :apply_result,
                observation_status = :observation_status,
                observation_window_hours = :observation_window_hours,
                notes = :notes,
                payload = CAST(:payload AS jsonb),
                updated_at = :updated_at
            WHERE release_id = :release_id
        """

    merged_timeframe, merged_combo_key, merged_payload = _normalize_combo_fields(
        merged
    )
    params = {
        "release_id": release_id,
        "family": str(merged.get("family") or ""),
        "timeframe": merged_timeframe,
        "combo_key": merged_combo_key,
        "recommendation_id": merged.get("recommendation_id"),
        "parameter_set_id": merged.get("parameter_set_id"),
        "previous_parameter_set_id": merged.get("previous_parameter_set_id"),
        "actor": merged.get("actor", "operator"),
        "gate_result_ref": merged.get("gate_result_ref"),
        "gate_status": merged.get("gate_status"),
        "apply_result": merged.get("apply_result", "pending"),
        "observation_status": merged.get("observation_status", "pending"),
        "observation_window_hours": int(
            merged.get("observation_window_hours") or 24
        ),
        "notes": merged.get("notes"),
        "payload": json_dumps(merged_payload),
        "created_at": parse_dt(merged.get("created_at")) or _utcnow(),
        "updated_at": _utcnow(),
    }
    session.execute(text(statement), params)
    return merged_payload


def db_load_release_history(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, recommendation_id,
                   parameter_set_id, previous_parameter_set_id, actor,
                   gate_result_ref, gate_status, apply_result,
                   observation_status, observation_window_hours, notes,
                   payload, created_at, updated_at
            FROM governance.parameter_releases
            ORDER BY created_at ASC
            """
        ),
    ).fetchall()
    return {
        "generated_at": _utcnow().isoformat(),
        "releases": [
            _parameter_release_from_row(row)
            for row in rows
        ],
    }


def db_find_parameter_release(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, recommendation_id,
                   parameter_set_id, previous_parameter_set_id, actor,
                   gate_result_ref, gate_status, apply_result,
                   observation_status, observation_window_hours, notes,
                   payload, created_at, updated_at
            FROM governance.parameter_releases
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _parameter_release_from_row(row)


def db_get_parameter_release_for_update(
    session: Session,
    *,
    release_id: str,
    family: str,
    timeframe: str,
    recommendation_id: str,
    parameter_set_id: str,
) -> dict[str, Any] | None:
    """Lock and return one canonical release inside a caller-owned transaction."""
    row = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, recommendation_id,
                   parameter_set_id, previous_parameter_set_id, actor,
                   gate_result_ref, gate_status, apply_result,
                   observation_status, observation_window_hours, notes,
                   payload, created_at, updated_at
            FROM governance.parameter_releases
            WHERE release_id = :release_id
              AND lower(btrim(family)) = lower(btrim(:family))
              AND lower(btrim(timeframe)) = lower(btrim(:timeframe))
              AND recommendation_id = :recommendation_id
              AND parameter_set_id = :parameter_set_id
            FOR UPDATE
            """
        ),
        {
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe,
            "recommendation_id": recommendation_id,
            "parameter_set_id": parameter_set_id,
        },
    ).fetchone()
    if row is None:
        return None
    return _parameter_release_from_row(row)


def db_update_parameter_release_status(
    session: Session,
    release_id: str,
    *,
    apply_result: str | None = None,
    observation_status: str | None = None,
) -> bool:
    """Compatibility presence check; lifecycle mutation is intentionally denied.

    Apply/observation/rollback states carry capital meaning and must use the
    row-locked, proof-bearing writers above.  This legacy partial UPDATE had no
    identity, monotonicity, or lineage proof and is therefore read-only now.
    """
    if apply_result is None and observation_status is None:
        # caller 没给任何变更，不必打 DB
        row = session.execute(
            text(
                """
                SELECT 1 AS present
                FROM governance.parameter_releases
                WHERE release_id = :release_id
                """
            ),
            {"release_id": release_id},
        ).fetchone()
        return row is not None

    raise ValueError(
        "partial parameter release status mutation is disabled; use a "
        "proof-bearing lifecycle transition"
    )


def db_get_latest_release_for_combo(
    session: Session,
    *,
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, recommendation_id,
                   parameter_set_id, previous_parameter_set_id, actor,
                   gate_result_ref, gate_status, apply_result,
                   observation_status, observation_window_hours, notes,
                   payload, created_at, updated_at
            FROM governance.parameter_releases
            WHERE family = :family AND timeframe = :timeframe
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()
    if row is None:
        return None
    return _parameter_release_from_row(row)


def db_upsert_observation_result(session: Session, result: dict[str, Any]) -> None:
    # M5 归一：历史上只 lower() 了 column，payload 仍 json_dumps(result) 原封保留
    # 大小写 timeframe / combo_key，读者从 payload 反序列化时与列值不一致。
    timeframe_norm, combo_key_norm, result_for_payload = _normalize_combo_fields(result)
    # A-0.3 (详见 rdp_hardening_batch_a_detailed_design.md §4.8 allowlist 注释):
    # observation_results.status 允许的取值为 {observing, completed, rollback_recommended}。
    # 历史的 .get("status", "unknown") 兜底会在 A-1 CHECK 约束上线后直接撞 FK / CHECK
    # 违反，而且早在今天就会让 payload 与 column 进入互相矛盾的状态；这里直接抛
    # ValueError，迫使上游修好 result 字典再来。
    status_val = result.get("status")
    if status_val not in {"observing", "completed", "rollback_recommended"}:
        raise ValueError(
            "db_upsert_observation_result: status must be one of "
            f"{{'observing','completed','rollback_recommended'}}; got {status_val!r}"
        )
    release_id = str(result.get("release_id") or "").strip()
    family = str(result.get("family") or "").strip()
    if not release_id or not family or not timeframe_norm:
        raise ValueError(
            "db_upsert_observation_result: release/family/timeframe are required"
        )
    if type(result.get("window_active")) is not bool:
        raise ValueError(
            "db_upsert_observation_result: window_active must be exact boolean"
        )
    window_hours = result.get("observation_window_hours")
    if type(window_hours) is not int or window_hours <= 0:
        raise ValueError(
            "db_upsert_observation_result: observation_window_hours must be "
            "a positive integer"
        )
    recommendation = result.get("recommendation")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise ValueError(
            "db_upsert_observation_result: recommendation is required"
        )
    if (status_val == "rollback_recommended") != (
        recommendation == "rollback_recommended"
    ):
        raise ValueError(
            "db_upsert_observation_result: rollback status/recommendation mismatch"
        )
    started_at = _strict_evidence_timestamp(
        result.get("started_at"),
        context="observation_result.started_at",
    )
    evaluated_at = _strict_evidence_timestamp(
        result.get("evaluated_at"),
        context="observation_result.evaluated_at",
    )
    if evaluated_at < started_at:
        raise ValueError(
            "db_upsert_observation_result: evaluated_at precedes started_at"
        )
    result_for_payload.update(
        {
            "release_id": release_id,
            "family": family,
            "started_at": started_at.isoformat(),
            "evaluated_at": evaluated_at.isoformat(),
            "window_active": result["window_active"],
            "observation_window_hours": window_hours,
            "status": status_val,
            "recommendation": recommendation,
        }
    )
    result_for_payload["evidence_fingerprint"] = _evidence_fingerprint(
        result_for_payload
    )
    session.execute(
        text(
            """
            INSERT INTO governance.observation_results
                (release_id, family, timeframe, combo_key, status, recommendation,
                 observation_window_hours, window_active, started_at, evaluated_at,
                 payload, updated_at)
            VALUES
                (:release_id, :family, :timeframe, :combo_key, :status, :recommendation,
                 :observation_window_hours, :window_active, :started_at, :evaluated_at,
                 CAST(:payload AS jsonb), :updated_at)
            ON CONFLICT (release_id) DO UPDATE SET
                family = EXCLUDED.family,
                timeframe = EXCLUDED.timeframe,
                combo_key = EXCLUDED.combo_key,
                status = EXCLUDED.status,
                recommendation = EXCLUDED.recommendation,
                observation_window_hours = EXCLUDED.observation_window_hours,
                window_active = EXCLUDED.window_active,
                started_at = EXCLUDED.started_at,
                evaluated_at = EXCLUDED.evaluated_at,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            WHERE
                observation_results.status <> 'rollback_recommended'
                AND (
                    EXCLUDED.status = 'rollback_recommended'
                    OR EXCLUDED.evaluated_at > observation_results.evaluated_at
                    OR (
                        EXCLUDED.evaluated_at = observation_results.evaluated_at
                        AND COALESCE(EXCLUDED.payload ->> 'evidence_fingerprint', '')
                            > COALESCE(
                                observation_results.payload ->> 'evidence_fingerprint',
                                ''
                            )
                    )
                )
            """
        ),
        {
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe_norm,
            "combo_key": combo_key_norm or result.get("combo_key"),
            "status": status_val,
            "recommendation": recommendation,
            "observation_window_hours": window_hours,
            "window_active": result["window_active"],
            "started_at": started_at,
            "evaluated_at": evaluated_at,
            "payload": json_dumps(result_for_payload),
            "updated_at": _utcnow(),
        },
    )


def db_get_observation_result(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, status,
                   recommendation, observation_window_hours, window_active,
                   started_at, evaluated_at, payload, updated_at
            FROM governance.observation_results
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _observation_result_from_row(row)


def db_list_observation_results(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key, status,
                   recommendation, observation_window_hours, window_active,
                   started_at, evaluated_at, payload, updated_at
            FROM governance.observation_results
            ORDER BY evaluated_at DESC
            """
        ),
    ).fetchall()
    return [
        _observation_result_from_row(row)
        for row in rows
    ]


def db_upsert_rollback_recommendation(session: Session, result: dict[str, Any]) -> None:
    # M5 归一：同 observation_results 的修复动机，避免 column 与 payload 的
    # timeframe / combo_key 大小写不一致。
    timeframe_norm, combo_key_norm, result_for_payload = _normalize_combo_fields(result)
    release_id = str(result.get("release_id") or "").strip()
    family = str(result.get("family") or "").strip()
    if not release_id or not family or not timeframe_norm:
        raise ValueError(
            "db_upsert_rollback_recommendation: release/family/timeframe are required"
        )
    rollback_recommended = result.get("rollback_recommended")
    if type(rollback_recommended) is not bool:
        raise ValueError(
            "db_upsert_rollback_recommendation: rollback_recommended must be "
            "exact boolean"
        )
    severity = result.get("severity")
    if severity not in {"none", "medium", "high"}:
        raise ValueError(
            "db_upsert_rollback_recommendation: invalid severity"
        )
    if rollback_recommended is False and severity != "none":
        raise ValueError(
            "db_upsert_rollback_recommendation: non-risk evidence must use "
            "severity=none"
        )
    evaluated_at = _strict_evidence_timestamp(
        result.get("evaluated_at"),
        context="rollback_recommendation.evaluated_at",
    )
    result_for_payload.update(
        {
            "release_id": release_id,
            "family": family,
            "rollback_recommended": rollback_recommended,
            "severity": severity,
            "evaluated_at": evaluated_at.isoformat(),
        }
    )
    result_for_payload["evidence_fingerprint"] = _evidence_fingerprint(
        result_for_payload
    )
    session.execute(
        text(
            """
            INSERT INTO governance.rollback_recommendations
                (release_id, family, timeframe, combo_key, rollback_recommended,
                 severity, suggested_target_parameter_set_id, evaluated_at,
                 payload, updated_at)
            VALUES
                (:release_id, :family, :timeframe, :combo_key, :rollback_recommended,
                 :severity, :suggested_target_parameter_set_id, :evaluated_at,
                 CAST(:payload AS jsonb), :updated_at)
            ON CONFLICT (release_id) DO UPDATE SET
                family = EXCLUDED.family,
                timeframe = EXCLUDED.timeframe,
                combo_key = EXCLUDED.combo_key,
                rollback_recommended = EXCLUDED.rollback_recommended,
                severity = EXCLUDED.severity,
                suggested_target_parameter_set_id = EXCLUDED.suggested_target_parameter_set_id,
                evaluated_at = EXCLUDED.evaluated_at,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            WHERE
                (
                    rollback_recommendations.rollback_recommended IS FALSE
                    AND EXCLUDED.rollback_recommended IS TRUE
                )
                OR (
                    rollback_recommendations.rollback_recommended IS FALSE
                    AND EXCLUDED.rollback_recommended IS FALSE
                    AND (
                        EXCLUDED.evaluated_at
                            > rollback_recommendations.evaluated_at
                        OR (
                            EXCLUDED.evaluated_at
                                = rollback_recommendations.evaluated_at
                            AND COALESCE(
                                EXCLUDED.payload ->> 'evidence_fingerprint',
                                ''
                            ) > COALESCE(
                                rollback_recommendations.payload
                                    ->> 'evidence_fingerprint',
                                ''
                            )
                        )
                    )
                )
            """
        ),
        {
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe_norm,
            "combo_key": combo_key_norm or result.get("combo_key"),
            "rollback_recommended": rollback_recommended,
            "severity": severity,
            "suggested_target_parameter_set_id": result.get("suggested_target_parameter_set_id"),
            "evaluated_at": evaluated_at,
            "payload": json_dumps(result_for_payload),
            "updated_at": _utcnow(),
        },
    )


def db_get_rollback_recommendation(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT release_id, family, timeframe, combo_key,
                   rollback_recommended, severity,
                   suggested_target_parameter_set_id, evaluated_at,
                   payload, updated_at
            FROM governance.rollback_recommendations
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _rollback_recommendation_from_row(row)


def _load_effectiveness_capital_truth_row(
    session: Session,
    *,
    release_id: str,
    family: str,
    timeframe: str,
) -> Any:
    """Load one release's canonical capital truth for terminal verification."""

    return session.execute(
        text(
            """
            SELECT
                r.apply_result AS release_apply_result,
                r.observation_status AS release_observation_status,
                r.parameter_set_id AS release_parameter_set_id,
                r.payload ->> 'rollback_to_parameter_set_id'
                    AS release_rollback_target,
                r.payload ->> 'rollback_operation_id'
                    AS release_rollback_operation_id,
                r.combo_key AS release_combo_key,
                a.parameter_set_id AS active_parameter_set_id,
                h.operation_type AS history_operation_type,
                h.family AS history_family,
                h.timeframe AS history_timeframe,
                h.from_parameter_set_id AS history_from_parameter_set_id,
                h.to_parameter_set_id AS history_to_parameter_set_id,
                h.actor AS history_actor,
                h.created_at AS history_created_at,
                d.current_status AS decision_status,
                d.last_updated_at AS decision_updated_at,
                d.notes AS decision_notes
            FROM governance.parameter_releases AS r
            LEFT JOIN governance.active_parameter_sets AS a
              ON lower(btrim(a.family)) = lower(btrim(r.family))
             AND lower(btrim(a.timeframe)) = lower(btrim(r.timeframe))
            LEFT JOIN governance.parameter_apply_history AS h
              ON h.operation_id = r.payload ->> 'rollback_operation_id'
            LEFT JOIN governance.active_decisions AS d
              ON lower(btrim(d.family)) = lower(btrim(r.family))
             AND lower(btrim(d.timeframe)) = lower(btrim(r.timeframe))
            WHERE r.release_id = :release_id
              AND lower(btrim(r.family)) = lower(btrim(:family))
              AND lower(btrim(r.timeframe)) = lower(btrim(:timeframe))
            """
        ),
        {
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe,
        },
    ).fetchone()


def _canonical_completed_rollback_fact(
    row: Any,
    *,
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """Return exact committed rollback lineage, independent of its actor."""

    if row is None:
        return None
    canonical_combo = f"{family}_{timeframe}".lower()
    release_parameter_set_id = str(
        getattr(row, "release_parameter_set_id", "") or ""
    ).strip()
    target = str(getattr(row, "release_rollback_target", "") or "").strip()
    operation_id = str(
        getattr(row, "release_rollback_operation_id", "") or ""
    ).strip()
    active_parameter_set_id = str(
        getattr(row, "active_parameter_set_id", "") or ""
    ).strip()
    actor = str(getattr(row, "history_actor", "") or "").strip()
    if not (
        getattr(row, "release_apply_result", None) == "success"
        and getattr(row, "release_observation_status", None) == "rolled_back"
        and str(getattr(row, "release_combo_key", "") or "").strip().lower()
        == canonical_combo
        and release_parameter_set_id
        and target
        and operation_id
        and active_parameter_set_id == target
        and getattr(row, "history_operation_type", None) == "rollback"
        and str(getattr(row, "history_family", "") or "").strip().lower()
        == family.lower()
        and str(getattr(row, "history_timeframe", "") or "").strip().lower()
        == timeframe
        and getattr(row, "history_from_parameter_set_id", None)
        == release_parameter_set_id
        and getattr(row, "history_to_parameter_set_id", None) == target
        and actor
    ):
        return None
    fact_observed_at = parse_dt(getattr(row, "history_created_at", None))
    if fact_observed_at is None:
        return None
    return {
        "operation_id": operation_id,
        "target_parameter_set_id": target,
        "actor": actor,
        "fact_observed_at": fact_observed_at.astimezone(timezone.utc),
    }


def db_get_completed_operator_rollback_fact(
    session: Session,
    *,
    release_id: str,
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """Return a canonical rollback completed outside the auto enforcer.

    The result is intentionally limited to exact release/history/active
    lineage.  It lets the effectiveness state machine attest an Operator
    action as an enforced rollback instead of mislabelling it as an unrelated
    active-set change.  Auto-enforcer history is excluded because an
    interrupted automatic attempt must remain fail-closed for reconciliation.
    """

    normalized_family = str(family or "").strip()
    normalized_timeframe = str(timeframe or "").strip().lower()
    normalized_release_id = str(release_id or "").strip()
    if not normalized_family or not normalized_timeframe or not normalized_release_id:
        raise ValueError("operator rollback fact requires release/family/timeframe")
    row = _load_effectiveness_capital_truth_row(
        session,
        release_id=normalized_release_id,
        family=normalized_family,
        timeframe=normalized_timeframe,
    )
    fact = _canonical_completed_rollback_fact(
        row,
        family=normalized_family,
        timeframe=normalized_timeframe,
    )
    if fact is None or fact["actor"] == "release_effectiveness_auto_rollback":
        return None
    return fact


def _verify_effectiveness_terminal_capital_truth(
    session: Session,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    """Re-derive a terminal effectiveness action from canonical DB state.

    The caller already holds the per-combo mutation lock.  This query and the
    effectiveness write therefore observe one serial capital state.  JSON
    proof fields are only selectors; they never prove themselves.
    """

    status = _effectiveness_action_status(evaluation)
    validate_effectiveness_terminal_proof_shape(
        evaluation,
        status=status,
        require_db_verified=False,
    )
    family = str(evaluation.get("family") or "").strip()
    timeframe = str(evaluation.get("timeframe") or "").strip().lower()
    release_id = str(evaluation.get("release_id") or "").strip()
    if not family or not timeframe or not release_id:
        raise ValueError("terminal rollback proof is missing canonical identity")
    row = _load_effectiveness_capital_truth_row(
        session,
        release_id=release_id,
        family=family,
        timeframe=timeframe,
    )
    if row is None:
        raise ValueError("terminal rollback proof has no canonical release")
    canonical_combo = f"{family}_{timeframe}".lower()
    if (
        getattr(row, "release_apply_result", None) != "success"
        or str(getattr(row, "release_combo_key", "") or "").strip().lower()
        != canonical_combo
    ):
        raise ValueError("terminal rollback proof release lineage is invalid")

    release_parameter_set_id = str(
        getattr(row, "release_parameter_set_id", "") or ""
    ).strip()
    active_parameter_set_id = str(
        getattr(row, "active_parameter_set_id", "") or ""
    ).strip()
    started_at = _effectiveness_action_timestamp(
        evaluation, "rollback_enforcement_started_at"
    )
    claimed_finished_at = _effectiveness_action_timestamp(
        evaluation, "rollback_enforcement_finished_at"
    )
    if status == "enforced":
        target = str(evaluation.get("rollback_to_parameter_set_id") or "").strip()
        operation_id = str(
            evaluation.get("rollback_capital_operation_id") or ""
        ).strip()
        rollback_fact = _canonical_completed_rollback_fact(
            row,
            family=family,
            timeframe=timeframe,
        )
        if not (
            rollback_fact is not None
            and rollback_fact["target_parameter_set_id"] == target
            and rollback_fact["operation_id"] == operation_id
        ):
            raise ValueError(
                "enforced rollback lacks exact canonical capital lineage"
            )
        fact_observed_at = rollback_fact["fact_observed_at"]
        if not (started_at <= fact_observed_at <= claimed_finished_at):
            raise ValueError("rollback history time is outside the action attempt")
        return {"fact_observed_at": fact_observed_at}

    proof_kind = evaluation.get("rollback_capital_proof_kind")
    if proof_kind == "active_parameter_changed":
        observed = str(
            evaluation.get("rollback_capital_proof_active_parameter_set_id") or ""
        ).strip()
        if not (
            getattr(row, "release_observation_status", None) != "rolled_back"
            and active_parameter_set_id
            and active_parameter_set_id == observed
            and active_parameter_set_id != release_parameter_set_id
        ):
            raise ValueError(
                "active-change cancellation lacks canonical capital-state proof"
            )
        fact_observed_at = _utcnow()
        if fact_observed_at < started_at:
            raise ValueError("active-change proof precedes the action attempt")
        return {"fact_observed_at": fact_observed_at}

    decision_updated_at = parse_dt(getattr(row, "decision_updated_at", None))
    expected_notes_prefix = (
        "soft_pause_auto_rollback_no_valid_target: "
        f"release={release_id} "
    )
    if not (
        proof_kind == "soft_pause"
        and getattr(row, "decision_status", None) == "pause"
        and decision_updated_at is not None
        and decision_updated_at.astimezone(timezone.utc) >= started_at
        and decision_updated_at.astimezone(timezone.utc) <= claimed_finished_at
        and str(getattr(row, "decision_notes", "") or "").startswith(
            expected_notes_prefix
        )
    ):
        raise ValueError("soft-pause cancellation lacks canonical decision proof")
    return {"fact_observed_at": decision_updated_at.astimezone(timezone.utc)}


def _insert_effectiveness_action_proof(
    session: Session,
    evaluation: dict[str, Any],
    *,
    fact_observed_at: datetime,
) -> None:
    """Insert the application-owned proof anchor for one terminal attempt."""

    row = session.execute(
        text(
            """
            INSERT INTO governance.release_effectiveness_action_proofs
                (release_id, attempt_id, outcome, proof_kind,
                 started_at_utc, finished_at_utc,
                 operation_id, target_parameter_set_id,
                 observed_active_parameter_set_id, decision_status,
                 fact_observed_at, created_at)
            VALUES
                (:release_id, :attempt_id, :outcome, :proof_kind,
                 :started_at_utc, :finished_at_utc,
                 :operation_id, :target_parameter_set_id,
                 :observed_active_parameter_set_id, :decision_status,
                 :fact_observed_at, :created_at)
            RETURNING release_id
            """
        ),
        {
            "release_id": evaluation.get("release_id"),
            "attempt_id": evaluation.get("rollback_enforcement_attempt_id"),
            "outcome": evaluation.get("rollback_enforcement_status"),
            "proof_kind": evaluation.get("rollback_capital_proof_kind"),
            "started_at_utc": evaluation.get("rollback_enforcement_started_at"),
            "finished_at_utc": evaluation.get("rollback_enforcement_finished_at"),
            "operation_id": evaluation.get("rollback_capital_operation_id"),
            "target_parameter_set_id": evaluation.get(
                "rollback_to_parameter_set_id"
            ),
            "observed_active_parameter_set_id": evaluation.get(
                "rollback_capital_proof_active_parameter_set_id"
            ),
            "decision_status": evaluation.get(
                "rollback_capital_proof_decision_status"
            ),
            "fact_observed_at": fact_observed_at,
            "created_at": _utcnow(),
        },
    ).fetchone()
    if row is None or getattr(row, "release_id", None) != evaluation.get(
        "release_id"
    ):
        raise ValueError("terminal rollback immutable proof was not persisted")


def db_upsert_release_effectiveness(
    session: Session,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    # M5 归一：该表列里没有 combo_key，但 payload 归一仍有价值——下游读 JSON
    # 时会拿到一致的 timeframe。_normalize_combo_fields 会同时计算 combo_key，
    # 这里只使用 timeframe 部分。
    release_id = str(evaluation.get("release_id") or "").strip()
    if not release_id:
        raise ValueError("release effectiveness requires release_id")
    timeframe_norm, _combo_key_norm, eval_for_payload = _normalize_combo_fields(
        evaluation
    )
    eval_for_payload["release_id"] = release_id
    # This attestation is DB-writer-owned.  Callers (including an offline file
    # mirror) cannot assert that canonical capital truth was verified.
    eval_for_payload.pop("rollback_capital_proof_verified", None)
    eval_for_payload["rollback_enforcement_status"] = (
        _effectiveness_action_status(eval_for_payload)
    )

    row = session.execute(
        text(
            """
            SELECT evaluation_id, release_id, family, timeframe, conclusion,
                   evaluated_at, payload, updated_at
            FROM governance.release_effectiveness
            WHERE release_id = :release_id
            FOR UPDATE
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    current_status = "pending"
    incoming_status = _effectiveness_action_status(eval_for_payload)
    if row is None:
        # Run new rows through the same validator/normalizer as updates.  In
        # particular, a rollback obligation without combo identity must enter
        # reconciliation immediately instead of becoming an unscoped pending
        # action that per-combo veto queries cannot see.
        merged = _merge_release_effectiveness_state({}, eval_for_payload)
        statement = """
            INSERT INTO governance.release_effectiveness
                (evaluation_id, release_id, family, timeframe, conclusion,
                 evaluated_at, payload, updated_at)
            VALUES
                (:evaluation_id, :release_id, :family, :timeframe, :conclusion,
                 :evaluated_at, CAST(:payload AS jsonb), :updated_at)
        """
    else:
        stored = _release_effectiveness_from_row(row)
        current_status = _effectiveness_action_status(stored)
        merged = _merge_release_effectiveness_state(
            stored,
            eval_for_payload,
        )
        statement = """
            UPDATE governance.release_effectiveness
            SET evaluation_id = :evaluation_id,
                family = :family,
                timeframe = :timeframe,
                conclusion = :conclusion,
                evaluated_at = :evaluated_at,
                payload = CAST(:payload AS jsonb),
                updated_at = :updated_at
            WHERE release_id = :release_id
        """

    if (
        current_status == "in_progress"
        and incoming_status in {"enforced", "cancelled"}
    ):
        proof = _verify_effectiveness_terminal_capital_truth(session, merged)
        fact_observed_at = proof["fact_observed_at"].astimezone(timezone.utc)
        fact_time = fact_observed_at.isoformat()
        merged["rollback_enforcement_finished_at"] = fact_time
        if incoming_status == "enforced":
            merged["rollback_enforced_at"] = fact_time
        else:
            merged["rollback_cancelled_at"] = fact_time
        validate_effectiveness_terminal_proof_shape(
            merged,
            status=incoming_status,
            require_db_verified=False,
        )
        _insert_effectiveness_action_proof(
            session,
            merged,
            fact_observed_at=fact_observed_at,
        )
        merged["rollback_capital_proof_verified"] = True

    merged_timeframe, _merged_combo_key, merged_payload = _normalize_combo_fields(
        merged
    )
    params = {
        "evaluation_id": merged.get("evaluation_id"),
        "release_id": release_id,
        "family": merged.get("family"),
        "timeframe": merged_timeframe or None,
        "conclusion": merged.get("conclusion", "unknown"),
        "evaluated_at": parse_dt(merged.get("evaluated_at")) or _utcnow(),
        "payload": json_dumps(merged_payload),
        "updated_at": _utcnow(),
    }
    session.execute(text(statement), params)
    return merged_payload


def db_load_effectiveness_registry(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT evaluation_id, release_id, family, timeframe, conclusion,
                   evaluated_at, payload, updated_at
            FROM governance.release_effectiveness
            ORDER BY evaluated_at ASC
            """
        ),
    ).fetchall()
    return {
        "generated_at": _utcnow().isoformat(),
        "evaluations": [
            _release_effectiveness_from_row(row)
            for row in rows
        ],
    }


def db_find_release_effectiveness(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT evaluation_id, release_id, family, timeframe, conclusion,
                   evaluated_at, payload, updated_at
            FROM governance.release_effectiveness
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _release_effectiveness_from_row(row)


def _decision_evidence_bundle_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical insert-once identity for one Phase 6 bundle."""

    round_id = entry.get("round_id")
    evidence_summary_path = entry.get("evidence_summary_path")
    if not isinstance(round_id, str) or not round_id.strip():
        raise ValueError("decision_evidence_bundle_round_id_invalid")
    if (
        not isinstance(evidence_summary_path, str)
        or not evidence_summary_path.strip()
    ):
        raise ValueError("decision_evidence_bundle_summary_path_invalid")
    created_at = parse_iso_datetime_utc(
        entry.get("created_at"),
        context="decision_evidence_bundle.created_at",
    )
    if created_at is None:
        raise ValueError("decision_evidence_bundle_created_at_required")

    identity = dict(entry)
    identity.update(
        {
            "round_id": round_id,
            "evidence_summary_path": evidence_summary_path,
            "phases_with_data": list(entry.get("phases_with_data") or []),
            "completeness_ratio": float(
                entry.get("completeness_ratio") or 0.0
            ),
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
        }
    )
    return json.loads(canonical_typed_json_bytes(identity).decode("utf-8"))


def db_get_decision_evidence_bundle(
    session: Session,
    *,
    round_id: str,
) -> dict[str, Any] | None:
    """Load one evidence bundle with DB columns overlaid as canonical truth."""

    row = session.execute(
        text(
            """
            SELECT round_id, evidence_summary_path, phases_with_data,
                   completeness_ratio, payload, created_at
            FROM governance.decision_evidence_bundles
            WHERE round_id = :round_id
            LIMIT 1
            """
        ),
        {"round_id": round_id},
    ).fetchone()
    if row is None:
        return None
    created_at = row.created_at
    if created_at is not None:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
    return _with_authoritative_columns(
        row.payload,
        round_id=row.round_id,
        evidence_summary_path=row.evidence_summary_path,
        phases_with_data=list(row.phases_with_data or []),
        completeness_ratio=float(row.completeness_ratio or 0.0),
        created_at=created_at.isoformat() if created_at is not None else None,
    )


def db_insert_decision_evidence_bundle(
    session: Session,
    entry: dict[str, Any],
) -> None:
    """Insert one immutable bundle, accepting only an exact identity retry."""

    expected = _decision_evidence_bundle_identity(entry)
    existing = db_get_decision_evidence_bundle(
        session,
        round_id=expected["round_id"],
    )
    if existing is not None:
        if typed_json_sha256(existing) != typed_json_sha256(expected):
            raise DBConflictError(
                "decision_evidence_bundle_immutable_identity_conflict"
            )
        return

    result = session.execute(
        text(
            """
            INSERT INTO governance.decision_evidence_bundles
                (round_id, evidence_summary_path, phases_with_data,
                 completeness_ratio, payload, created_at, updated_at)
            VALUES
                (:round_id, :evidence_summary_path,
                 CAST(:phases_with_data AS jsonb), :completeness_ratio,
                 CAST(:payload AS jsonb), :created_at, :updated_at)
            ON CONFLICT (round_id) DO NOTHING
            RETURNING round_id
            """
        ),
        {
            "round_id": expected["round_id"],
            "evidence_summary_path": expected["evidence_summary_path"],
            "phases_with_data": json_dumps(expected["phases_with_data"]),
            "completeness_ratio": expected["completeness_ratio"],
            "payload": canonical_typed_json_bytes(expected).decode("utf-8"),
            "created_at": parse_iso_datetime_utc(
                expected["created_at"],
                context="decision_evidence_bundle.created_at",
            ),
            "updated_at": _utcnow(),
        },
    )
    if result.fetchone() is not None:
        return

    # A non-Phase6 writer may have raced the INSERT.  It is safe only when it
    # independently published the byte-equivalent immutable identity.
    existing = db_get_decision_evidence_bundle(
        session,
        round_id=expected["round_id"],
    )
    if (
        existing is None
        or typed_json_sha256(existing) != typed_json_sha256(expected)
    ):
        raise DBConflictError(
            "decision_evidence_bundle_immutable_identity_conflict"
        )


def db_upsert_decision_evidence_bundle(session: Session, entry: dict[str, Any]) -> None:
    """Compatibility name for the now-immutable evidence-bundle insert."""

    db_insert_decision_evidence_bundle(session, entry)


def db_load_decision_evidence_bundle_index(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT payload, created_at
            FROM governance.decision_evidence_bundles
            ORDER BY created_at ASC
            """
        ),
    ).fetchall()
    return {
        "generated_at": _utcnow().isoformat(),
        "bundles": [
            _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)
            for row in rows
        ],
    }

