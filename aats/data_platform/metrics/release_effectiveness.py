"""Release Effectiveness Evaluation 模块.

工作包 C: 给每次 parameter release 一个 effectiveness 评价。

评价维度:
  1. 行为层 — attribution 是否改善
  2. 执行层 — execution realism 是否恶化
  3. 运营层 — 是否触发 rollback, observation 是否完成
  4. 治理层 — evidence freshness, unresolved alerts

结论分类:
  effective / mixed / ineffective / rollback_triggered / insufficient_evidence
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
    try_governance_db,
)
from aats.data_platform.governance._exceptions import DBUnavailableError


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_json(fp: Path) -> dict | None:
    if not fp.exists():
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _effectiveness_registry_path(root: Path) -> Path:
    return root / "artifacts" / "metrics" / "release_effectiveness_registry.json"


def load_effectiveness_registry(root: Path) -> dict:
    managed_truth = has_explicit_governance_db_configuration(root)
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_load_effectiveness_registry,
            )

            with Session(engine) as session:
                registry = db_load_effectiveness_registry(session)
            # DB 是真源：空表也直接返回，避免把旧 effectiveness 结论重新注入评估链
            return registry
        except Exception as exc:
            if managed_truth:
                raise DBUnavailableError(
                    "managed release-effectiveness read failed; stale file fallback denied"
                ) from exc
        finally:
            if engine is not None:
                engine.dispose()
    elif managed_truth:
        raise DBUnavailableError(
            "managed release-effectiveness unavailable; stale file fallback denied"
        )

    fp = _effectiveness_registry_path(root)
    if not fp.exists():
        return {"evaluations": [], "generated_at": None}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def _persist_effectiveness_evaluations(
    root: Path,
    evaluations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Persist only caller-owned rows and return their canonical merged state."""
    # Canonical verification is an attestation owned exclusively by the DB
    # writer after it re-derives capital truth.  Never carry a caller-supplied
    # flag into either the managed path or the offline audit mirror.
    evaluations = [dict(item) for item in evaluations if isinstance(item, dict)]
    for evaluation in evaluations:
        evaluation.pop("rollback_capital_proof_verified", None)
    managed_truth = has_explicit_governance_db_configuration(root)
    engine, ok = try_governance_db()
    canonical_registry: dict[str, Any] | None = None
    persisted: dict[str, dict[str, Any]] = {}
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_load_effectiveness_registry,
                db_upsert_release_effectiveness,
            )
            from aats.data_platform.governance.active_params_db import (
                db_try_acquire_parameter_apply_lock,
            )

            ordered = sorted(
                (item for item in evaluations if isinstance(item, dict)),
                key=lambda item: (
                    str(item.get("family") or ""),
                    str(item.get("timeframe") or "").lower(),
                    str(item.get("release_id") or ""),
                ),
            )
            with Session(engine) as session, session.begin():
                for evaluation in ordered:
                    family = str(evaluation.get("family") or "").strip()
                    timeframe = str(evaluation.get("timeframe") or "").strip()
                    release_id = str(evaluation.get("release_id") or "").strip()
                    if not family or not timeframe or not release_id:
                        raise ValueError(
                            "effectiveness evaluation missing release/family/timeframe"
                        )
                    # Every action-state writer shares the capital mutation
                    # lock.  Locking only pending rows would still let a stale
                    # terminal snapshot race an apply or another action owner.
                    if not db_try_acquire_parameter_apply_lock(
                        session,
                        family=family,
                        timeframe=timeframe,
                    ):
                        raise RuntimeError(
                            "parameter combo mutation is in progress; "
                            "effectiveness persistence rejected"
                        )
                    persisted[release_id] = db_upsert_release_effectiveness(
                        session,
                        evaluation,
                    )
                canonical_registry = db_load_effectiveness_registry(session)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "release effectiveness DB 同步失败，保存未完成",
            )
            error_type = DBUnavailableError if managed_truth else RuntimeError
            raise error_type(
                "release effectiveness persistence failed"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()
    elif managed_truth:
        raise DBUnavailableError(
            "managed release-effectiveness persistence unavailable"
        )

    if canonical_registry is None:
        # Explicit offline development mode: merge the owned rows into the file
        # snapshot.  Managed deployments never reach this fallback.
        current = _load_json(_effectiveness_registry_path(root)) or {
            "evaluations": []
        }
        by_release = {
            str(item.get("release_id")): item
            for item in current.get("evaluations", [])
            if isinstance(item, dict) and item.get("release_id")
        }
        for evaluation in evaluations:
            if isinstance(evaluation, dict) and evaluation.get("release_id"):
                by_release[str(evaluation["release_id"])] = evaluation
                persisted[str(evaluation["release_id"])] = evaluation
        canonical_registry = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "evaluations": list(by_release.values()),
        }

    canonical_registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _atomic_write_json(_effectiveness_registry_path(root), canonical_registry)
    except Exception as exc:
        if not managed_truth:
            raise
        import logging as _logging

        _logging.getLogger(__name__).error(
            "release_effectiveness_mirror_degraded: canonical DB evaluation "
            "committed but local JSON mirror failed: %s",
            type(exc).__name__,
        )
    return persisted


def save_effectiveness_registry(root: Path, data: dict) -> None:
    """Compatibility batch import with row-locked monotonic DB merges."""
    _persist_effectiveness_evaluations(
        root,
        [item for item in data.get("evaluations", []) if isinstance(item, dict)],
    )


def save_effectiveness_evaluation(root: Path, evaluation: dict[str, Any]) -> dict:
    """Persist one owned evaluation/action transition and return DB truth."""
    release_id = str(evaluation.get("release_id") or "").strip()
    if not release_id:
        raise ValueError("effectiveness evaluation requires release_id")
    persisted = _persist_effectiveness_evaluations(root, [evaluation])
    return persisted[release_id]


# ── 维度评估函数 ──────────────────────────────────────────────

def _evaluate_behavior(
    release: dict,
    observation: dict[str, Any] | None,
) -> dict:
    """行为层: 检查 observation 中的 attribution 和 decision status."""
    obs = observation
    if not obs:
        return {
            "dimension": "behavior",
            "score": "unknown",
            "detail": "no observation data",
        }

    raw_checklist = obs.get("checklist")
    if not isinstance(raw_checklist, list):
        return {
            "dimension": "behavior",
            "score": "unknown",
            "detail": "malformed observation checklist",
        }
    checklist = {
        item["name"]: item
        for item in raw_checklist
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }

    attr_check = checklist.get("attribution", {})
    decision_check = checklist.get("decision_status", {})

    issues = []
    if attr_check.get("status") == "regression":
        issues.append("attribution regression detected")
    if decision_check.get("status") == "regression":
        issues.append("decision status regression")

    if issues:
        return {"dimension": "behavior", "score": "negative", "detail": "; ".join(issues)}
    valid_statuses = {"ok", "warn", "regression"}
    if (
        attr_check.get("status") not in valid_statuses
        or decision_check.get("status") not in valid_statuses
    ):
        return {"dimension": "behavior", "score": "unknown", "detail": "insufficient data"}
    return {"dimension": "behavior", "score": "positive", "detail": "no regression detected"}


def _evaluate_execution(
    release: dict,
    observation: dict[str, Any] | None,
) -> dict:
    """执行层: 检查 execution realism 是否恶化."""
    obs = observation
    if not obs:
        return {
            "dimension": "execution",
            "score": "unknown",
            "detail": "no observation data",
        }

    raw_checklist = obs.get("checklist")
    if not isinstance(raw_checklist, list):
        return {
            "dimension": "execution",
            "score": "unknown",
            "detail": "malformed observation checklist",
        }
    checklist = {
        item["name"]: item
        for item in raw_checklist
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    exec_check = checklist.get("execution_realism", {})

    if exec_check.get("status") == "regression":
        return {
            "dimension": "execution",
            "score": "negative",
            "detail": exec_check.get("detail", "execution regression"),
        }
    if exec_check.get("status") not in {"ok", "warn", "regression"}:
        return {"dimension": "execution", "score": "unknown", "detail": "no execution data"}
    return {"dimension": "execution", "score": "positive", "detail": "execution stable or improved"}


def _evaluate_operations(
    release: dict,
    observation: dict[str, Any] | None,
    rollback_recommendation: dict[str, Any] | None,
) -> dict:
    """运营层: 检查 rollback 和 observation 完成."""
    rb = rollback_recommendation
    rollback_recommended = (
        rb.get("rollback_recommended") if isinstance(rb, dict) else None
    )

    if rollback_recommended is True:
        return {
            "dimension": "operations",
            "score": "negative",
            "detail": f"rollback recommended (severity={rb.get('severity', '?')})",
            "rollback_related": True,
        }

    release_observation_status = release.get("observation_status", "unknown")
    if release_observation_status == "rolled_back":
        return {
            "dimension": "operations",
            "score": "negative",
            "detail": "rollback executed after apply",
            "rollback_related": True,
        }
    obs_status = observation.get("status") if observation else None
    if obs_status == "rollback_recommended":
        return {
            "dimension": "operations",
            "score": "negative",
            "detail": "rollback recommended by observation status",
            "rollback_related": True,
        }
    if obs_status == "completed":
        return {"dimension": "operations", "score": "positive", "detail": "observation completed, no rollback"}
    if obs_status == "observing":
        return {"dimension": "operations", "score": "unknown", "detail": "still observing"}
    if rollback_recommended not in {None, False}:
        return {
            "dimension": "operations",
            "score": "unknown",
            "detail": "malformed rollback recommendation",
        }
    return {
        "dimension": "operations",
        "score": "unknown",
        "detail": f"observation_status={obs_status or 'unavailable'}",
    }


def _evaluate_governance(root: Path, release: dict) -> dict:
    """治理层: evidence freshness + unresolved alerts."""
    # gate status
    gate_status = release.get("gate_status", "unknown")

    issues = []
    if gate_status == "block":
        issues.append("gate was blocked")

    if issues:
        return {"dimension": "governance", "score": "negative", "detail": "; ".join(issues)}
    if gate_status == "warn":
        return {"dimension": "governance", "score": "mixed", "detail": "gate passed with warnings"}
    # The mutable current_alerts.json mirror is not canonical runtime truth.
    # Until a release-bound DB alert snapshot is available, a passing gate is
    # insufficient to award a positive governance score.
    return {
        "dimension": "governance",
        "score": "unknown",
        "detail": "canonical runtime alert evidence unavailable",
    }


# ── 综合评估 ──────────────────────────────────────────────────

def evaluate_release_effectiveness(
    root: Path,
    release_id: str,
    *,
    save_result: bool = True,
) -> dict:
    """评估一次 release 的 effectiveness.

    Returns:
        evaluation dict with dimensions, conclusion, detail
    """
    now = datetime.now(timezone.utc)

    # 找 release
    from aats.data_platform.production_workflow.observation_window import (
        load_observation_result,
    )
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
        validate_post_apply_release_identity,
        validate_release_bound_evidence,
    )
    from aats.data_platform.production_workflow.rollback_policy import (
        load_rollback_recommendation,
    )

    rel_data = load_release_history(root)
    release, identity_error = validate_post_apply_release_identity(
        rel_data or {},
        release_id=release_id,
        requested_family=None,
        requested_timeframe=None,
    )
    if identity_error is not None:
        return identity_error
    assert release is not None

    observation = load_observation_result(root, release_id)
    rollback_recommendation = load_rollback_recommendation(root, release_id)
    evidence_errors: list[dict[str, Any]] = []
    valid_observation = observation
    valid_rollback_recommendation = rollback_recommendation
    for evidence_kind, evidence in (
        ("observation", observation),
        ("rollback_recommendation", rollback_recommendation),
    ):
        evidence_error = validate_release_bound_evidence(
            release,
            evidence,
            evidence_kind=evidence_kind,
        )
        if evidence_error is not None:
            evidence_errors.append(evidence_error)
            if evidence_kind == "observation":
                valid_observation = None
            else:
                valid_rollback_recommendation = None

    # 评估各维度
    dimensions = [
        _evaluate_behavior(release, valid_observation),
        _evaluate_execution(release, valid_observation),
        _evaluate_operations(
            release,
            valid_observation,
            valid_rollback_recommendation,
        ),
        _evaluate_governance(root, release),
    ]

    # 综合结论
    conclusion = _derive_effectiveness(dimensions, release)
    valid_risk_evidence = bool(
        (
            isinstance(valid_rollback_recommendation, dict)
            and valid_rollback_recommendation.get("rollback_recommended") is True
        )
        or (
            isinstance(valid_observation, dict)
            and valid_observation.get("status") == "rollback_recommended"
            and valid_observation.get("recommendation")
            == "rollback_recommended"
        )
        or release.get("observation_status") == "rolled_back"
    )
    # A malformed sibling artifact may never mask a separate, valid high-risk
    # signal.  Conversely, without valid risk evidence we must not infer a
    # positive/mixed conclusion from a partial evidence set.
    if evidence_errors and not valid_risk_evidence:
        conclusion = "insufficient_evidence"

    # 加载 baseline comparison (如果有)
    comparison = _load_json(
        root / "artifacts" / "metrics" / "release_comparisons"
        / release_id / "baseline_comparison.json"
    )
    comparison_conclusion = comparison.get("conclusion") if comparison else None

    # RDP Bug 7: 原生成器 f"eff_{strftime('%Y%m%d_%H%M%S')}" 在同一秒内对多个
    # release 会产生相同 id。observation_cycle 从 weekly decision_cycle 拆到
    # hourly 后（Bug 1），单次运行可能在同一秒内处理多条积压 release，命中
    # uq_release_effectiveness_evaluation_id 唯一约束触发 IntegrityError，
    # 导致整批评估滚回。加 6 位 uuid 后缀保证唯一。
    evaluation = {
        "evaluation_id": f"eff_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}",
        "release_id": release_id,
        "family": release.get("family"),
        "timeframe": release.get("timeframe"),
        "combo_key": (
            f"{release.get('family')}_{str(release.get('timeframe') or '').lower()}"
        ),
        "evaluated_at": now.isoformat(),
        "dimensions": dimensions,
        "baseline_comparison_conclusion": comparison_conclusion,
        "conclusion": conclusion,
        "detail": _effectiveness_detail(dimensions, conclusion),
    }
    if evidence_errors:
        evaluation["evidence_reconciliation_required"] = True
        evaluation["evidence_errors"] = evidence_errors

    if save_result:
        # 保存到 registry
        registry = load_effectiveness_registry(root)
        # 去重: 替换同 release_id 的旧评估
        # Bug 8 修复: 保留前次 evaluation 的 rollback_cancelled / rollback_enforced
        # 等 action state 字段。原逻辑每次 evaluate 都构造全新 dict,覆盖掉
        # enforce_pending_rollbacks 写入的 rollback_cancelled 标记，导致:
        #   - cleanup 脚本 (rdp_migration_bug8_cancel_stale_rollbacks) 的 cancel
        #     标记在下一轮 observation_cycle 被抹掉, rollback 再次入 pending
        #   - Layer 2 的 soft-pause (写 rollback_cancelled='soft_paused_...')
        #     同样会被抹掉, 实际没有兜底效果
        # carry-over 字段: rollback_cancelled, rollback_cancelled_at,
        # rollback_cancelled_reason, rollback_enforced, rollback_enforced_at,
        # rollback_to_parameter_set_id, rollback_attempts, last_rollback_error,
        # rollback_soft_pause_applied
        _ACTION_STATE_FIELDS = (
            "rollback_cancelled",
            "rollback_cancelled_at",
            "rollback_cancelled_reason",
            "rollback_enforced",
            "rollback_enforced_at",
            "rollback_to_parameter_set_id",
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
        previous = next(
            (e for e in registry["evaluations"] if e.get("release_id") == release_id),
            None,
        )
        if previous is not None:
            for field in _ACTION_STATE_FIELDS:
                if field in previous and field not in evaluation:
                    evaluation[field] = previous[field]
        evaluation = dict(save_effectiveness_evaluation(root, evaluation))

    return evaluation


def _derive_effectiveness(dimensions: list[dict], release: dict) -> str:
    """从各维度分数推导 effectiveness."""
    # 如果有 rollback
    ops_dim = next((d for d in dimensions if d["dimension"] == "operations"), None)
    if (
        ops_dim
        and ops_dim["score"] == "negative"
        and (
            bool(ops_dim.get("rollback_related"))
            or "rollback" in ops_dim.get("detail", "")
            or "rolled back" in ops_dim.get("detail", "")
        )
    ):
        return "rollback_triggered"

    scores = [d["score"] for d in dimensions]

    # 全部 unknown = insufficient_evidence
    if all(s == "unknown" for s in scores):
        return "insufficient_evidence"

    negatives = sum(1 for s in scores if s == "negative")
    positives = sum(1 for s in scores if s == "positive")
    unknowns = sum(1 for s in scores if s == "unknown")

    if negatives >= 2:
        return "ineffective"
    if negatives == 0 and positives >= 2 and unknowns == 0:
        return "effective"
    if unknowns >= 3:
        return "insufficient_evidence"
    return "mixed"


def _effectiveness_detail(dimensions: list[dict], conclusion: str) -> str:
    """生成 effectiveness 描述。"""
    parts = [f"conclusion={conclusion}"]
    for d in dimensions:
        parts.append(f"{d['dimension']}={d['score']}")
    return "; ".join(parts)


def find_effectiveness(root: Path, release_id: str) -> dict | None:
    """查找指定 release 的 effectiveness 评估."""
    registry = load_effectiveness_registry(root)
    for e in registry.get("evaluations", []):
        if e.get("release_id") == release_id:
            return e
    return None


# ── P2 自动回滚执行 ─────────────────────────────────────────────


_ROLLBACK_BOOLEAN_FLAGS = (
    "rollback_enforced",
    "rollback_cancelled",
    "rollback_soft_pause_applied",
)
_ROLLBACK_CAPITAL_PROOF_VERSION = "rdp-rollback-capital-proof/v1"
_ROLLBACK_PRIOR_ACTION_ANCHORS = (
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


def _validate_rollback_boolean_flags(evaluation: dict[str, Any]) -> None:
    """Reject type-polluted action flags before classifying rollback state.

    JSON strings such as ``"true"`` are neither truthy compatibility values
    nor equivalent to a JSON boolean.  Treating them as false can replay a
    capital action; treating the opposite flag as false can incorrectly close
    a still-unresolved action.  Missing keys remain valid for legacy rows.
    """
    invalid = [
        key
        for key in _ROLLBACK_BOOLEAN_FLAGS
        if key in evaluation and type(evaluation[key]) is not bool
    ]
    if invalid:
        raise ValueError(
            "rollback boolean flag must be an exact bool: "
            + ",".join(sorted(invalid))
        )

    raw_status = evaluation.get("rollback_enforcement_status")
    if raw_status is not None and type(raw_status) is not str:
        raise ValueError("rollback_enforcement_status must be a string or null")


def _rollback_has_prior_action_anchor(evaluation: dict[str, Any]) -> bool:
    attempts_present = "rollback_attempts" in evaluation
    attempts = evaluation.get("rollback_attempts")
    if attempts_present and (type(attempts) is not int or attempts != 0):
        return True
    if evaluation.get("rollback_soft_pause_applied", False) is True:
        return True
    return any(
        evaluation.get(key) is not None and evaluation.get(key) != ""
        for key in _ROLLBACK_PRIOR_ACTION_ANCHORS
    )


def _rollback_resolution(evaluation: dict[str, Any]) -> str | None:
    """Return a terminal action only when the DB-attested contract is complete."""
    _validate_rollback_boolean_flags(evaluation)
    raw_status = evaluation.get("rollback_enforcement_status")
    if raw_status not in {"enforced", "cancelled"}:
        # status=NULL single booleans are legacy claims without attempt or
        # capital lineage.  They remain unresolved and therefore block apply.
        return None
    from aats.data_platform.governance.operational_state_db import (
        validate_effectiveness_terminal_proof_shape,
    )

    try:
        validate_effectiveness_terminal_proof_shape(
            evaluation,
            status=raw_status,
            require_db_verified=True,
        )
    except ValueError:
        return None
    return raw_status


def _rollback_is_clean_pending(evaluation: dict[str, Any]) -> bool:
    _validate_rollback_boolean_flags(evaluation)
    return (
        evaluation.get("rollback_enforced", False) is False
        and evaluation.get("rollback_cancelled", False) is False
        and evaluation.get("rollback_soft_pause_applied", False) is False
        and evaluation.get("rollback_enforcement_status") in {None, "pending"}
        and not _rollback_has_prior_action_anchor(evaluation)
    )


def pending_rollback_combos(root: Path) -> dict[str, str]:
    """返回所有 rollback_triggered 但未执行回滚的 combo → release_id 映射.

    供 apply/rollback 入口做安全检查。
    """
    registry = load_effectiveness_registry(root)
    result: dict[str, str] = {}
    for ev in registry.get("evaluations", []):
        if ev.get("conclusion") != "rollback_triggered":
            continue
        try:
            resolved = _rollback_resolution(ev)
        except ValueError:
            # A malformed row is still a blocking rollback obligation.  Keep
            # it visible to apply/rollback guards instead of dropping it or
            # making this read path unavailable.
            resolved = None
        if resolved is None:
            combo = f"{ev.get('family')}_{ev.get('timeframe', '').lower()}"
            result[combo] = ev.get("release_id", "?")
    return result


def _load_completed_operator_rollback_fact(
    root: Path,
    *,
    release_id: str,
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """Read an already committed Operator rollback from canonical DB truth."""

    managed_truth = has_explicit_governance_db_configuration(root)
    engine, ok = try_governance_db()
    if ok and engine is not None:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_get_completed_operator_rollback_fact,
            )

            with Session(engine) as session:
                return db_get_completed_operator_rollback_fact(
                    session,
                    release_id=release_id,
                    family=family,
                    timeframe=timeframe,
                )
        except Exception as exc:
            if managed_truth:
                raise DBUnavailableError(
                    "managed operator-rollback proof read failed"
                ) from exc
            return None
        finally:
            engine.dispose()
    if managed_truth:
        raise DBUnavailableError(
            "managed operator-rollback proof unavailable"
        )
    return None


def _set_enforced_rollback_fields(
    evaluation: dict[str, Any],
    *,
    target_parameter_set_id: str,
    operation_id: str | None,
    finished_at: str,
) -> None:
    """Apply the single proof shape shared by auto and Operator rollbacks."""

    evaluation["rollback_enforced"] = True
    evaluation["rollback_enforced_at"] = finished_at
    evaluation["rollback_to_parameter_set_id"] = target_parameter_set_id
    evaluation["rollback_enforcement_status"] = "enforced"
    evaluation["rollback_enforcement_finished_at"] = finished_at
    evaluation["rollback_soft_pause_applied"] = False
    evaluation["rollback_capital_proof_version"] = (
        _ROLLBACK_CAPITAL_PROOF_VERSION
    )
    evaluation["rollback_capital_proof_kind"] = "rollback"
    evaluation["rollback_capital_operation_id"] = operation_id
    evaluation.pop("rollback_cancelled", None)
    evaluation.pop("rollback_cancelled_at", None)
    evaluation.pop("rollback_cancelled_reason", None)
    evaluation.pop("rollback_capital_proof_active_parameter_set_id", None)
    evaluation.pop("rollback_capital_proof_decision_status", None)


def enforce_pending_rollbacks(
    root: Path,
    *,
    release_ids: Collection[str] | None = None,
) -> list[dict]:
    """检查并执行指定范围内 pending 的 rollback_triggered 结论.

    针对每个 rollback_triggered 且未标记 rollback_enforced 的评估：
      1. 从 release history 查找对应 release 的 previous_parameter_set_id
      2. 调用 rollback_active_parameter_set() 回滚到上一版本
      3. 标记 evaluation 为 rollback_enforced

    ``release_ids=None`` 保留 observation cycle 的全量风险收敛语义；显式
    集合只处理集合内 release，供单 release Operator CLI 使用。字符串不能
    作为集合传入，避免被逐字符解释而静默扩大或缩小动作范围。

    Returns
    -------
    list[dict]  每个回滚操作的结果
    """
    target_release_ids: frozenset[str] | None = None
    if release_ids is not None:
        if isinstance(release_ids, (str, bytes)):
            raise TypeError("release_ids must be a collection of release IDs")
        normalized_release_ids: set[str] = set()
        for release_id in release_ids:
            if (
                type(release_id) is not str
                or not release_id
                or release_id != release_id.strip()
            ):
                raise ValueError("release_ids contains an invalid release ID")
            normalized_release_ids.add(release_id)
        target_release_ids = frozenset(normalized_release_ids)

    registry = load_effectiveness_registry(root)
    results: list[dict] = []
    modified = False

    # Fix P1: 将重复文件 I/O 移到循环外，避免每次评估都重新加载
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
        validate_post_apply_release_identity,
        validate_release_bound_evidence,
    )
    from aats.data_platform.production_workflow.observation_window import (
        load_observation_result,
    )
    from aats.data_platform.production_workflow.rollback_policy import (
        load_rollback_recommendation,
    )

    rel_data = load_release_history(root)

    for ev in registry.get("evaluations", []):
        if ev.get("conclusion") != "rollback_triggered":
            continue
        if (
            target_release_ids is not None
            and ev.get("release_id") not in target_release_ids
        ):
            continue
        try:
            resolution = _rollback_resolution(ev)
            clean_pending = _rollback_is_clean_pending(ev)
        except ValueError as exc:
            results.append({
                "release_id": ev.get("release_id"),
                "family": ev.get("family"),
                "timeframe": ev.get("timeframe"),
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": str(exc),
            })
            continue
        if resolution is not None:
            continue

        if not clean_pending:
            results.append({
                "release_id": ev.get("release_id"),
                "family": ev.get("family"),
                "timeframe": ev.get("timeframe"),
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": (
                    "rollback enforcement has a prior or malformed attempt; "
                    "operator reconciliation is required"
                ),
            })
            continue

        release_id = ev.get("release_id")
        family = ev.get("family")
        timeframe = ev.get("timeframe")

        if not family or not timeframe:
            results.append({
                "release_id": release_id,
                "ok": False,
                "error": "evaluation missing family/timeframe",
            })
            continue

        release, release_error = validate_post_apply_release_identity(
            rel_data or {},
            release_id=str(release_id or ""),
            requested_family=str(family),
            requested_timeframe=str(timeframe),
        )
        if release_error is not None:
            reason = str(release_error.get("reason") or "release_identity_invalid")
            ev["rollback_enforcement_status"] = "reconciliation_required"
            ev["rollback_reconciliation_reason"] = reason
            save_effectiveness_evaluation(root, ev)
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": reason,
            })
            continue
        assert release is not None

        canonical_combo = (
            f"{release['family']}_{str(release['timeframe']).lower()}"
        )
        ev_combo = ev.get("combo_key")
        if (
            ev.get("release_id") != release.get("release_id")
            or ev.get("family") != release.get("family")
            or str(ev.get("timeframe") or "").lower()
            != str(release.get("timeframe") or "").lower()
            or (
                ev_combo is not None
                and str(ev_combo).strip().lower() != canonical_combo.lower()
            )
        ):
            reason = "effectiveness_release_identity_mismatch"
            ev["rollback_enforcement_status"] = "reconciliation_required"
            ev["rollback_reconciliation_reason"] = reason
            save_effectiveness_evaluation(root, ev)
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": reason,
            })
            continue

        observation = load_observation_result(root, str(release_id))
        rollback_recommendation = load_rollback_recommendation(
            root, str(release_id)
        )
        evidence_errors: list[dict[str, Any]] = []
        valid_observation = observation
        valid_rollback_recommendation = rollback_recommendation
        for evidence_kind, evidence in (
            ("observation", observation),
            ("rollback_recommendation", rollback_recommendation),
        ):
            evidence_error = validate_release_bound_evidence(
                release,
                evidence,
                evidence_kind=evidence_kind,
            )
            if evidence_error is not None:
                evidence_errors.append(evidence_error)
                if evidence_kind == "observation":
                    valid_observation = None
                else:
                    valid_rollback_recommendation = None

        rollback_supported = bool(
            (
                isinstance(valid_rollback_recommendation, dict)
                and valid_rollback_recommendation.get("rollback_recommended")
                is True
            )
            or (
                isinstance(valid_observation, dict)
                and valid_observation.get("status") == "rollback_recommended"
                and valid_observation.get("recommendation")
                == "rollback_recommended"
            )
        )
        if not rollback_supported:
            reason = (
                str(evidence_errors[0].get("reason"))
                if evidence_errors
                else "rollback_supporting_provenance_missing"
            )
            ev["rollback_enforcement_status"] = "reconciliation_required"
            ev["rollback_reconciliation_reason"] = reason
            save_effectiveness_evaluation(root, ev)
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": reason,
            })
            continue

        combo_key = f"{family}_{timeframe.lower()}"
        release_ps_id = release.get("parameter_set_id")
        if not release_ps_id:
            results.append({
                "release_id": release_id,
                "ok": False,
                "error": "release has no parameter_set_id; cannot verify stale rollback safety",
            })
            continue

        prev_ps_id = release.get("previous_parameter_set_id")
        if not prev_ps_id:
            results.append({
                "release_id": release_id,
                "ok": False,
                "error": "release has no previous_parameter_set_id to rollback to",
            })
            continue

        # Operator rollback and the automatic enforcer share the same release
        # capital lineage.  If the Operator has already completed the exact
        # rollback, that is an enforced rollback fact—not an unrelated active
        # change to cancel.  Only canonical DB release/history/active truth may
        # activate this path.
        existing_operator_rollback = _load_completed_operator_rollback_fact(
            root,
            release_id=str(release_id),
            family=str(family),
            timeframe=str(timeframe),
        )
        if (
            release.get("observation_status") == "rolled_back"
            and existing_operator_rollback is None
        ):
            reason = "rolled_back_release_lacks_operator_capital_proof"
            ev["rollback_enforcement_status"] = "reconciliation_required"
            ev["rollback_reconciliation_reason"] = reason
            save_effectiveness_evaluation(root, ev)
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": reason,
            })
            continue

        # 先把不可重复执行的 intent 写入治理真源，再执行资本状态变更。若动作
        # 成功而最终状态写回失败，DB 会保留 in_progress；下次运行只能进入人工
        # reconciliation，绝不会自动重放同一回滚。
        if existing_operator_rollback is not None:
            # The capital action predates this reconciliation pass.  Anchor the
            # proof interval to the immutable history fact so the attempt does
            # not claim that the enforcer itself performed the rollback.
            now = existing_operator_rollback["fact_observed_at"].isoformat()
        else:
            now = datetime.now(timezone.utc).isoformat()
        ev["rollback_enforcement_status"] = "in_progress"
        ev["rollback_enforcement_attempt_id"] = f"rb_{uuid4().hex}"
        ev["rollback_enforcement_started_at"] = now
        ev.pop("rollback_reconciliation_reason", None)
        modified = True
        claimed = dict(save_effectiveness_evaluation(root, ev))
        modified = False
        if (
            claimed.get("rollback_enforcement_status") != "in_progress"
            or claimed.get("rollback_enforcement_attempt_id")
            != ev.get("rollback_enforcement_attempt_id")
        ):
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "reconciliation_required": True,
                "error": (
                    "rollback action ownership was not acquired; "
                    "canonical state must be reconciled"
                ),
            })
            continue
        ev.clear()
        ev.update(claimed)

        if existing_operator_rollback is not None:
            _set_enforced_rollback_fields(
                ev,
                target_parameter_set_id=existing_operator_rollback[
                    "target_parameter_set_id"
                ],
                operation_id=existing_operator_rollback["operation_id"],
                finished_at=existing_operator_rollback[
                    "fact_observed_at"
                ].isoformat(),
            )
            terminal = dict(save_effectiveness_evaluation(root, ev))
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": terminal.get("rollback_enforcement_status") == "enforced",
                "resolved_by_existing_rollback": True,
                "reconciliation_required": False,
                "rollback_result": {
                    "ok": True,
                    "code": "OPERATOR_ROLLBACK_ALREADY_COMPLETED",
                    "operation_id": existing_operator_rollback["operation_id"],
                    "to_parameter_set_id": existing_operator_rollback[
                        "target_parameter_set_id"
                    ],
                },
            })
            continue

        # 执行回滚
        from aats.data_platform.decision_system.active_parameter_apply import (
            rollback_active_parameter_set,
        )

        rb_result = rollback_active_parameter_set(
            root,
            family=family,
            timeframe=timeframe,
            to_parameter_set_id=prev_ps_id,
            expected_from_parameter_set_id=release_ps_id,
            expected_from_recommendation_id=release.get("recommendation_id"),
            expected_previous_parameter_set_id=prev_ps_id,
            trigger_release_id=str(release_id),
            actor="release_effectiveness_auto_rollback",
            notes=(
                f"自动回滚: release {release_id} 的 effectiveness 评估结论为"
                f" rollback_triggered"
            ),
        )

        rb_ok = rb_result.get("ok", False)
        active_set_changed = rb_result.get("code") == "ACTIVE_SET_CHANGED"
        rb_reason = rb_result.get("reason") or ""
        # Bug 8 Layer 2: 识别 "无合法 rollback target" 类型的失败 (而非临时锁/IO
        # 错误)，降级为 soft pause —— 不硬回滚、不重试，通过 active_decisions
        # pause 阻止未来新 apply，实盘当前参数保持不变。
        _SOFT_PAUSE_REASONS = {
            "target_deprecated_too_old",
            "target_deprecated_without_timestamp",
            "no_apply_history_for_target",
            "target_not_found_or_wrong_combo",
            "target_has_known_bad_effectiveness",
        }
        rb_reason_head = rb_reason.split(":", 1)[0]
        is_permanent_no_target = rb_reason_head in _SOFT_PAUSE_REASONS

        resolved_by_existing_rollback = False
        if rb_ok:
            _set_enforced_rollback_fields(
                ev,
                target_parameter_set_id=str(prev_ps_id),
                operation_id=rb_result.get("operation_id"),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            modified = True
        elif active_set_changed:
            raced_operator_rollback = _load_completed_operator_rollback_fact(
                root,
                release_id=str(release_id),
                family=str(family),
                timeframe=str(timeframe),
            )
            if raced_operator_rollback is not None:
                _set_enforced_rollback_fields(
                    ev,
                    target_parameter_set_id=raced_operator_rollback[
                        "target_parameter_set_id"
                    ],
                    operation_id=raced_operator_rollback["operation_id"],
                    finished_at=raced_operator_rollback[
                        "fact_observed_at"
                    ].isoformat(),
                )
                resolved_by_existing_rollback = True
                rb_ok = True
            else:
                # A non-rollback active change means this release no longer
                # controls the combo.  Only that distinct fact is cancellation.
                ev["rollback_cancelled"] = True
                ev["rollback_enforcement_status"] = "cancelled"
                ev["rollback_cancelled_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                ev["rollback_cancelled_reason"] = (
                    "active_parameter_set_changed_before_rollback: "
                    f"expected={release_ps_id} "
                    f"actual={rb_result.get('from_parameter_set_id')}"
                )
                ev["rollback_enforcement_finished_at"] = ev[
                    "rollback_cancelled_at"
                ]
                ev["rollback_soft_pause_applied"] = False
                ev["rollback_capital_proof_version"] = (
                    _ROLLBACK_CAPITAL_PROOF_VERSION
                )
                ev["rollback_capital_proof_kind"] = "active_parameter_changed"
                ev["rollback_capital_proof_active_parameter_set_id"] = (
                    rb_result.get("from_parameter_set_id")
                )
                ev.pop("rollback_capital_operation_id", None)
                ev.pop("rollback_capital_proof_decision_status", None)
            modified = True
        elif is_permanent_no_target:
            # Layer 2 soft pause: 无合法 rollback target → 写 combo pause +
            # 标记 evaluation cancelled (带 soft_paused reason)，避免无限重试。
            from aats.data_platform.governance._db_util import try_governance_db
            from aats.data_platform.governance.recommendations_db import (
                db_set_combo_pause,
            )
            from aats.data_platform.governance.active_params_db import (
                db_try_acquire_parameter_apply_lock,
            )
            from sqlalchemy.orm import Session as _SQLSession

            pause_ok = False
            pause_error: str | None = None
            engine, db_ok = try_governance_db()
            if db_ok and engine is not None:
                try:
                    with _SQLSession(engine) as pause_sess:
                        if db_try_acquire_parameter_apply_lock(
                            pause_sess,
                            family=family,
                            timeframe=timeframe,
                        ):
                            pause_ok = db_set_combo_pause(
                                pause_sess,
                                family=family,
                                timeframe=timeframe,
                                reason=(
                                    "soft_pause_auto_rollback_no_valid_target: "
                                    f"release={release_id} reason={rb_reason}"
                                ),
                            ) is True
                        else:
                            pause_error = "parameter_combo_lock_busy"
                        pause_sess.commit()
                except Exception as exc:
                    pause_error = type(exc).__name__
                finally:
                    engine.dispose()
            ev["rollback_soft_pause_applied"] = pause_ok
            if pause_ok:
                ev["rollback_cancelled"] = True
                ev["rollback_enforcement_status"] = "cancelled"
                ev["rollback_cancelled_at"] = datetime.now(timezone.utc).isoformat()
                ev["rollback_cancelled_reason"] = (
                    f"soft_paused_no_valid_rollback_target: {rb_reason}"
                )
                ev["rollback_enforcement_finished_at"] = ev[
                    "rollback_cancelled_at"
                ]
                ev["rollback_capital_proof_version"] = (
                    _ROLLBACK_CAPITAL_PROOF_VERSION
                )
                ev["rollback_capital_proof_kind"] = "soft_pause"
                ev["rollback_capital_proof_decision_status"] = "pause"
                ev.pop("rollback_capital_operation_id", None)
                ev.pop("rollback_capital_proof_active_parameter_set_id", None)
            else:
                ev.pop("rollback_cancelled", None)
                ev.pop("rollback_cancelled_at", None)
                ev.pop("rollback_cancelled_reason", None)
                ev.setdefault("rollback_attempts", 0)
                ev["rollback_attempts"] += 1
                ev["last_rollback_error"] = (
                    "soft pause was not persisted"
                    + (f" ({pause_error})" if pause_error else "")
                )
                ev["rollback_enforcement_status"] = "reconciliation_required"
                ev["rollback_enforcement_finished_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                ev["rollback_reconciliation_reason"] = "soft_pause_not_persisted"
            modified = True

            # Layer 3 structured log → Loki/Grafana alert (Bug 6 链路)
            # Task P3-1：修 F821 —— 原先写 `log.error(...)` 但 `log` 未定义；
            # 与本文件其他 except 分支一致走 logging.getLogger(__name__)。
            import logging as _logging
            _logging.getLogger(__name__).error(
                "rdp_rollback_soft_paused release_id=%s combo=%s reason=%r "
                "pause_applied=%s",
                release_id, combo_key, rb_reason, pause_ok,
                extra={
                    "event_name": "rdp_rollback_soft_paused",
                    "release_id": release_id,
                    "family": family,
                    "timeframe": timeframe,
                    "rollback_target": prev_ps_id,
                    "rollback_reason": rb_reason,
                    "pause_applied": pause_ok,
                },
            )
        else:
            # 一旦调用过回滚入口，就不能再假设失败一定发生在副作用之前。
            # 记录 reconciliation_required，禁止自动重试，交由 operator 根据
            # active parameter / audit / DB 三方证据确认真实结果。
            ev.setdefault("rollback_attempts", 0)
            ev["rollback_attempts"] += 1
            ev["last_rollback_error"] = rb_result.get("message", "unknown error")
            ev["rollback_enforcement_status"] = "reconciliation_required"
            ev["rollback_enforcement_finished_at"] = datetime.now(timezone.utc).isoformat()
            ev["rollback_reconciliation_reason"] = "rollback_outcome_not_proven"
            modified = True

        results.append({
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe,
            "ok": rb_ok,
            "soft_paused": (
                (not rb_ok)
                and is_permanent_no_target
                and bool(ev.get("rollback_soft_pause_applied"))
            ),
            "cancelled_due_to_active_change": (
                active_set_changed and not resolved_by_existing_rollback
            ),
            "resolved_by_existing_rollback": resolved_by_existing_rollback,
            "reconciliation_required": (
                ev.get("rollback_enforcement_status") == "reconciliation_required"
            ),
            "rollback_result": rb_result,
        })

        # 每个资本动作单独收口。写回失败时异常向上传播，而 DB 中此前持久化的
        # in_progress anchor 会阻止下一轮重复执行。
        if modified:
            save_effectiveness_evaluation(root, ev)
            modified = False

    return results
