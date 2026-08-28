"""Parameter Apply / Rollback 逻辑.

工作包 C 交付物：将已批准 recommendation 受控地应用为 active parameter set，
并支持回滚。

核心约束:
  - apply 必须是显式动作
  - apply 必须可审计
  - apply 必须可回滚
  - recommendation 不能自动生效

数据流:
  approved recommendation
    → 解析 target_parameter_set_id
    → 从 parameter_registry 获取 values
    → DB 事务: UPSERT governance.active_parameter_sets + INSERT history
    → 一次提交，要么全成功要么全回滚

回滚:
  → 从 history 查找上一个 active parameter set
  → 重新写为 active
  → 写入 rollback history
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aats.data_platform.governance._time_util import parse_iso_datetime_utc
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
    try_governance_db,
)

log = logging.getLogger(__name__)


# ── 路径常量 ───────────────────────────────────────────────────────

PARAMETER_APPLY_HISTORY_FILENAME = "parameter_apply_history.json"
DECISION_SYSTEM_DIR = "artifacts/decision_system"
GOVERNANCE_DIR = "artifacts/governance"


def _make_operation_id() -> str:
    return f"op_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _approval_attestation_valid(recommendation: dict[str, Any]) -> bool:
    """Require a named approver and a canonical, non-future UTC timestamp."""
    approved_by = recommendation.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        return False
    raw = recommendation.get("approved_at")
    try:
        if isinstance(raw, datetime):
            if raw.tzinfo is None or raw.utcoffset() is None:
                return False
            approved_at = raw.astimezone(timezone.utc)
        elif isinstance(raw, str):
            token = raw.strip()
            if not token or not (token.endswith("Z") or token.endswith("+00:00")):
                return False
            approved_at = parse_iso_datetime_utc(
                token,
                context="active_parameter_apply.approved_at",
            )
        else:
            return False
    except (TypeError, ValueError):
        return False
    return approved_at <= datetime.now(timezone.utc) + timedelta(minutes=5)


# ── Apply History 管理 ─────────────────────────────────────────────


def _apply_history_path(project_root: Path) -> Path:
    return project_root / DECISION_SYSTEM_DIR / PARAMETER_APPLY_HISTORY_FILENAME


def load_apply_history(project_root: Path) -> dict[str, Any]:
    """加载 parameter_apply_history.json."""
    engine, ok = try_governance_db()
    if ok:
        try:
            with Session(engine) as session:
                rows = session.execute(
                    text(
                        """
                        SELECT operation_id, operation_type, family, timeframe,
                               from_parameter_set_id, to_parameter_set_id,
                               recommendation_id, actor, notes, created_at
                        FROM governance.parameter_apply_history
                        ORDER BY created_at ASC
                        """
                    ),
                ).mappings().fetchall()
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "operations": [
                    {
                        **dict(row),
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    }
                    for row in rows
                ],
            }
        except Exception as exc:
            log.warning(
                "无法从 DB 加载 apply history (%s)",
                type(exc).__name__,
            )
        finally:
            if engine is not None:
                engine.dispose()

    path = _apply_history_path(project_root)
    if not path.exists():
        return {"generated_at": None, "operations": []}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("无法加载 apply history %s: %s", path, exc)
        return {"generated_at": None, "operations": []}


def save_apply_history(history: dict[str, Any], project_root: Path) -> Path:
    """保存 parameter_apply_history.json（原子写入）."""
    from aats.data_platform.governance._atomic_io import atomic_json_write

    path = _apply_history_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    history["generated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(history, path)
    engine, ok = try_governance_db()
    if ok:
        try:
            with Session(engine) as session, session.begin():
                for op in history.get("operations", []):
                    if not isinstance(op, dict) or not op.get("operation_id"):
                        continue
                    session.execute(
                        text(
                            """
                            INSERT INTO governance.parameter_apply_history
                                (operation_id, operation_type, family, timeframe,
                                 from_parameter_set_id, to_parameter_set_id,
                                 recommendation_id, actor, notes, created_at)
                            VALUES
                                (:operation_id, :operation_type, :family, :timeframe,
                                 :from_parameter_set_id, :to_parameter_set_id,
                                 :recommendation_id, :actor, :notes, :created_at)
                            ON CONFLICT (operation_id) DO UPDATE SET
                                operation_type = EXCLUDED.operation_type,
                                family = EXCLUDED.family,
                                timeframe = EXCLUDED.timeframe,
                                from_parameter_set_id = EXCLUDED.from_parameter_set_id,
                                to_parameter_set_id = EXCLUDED.to_parameter_set_id,
                                recommendation_id = EXCLUDED.recommendation_id,
                                actor = EXCLUDED.actor,
                                notes = EXCLUDED.notes,
                                created_at = EXCLUDED.created_at
                            """
                        ),
                        {
                            "operation_id": op.get("operation_id"),
                            "operation_type": op.get("operation_type"),
                            "family": op.get("family"),
                            "timeframe": str(op.get("timeframe") or "").lower(),
                            "from_parameter_set_id": op.get("from_parameter_set_id"),
                            "to_parameter_set_id": op.get("to_parameter_set_id"),
                            "recommendation_id": op.get("recommendation_id"),
                            "actor": op.get("actor"),
                            "notes": op.get("notes"),
                            "created_at": (
                                parse_iso_datetime_utc(
                                    str(op.get("created_at")),
                                    context="active_parameter_apply.history.created_at",
                                )
                                if op.get("created_at")
                                else datetime.now(timezone.utc)
                            ),
                        },
                    )
        except Exception as exc:
            log.warning(
                "apply history DB 同步失败 (%s)",
                type(exc).__name__,
            )
        finally:
            if engine is not None:
                engine.dispose()
    log.info("已保存 apply history -> %s (%d operations)", path, len(history.get("operations", [])))
    return path


def get_latest_operation_for_combo(
    history: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """获取指定 combo 最近的 apply/rollback 操作."""
    combo_key = f"{family}_{timeframe.lower()}"
    for op in reversed(history.get("operations", [])):
        if op.get("family") == family and op.get("timeframe") == timeframe:
            return op
        # 兼容 combo_key 查找
        if f"{op.get('family')}_{op.get('timeframe', '').lower()}" == combo_key:
            return op
    return None


def get_previous_parameter_set_id(
    history: dict[str, Any],
    family: str,
    timeframe: str,
) -> str | None:
    """获取指定 combo 上一个 active parameter set id.

    用于回滚时确定回滚目标。
    跳过最近的一条（当前），返回前一条。
    """
    combo_key = f"{family}_{timeframe.lower()}"
    found_current = False
    for op in reversed(history.get("operations", [])):
        op_combo = f"{op.get('family')}_{op.get('timeframe', '').lower()}"
        if op_combo != combo_key:
            continue
        if op.get("operation_type") != "apply":
            continue
        if not found_current:
            found_current = True
            continue
        return op.get("to_parameter_set_id")
    return None


# ── Apply 操作 ─────────────────────────────────────────────────────


def apply_approved_recommendation(
    project_root: Path,
    *,
    recommendation_id: str,
    actor: str = "operator",
    notes: str | None = None,
    dry_run: bool = False,
    release_id: str | None = None,
    gate_result: dict[str, Any] | None = None,
    promotion_authorization: Any | None = None,
) -> dict[str, Any]:
    """从已批准的 recommendation 应用参数到 active parameter set.

    流程:
      1. 从 recommendation_registry 查找已批准的 recommendation
      2. 从 parameter_registry 查找 target_parameter_set_id
      3. DB 模式: 单事务 UPSERT active_parameter_sets + INSERT history
         文件 fallback: 写入 active_parameter_registry.json + per-file JSON + history JSON

    Returns
    -------
    dict  操作结果 {"ok": bool, "message": str, ...}
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        find_recommendation,
        load_recommendation_registry,
    )
    from aats.data_platform.governance.parameter_registry import load_registry
    from aats.data_platform.operations.environment_guard import (
        get_current_environment,
        get_policy,
        guard_parameter_apply,
    )
    from aats.data_platform.production_workflow.pre_apply_gate import (
        gate_result_allows_apply,
    )

    env = get_current_environment()
    apply_guard = guard_parameter_apply(env)
    if not apply_guard.allowed:
        return {"ok": False, "message": apply_guard.reason, "environment": env}
    if (
        not dry_run
        and release_id is None
        and has_explicit_governance_db_configuration(project_root)
    ):
        return {
            "ok": False,
            "code": "release_required",
            "message": (
                "受管环境禁止无 parameter release 的 direct apply；"
                "请使用 approve-and-release 或 releases/create 完整链路"
            ),
            "environment": env,
        }

    policy = get_policy(env)
    # A-0.5: prod 写闸改由 API 层的 HMAC apply-token 强制，不再用 env flag。

    # 1. 加载 recommendation
    rec_path = project_root / DECISION_SYSTEM_DIR / "recommendation_registry.json"
    rec_registry = load_recommendation_registry(rec_path)
    rec = find_recommendation(rec_registry, recommendation_id)

    if rec is None:
        return {"ok": False, "message": f"未找到 recommendation: {recommendation_id}"}

    if rec["status"] != "approved":
        return {
            "ok": False,
            "message": f"recommendation 状态为 '{rec['status']}'，必须为 approved 才能 apply",
        }

    # Recheck the exact recommendation evidence for direct, release-owned and
    # dry-run calls alike.  Keep this ahead of every parameter-registry read so
    # a dry run cannot act as a legacy-evidence disclosure/bypass path.
    from aats.data_platform.decision_system.promotion_guard import (
        PromotionQualificationBlockedError,
        require_apply_promotion_qualification,
    )

    try:
        qualification_verdict = require_apply_promotion_qualification(
            project_root,
            rec,
            authorization=promotion_authorization,
        )
    except PromotionQualificationBlockedError as exc:
        qualification_failure = exc.to_dict()
        qualification_failure["environment"] = env
        return qualification_failure
    qualified_values_fingerprint = qualification_verdict.to_dict().get(
        "parameter_values_fingerprint"
    )

    if policy["require_approval"] and not _approval_attestation_valid(rec):
        return {
            "ok": False,
            "code": "recommendation_approval_attestation_invalid",
            "message": (
                f"{env} environment requires a named approver and canonical "
                "non-future UTC approval timestamp before apply"
            ),
            "environment": env,
        }

    if policy["require_gate_pass"]:
        if gate_result is None:
            return {
                "ok": False,
                "message": (
                    f"{env} environment requires pre-apply gate; "
                    "use create_parameter_release()/release flow instead of direct apply"
                ),
                "environment": env,
            }
        if not gate_result_allows_apply(gate_result):
            return {
                "ok": False,
                "message": f"gate blocked apply: {gate_result.get('blocking_reasons')}",
                "environment": env,
                "gate_result": gate_result,
            }

    if env == "prod" and release_id is None:
        return {
            "ok": False,
            "message": "prod direct apply is not allowed; create a parameter release instead",
            "environment": env,
        }

    ps_id = rec.get("target_parameter_set_id")
    if not ps_id:
        return {
            "ok": False,
            "message": f"recommendation {recommendation_id} 没有 target_parameter_set_id",
        }

    # 2. 从 governance registry 获取参数值
    gov_reg_path = project_root / GOVERNANCE_DIR / "current_parameter_registry.json"
    gov_registry = load_registry(
        gov_reg_path,
        fail_closed_on_db_error=True,
    )

    target_ps = None
    for ps in gov_registry.get("parameter_sets", []):
        if ps["parameter_set_id"] == ps_id:
            target_ps = ps
            break

    if target_ps is None:
        return {"ok": False, "message": f"parameter_registry 中未找到 {ps_id}"}

    from aats.data_platform.governance.parameter_identity import (
        parameter_values_fingerprint,
    )

    try:
        registry_values_fingerprint = parameter_values_fingerprint(
            target_ps.get("values")
        )
    except ValueError:
        return {
            "ok": False,
            "code": "parameter_values_invalid",
            "message": "parameter registry 的 values 不是可验证 JSON object",
            "environment": env,
            "parameter_set_id": ps_id,
        }
    if registry_values_fingerprint != qualified_values_fingerprint:
        return {
            "ok": False,
            "code": "parameter_set_evidence_fingerprint_mismatch",
            "message": (
                "parameter set 当前 values 与精确 Phase 6 资格证据不一致；"
                "必须生成新的不可变参数集并重新审批"
            ),
            "environment": env,
            "parameter_set_id": ps_id,
        }

    family = target_ps["family"]
    timeframe = target_ps["timeframe"]
    values = target_ps["values"]
    combo_key = f"{family}_{timeframe.lower()}"

    result = {
        "ok": True,
        "operation_type": "apply",
        "combo_key": combo_key,
        "family": family,
        "timeframe": timeframe,
        "recommendation_id": recommendation_id,
        "parameter_set_id": ps_id,
        "values": values,
        "parameter_values_fingerprint": registry_values_fingerprint,
        "environment": env,
        "release_id": release_id,
    }

    if dry_run:
        result["message"] = f"[DRY RUN] 将 apply {ps_id} 到 {combo_key}"
        return result

    op_id = _make_operation_id()

    # DB 单事务原子写入
    from aats.data_platform.db import get_session
    from aats.data_platform.governance.active_params_db import (
        db_append_history,
        db_get_known_bad_release_id_for_parameter_set,
        db_get_parameter_set_for_update,
        db_get_pending_rollback_release_id,
        db_try_acquire_parameter_apply_lock,
        db_upsert_active_set,
    )
    from aats.data_platform.governance.recommendations_db import (
        db_get_active_decision_for_update,
        db_get_recommendation_for_update,
    )

    with get_session() as session:
        # 所有人工/API/scheduler apply 在同一事务内共用 combo 锁。使用 try-lock
        # 而非等待，避免 Gate 证据在长时间排队后悄然过期。
        if not db_try_acquire_parameter_apply_lock(
            session,
            family=family,
            timeframe=timeframe,
        ):
            return {
                "ok": False,
                "code": "parameter_apply_conflict",
                "message": f"{combo_key} 正有另一个参数发布事务，请稍后重新核验",
                "environment": env,
            }

        # release-owned apply 必须把 pending release 与 active/history/parameter
        # lifecycle 放在同一个资本事务里。先锁住确切 release，证明调用者没有
        # 换绑 recommendation/parameter/combo，也防止两个恢复者同时收口。
        locked_release = None
        if release_id is not None:
            from aats.data_platform.governance.operational_state_db import (
                db_get_parameter_release_for_update,
            )

            locked_release = db_get_parameter_release_for_update(
                session,
                release_id=release_id,
                family=family,
                timeframe=timeframe,
                recommendation_id=recommendation_id,
                parameter_set_id=ps_id,
            )
            canonical_family = family.strip().lower()
            canonical_timeframe = timeframe.strip().lower()
            canonical_combo = f"{canonical_family}_{canonical_timeframe}"
            release_identity_valid = bool(
                locked_release is not None
                and str(locked_release.get("release_id") or "") == release_id
                and str(locked_release.get("family") or "").strip().lower()
                == canonical_family
                and str(locked_release.get("timeframe") or "").strip().lower()
                == canonical_timeframe
                and str(locked_release.get("combo_key") or "").strip().lower()
                == canonical_combo
                and locked_release.get("recommendation_id") == recommendation_id
                and locked_release.get("parameter_set_id") == ps_id
                and locked_release.get("apply_result") == "pending"
                and locked_release.get("observation_status") == "pending"
                and (
                    locked_release.get("apply_reconciliation_required") is None
                    or locked_release.get("apply_reconciliation_required") is False
                )
                and type(
                    locked_release.get("apply_reconciliation_required", False)
                ) is bool
                and locked_release.get("apply_reconciliation_reason") is None
                and locked_release.get("apply_operation_id") is None
                and type(locked_release.get("observation_window_hours")) is int
                and locked_release.get("observation_window_hours") > 0
            )
            if not release_identity_valid:
                return {
                    "ok": False,
                    "code": "release_state_changed",
                    "message": (
                        "pending release 缺失、身份不一致或已被其他事务推进；"
                        "本次 apply 已零资本写入阻断"
                    ),
                    "environment": env,
                    "release_id": release_id,
                }

        pending_rollback_release_id = db_get_pending_rollback_release_id(
            session,
            family=family,
            timeframe=timeframe,
        )
        if pending_rollback_release_id is not None:
            return {
                "ok": False,
                "code": "pending_rollback",
                "message": (
                    f"{combo_key} 存在未收口回滚 {pending_rollback_release_id}，"
                    "完成回滚或人工对账前禁止发布新参数"
                ),
                "environment": env,
                "pending_rollback_release_id": pending_rollback_release_id,
            }

        known_bad_release_id = db_get_known_bad_release_id_for_parameter_set(
            session,
            family=family,
            timeframe=timeframe,
            parameter_set_id=ps_id,
        )
        if known_bad_release_id is not None:
            return {
                "ok": False,
                "code": "known_bad_parameter_set",
                "message": (
                    f"参数集 {ps_id} 已被 release {known_bad_release_id} 的效果评估"
                    "判定为 rollback_triggered；必须生成新的不可变参数集并重新审批"
                ),
                "environment": env,
                "known_bad_release_id": known_bad_release_id,
            }

        if policy["require_gate_pass"]:
            # Gate is a snapshot.  Re-read the mutable combo decision only
            # after acquiring the same lock used by soft-pause/rollback, so a
            # pause committed after Gate evaluation cannot be bypassed.
            locked_decision = db_get_active_decision_for_update(
                session,
                family=family,
                timeframe=timeframe,
            )
            allowed_decision_statuses = {
                "keep_active",
                "lower_priority",
                "require_review",
            }
            locked_status = (
                locked_decision.get("current_status")
                if isinstance(locked_decision, dict)
                else None
            )
            if locked_status not in allowed_decision_statuses:
                return {
                    "ok": False,
                    "code": "decision_state_changed",
                    "message": (
                        f"{combo_key} 当前决策状态缺失、无效或已暂停；"
                        "旧 Gate 结果不可继续用于 apply"
                    ),
                    "environment": env,
                    "current_decision_status": locked_status,
                }

        # 在持有 combo 锁后重读 recommendation 行并加行锁。两个 approved
        # recommendation 即使同时通过了事务外预检，也只有先到者能写 active；
        # 后到者会看到自身已被 superseded，绝不能继续覆盖。
        locked_rec = db_get_recommendation_for_update(session, recommendation_id)
        identity_fields = (
            "family",
            "symbol",
            "timeframe",
            "recommendation_type",
            "target_parameter_set_id",
            "source_round_id",
            "confidence",
            "reason",
            "evidence_bundle_ref",
        )
        identity_matches = isinstance(locked_rec, dict)
        if identity_matches:
            for field in identity_fields:
                expected = rec.get(field)
                actual = locked_rec.get(field)
                if field == "timeframe":
                    expected = str(expected or "").lower()
                    actual = str(actual or "").lower()
                if actual != expected:
                    identity_matches = False
                    break
        locked_approval_state_ok = bool(
            isinstance(locked_rec, dict)
            and locked_rec.get("status") == "approved"
            and locked_rec.get("recommendation_type") == "parameter_upgrade"
            and locked_rec.get("target_parameter_set_id") == ps_id
            and locked_rec.get("family") == family
            and locked_rec.get("symbol") == target_ps.get("symbol")
            and str(locked_rec.get("timeframe") or "").lower() == timeframe.lower()
        )
        locked_attestation_ok = bool(
            not policy["require_approval"]
            or (
                isinstance(locked_rec, dict)
                and _approval_attestation_valid(locked_rec)
            )
        )
        if identity_matches and locked_approval_state_ok and not locked_attestation_ok:
            return {
                "ok": False,
                "code": "recommendation_approval_attestation_invalid",
                "message": (
                    "锁内 recommendation 批准人或 canonical UTC 批准时间无效；"
                    "本次 apply 已零资本写入阻断"
                ),
                "environment": env,
            }
        if not identity_matches or not locked_approval_state_ok:
            return {
                "ok": False,
                "code": "recommendation_state_changed",
                "message": (
                    "recommendation 在 apply 事务开始前已变更或不再 approved，"
                    "必须重新执行资格与 Gate 核验"
                ),
                "environment": env,
            }

        expected_values = target_ps.get("values")
        expected_source_round_id = target_ps.get("source_round_id")
        expected_symbol = target_ps.get("symbol")
        if not (
            type(expected_values) is dict
            and isinstance(expected_source_round_id, str)
            and expected_source_round_id.strip()
            and isinstance(expected_symbol, str)
            and expected_symbol.strip()
        ):
            return {
                "ok": False,
                "code": "parameter_set_identity_incomplete",
                "message": (
                    "parameter registry 缺少 immutable values/source_round/symbol；"
                    "本次 apply 已零资本写入阻断"
                ),
                "environment": env,
                "parameter_set_id": ps_id,
            }
        locked_ps = db_get_parameter_set_for_update(
            session,
            parameter_set_id=ps_id,
            family=family,
            timeframe=timeframe,
            symbol=expected_symbol,
            source_round_id=expected_source_round_id,
            expected_values=expected_values,
        )
        locked_values_fingerprint = None
        if isinstance(locked_ps, dict):
            try:
                locked_values_fingerprint = parameter_values_fingerprint(
                    locked_ps.get("values")
                )
            except ValueError:
                locked_values_fingerprint = None
            if locked_values_fingerprint != qualified_values_fingerprint:
                return {
                    "ok": False,
                    "code": "parameter_set_evidence_fingerprint_mismatch",
                    "message": (
                        "锁内 parameter set values 与精确 Phase 6 资格证据不一致；"
                        "本次 apply 已零资本写入阻断"
                    ),
                    "environment": env,
                    "parameter_set_id": ps_id,
                }
        parameter_set_identity_valid = bool(
            isinstance(locked_ps, dict)
            and locked_ps.get("parameter_set_id") == ps_id
            and locked_ps.get("family") == family
            and locked_ps.get("symbol") == rec.get("symbol")
            and str(locked_ps.get("timeframe") or "").lower()
            == timeframe.lower()
            and locked_ps.get("source_round_id") == expected_source_round_id
            and type(locked_ps.get("values")) is dict
            and locked_ps.get("values") == expected_values
            and locked_values_fingerprint == qualified_values_fingerprint
            and locked_ps.get("status") in {"candidate", "frozen"}
        )
        if not parameter_set_identity_valid:
            return {
                "ok": False,
                "code": "parameter_set_state_changed",
                "message": (
                    "parameter set 在 apply 事务开始前已变更、身份不完整或不再处于"
                    " candidate/frozen；本次 apply 已零资本写入阻断"
                ),
                "environment": env,
                "parameter_set_id": ps_id,
            }
        # 此后所有资本写入只使用锁内 canonical values/source，绝不继续使用
        # 事务外 registry 快照。
        values = locked_ps["values"]
        target_source_round_id = locked_ps["source_round_id"]

        if policy["require_gate_pass"]:
            from aats.data_platform.governance.operational_state_db import (
                db_get_gate_result_by_run_id,
            )

            gate_run_id = str((gate_result or {}).get("gate_run_id") or "").strip()
            persisted_gate = (
                db_get_gate_result_by_run_id(session, gate_run_id)
                if gate_run_id
                else None
            )
            gate_identity_matches = bool(
                isinstance(persisted_gate, dict)
                and persisted_gate.get("gate_run_id") == gate_run_id
                and persisted_gate.get("recommendation_id") == recommendation_id
                and gate_result_allows_apply(persisted_gate)
                and (
                    release_id is None
                    or persisted_gate.get("release_id") == release_id
                )
            )
            if not gate_identity_matches:
                return {
                    "ok": False,
                    "code": "gate_state_changed",
                    "message": (
                        "持久化 Gate 结果缺失、已变更或未绑定当前 release，"
                        "必须重新运行发布流程"
                    ),
                    "environment": env,
                }

        # 同一 recommendation 是一次性资本授权。即使调用方因网络超时重试，
        # 也不能产生第二条 success release/history；必须人工核验首个结果。
        prior_apply = session.execute(
            text(
                """
                SELECT operation_id
                FROM governance.parameter_apply_history
                WHERE recommendation_id = :recommendation_id
                  AND operation_type = 'apply'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"recommendation_id": recommendation_id},
        ).fetchone()

        # 查当前 active（用于 from_parameter_set_id）；legacy 数据可能缺少 history，
        # 因此同时检查 active lineage，避免旧数据上的重复 apply。
        existing = session.execute(
            text(
                "SELECT parameter_set_id, approval_recommendation_id "
                "FROM governance.active_parameter_sets "
                "WHERE family = :f AND timeframe = :t"
            ),
            {"f": family, "t": timeframe.lower()},
        ).fetchone()
        from_ps_id = existing.parameter_set_id if existing else None
        active_approval_rec = (
            getattr(existing, "approval_recommendation_id", None)
            if existing is not None
            else None
        )
        if prior_apply is not None or (
            active_approval_rec == recommendation_id and from_ps_id == ps_id
        ):
            return {
                "ok": False,
                "code": "recommendation_already_applied",
                "message": (
                    f"recommendation {recommendation_id} 已执行过 apply；"
                    "拒绝重复资本状态变更，需核验既有 release/history"
                ),
                "environment": env,
                "parameter_set_id": ps_id,
                "existing_operation_id": (
                    getattr(prior_apply, "operation_id", None)
                    if prior_apply is not None
                    else None
                ),
            }

        # Locks may have been contended long enough for the short-lived
        # authorization or underlying evidence window to expire.  Revalidate
        # immediately before the first capital-state write, using the locked
        # canonical recommendation.  A stale token/verdict must result in zero
        # active/history/release/parameter lifecycle mutations.
        try:
            lock_in_verdict = require_apply_promotion_qualification(
                project_root,
                locked_rec,
                authorization=promotion_authorization,
            )
        except PromotionQualificationBlockedError as exc:
            lock_in_failure = exc.to_dict()
            lock_in_failure["environment"] = env
            lock_in_failure["code"] = "promotion_qualification_changed_at_lock_in"
            return lock_in_failure
        lock_in_fingerprint = lock_in_verdict.to_dict().get(
            "parameter_values_fingerprint"
        )
        if lock_in_fingerprint != locked_values_fingerprint:
            return {
                "ok": False,
                "code": "promotion_evidence_changed_at_lock_in",
                "message": (
                    "锁内最终资格证据与 parameter set 内容不一致；"
                    "本次 apply 已零资本写入阻断"
                ),
                "environment": env,
                "parameter_set_id": ps_id,
            }

        db_upsert_active_set(
            session,
            family=family,
            timeframe=timeframe,
            parameter_set_id=ps_id,
            values=values,
            source_round_id=target_source_round_id,
            approval_recommendation_id=recommendation_id,
            applied_by=f"rdp_apply ({actor})",
        )
        db_append_history(
            session,
            operation_id=op_id,
            operation_type="apply",
            family=family,
            timeframe=timeframe,
            from_parameter_set_id=from_ps_id,
            to_parameter_set_id=ps_id,
            recommendation_id=recommendation_id,
            actor=actor,
            notes=notes,
        )

        # RDP Bug 2 修复: apply 成功后，把同 (family, timeframe) 下其他
        # 历史 approved parameter_upgrade recommendations 标记为 superseded。
        # 原本语义：approved ≈ "ready to apply"，但实际上一个 combo 只能有
        # 一条 live parameter set，旧 approved 被新 apply 覆盖后应该降级。
        # 不标记会导致：
        #   - `SELECT COUNT(*) WHERE status='approved'` 返回"虚假活跃"数字
        #   - UI 显示 N 条可 apply 给 operator，实际只有 1 条是 live
        # 与 Bug 2 的同事务：apply 失败会回滚 UPDATE，保证原子性。
        supersede_result = session.execute(
            text(
                """
                UPDATE governance.recommendations
                SET status = 'superseded',
                    superseded_by = :new_rec_id,
                    superseded_at = now(),
                    superseded_by_recommendation_id = :new_rec_id
                WHERE family = :family
                  AND timeframe = :timeframe
                  AND recommendation_type = 'parameter_upgrade'
                  AND status = 'approved'
                  AND recommendation_id != :current_rec_id
                  AND superseded_by IS NULL
                """,
            ),
            {
                "family": family,
                "timeframe": timeframe.lower(),
                "new_rec_id": recommendation_id,
                "current_rec_id": recommendation_id,
            },
        )
        superseded_count = supersede_result.rowcount if supersede_result.rowcount is not None else 0
        if superseded_count > 0:
            log.info(
                "apply_superseded_stale_approvals family=%s timeframe=%s "
                "current_recommendation_id=%s superseded_count=%d",
                family, timeframe.lower(), recommendation_id, superseded_count,
            )

        # RDP Bug 9 修复: parameter_sets.status 生命周期
        #
        # 原状态机: candidate → (freeze_parameter_set) frozen → deprecated
        #          candidate → (deprecate_parameter_set) deprecated
        #
        # 但 `validate_rollback_target` 规则 2 要求 status ∈ {frozen, released}，
        # 而 "released" 状态**从未被代码写入过**（grep 零命中）。结果：
        #   - 当前所有 live parameter_sets 在 governance.parameter_sets 里
        #     实际是 deprecated 状态
        #   - auto-rollback 永远找不到合法 target，全部被拒
        #
        # Forward-compat 说明 (与 Bug 8 fallback 策略配套):
        # frozen 状态是"计划中但未交付"（rdp_hardening_batch_a_detailed_design.md
        # §3 禁用 freeze_parameter_set 脚本, API 未实现, DB 0 行）。当前直接
        # candidate → released 是合理的 (frozen 不产生)。
        #
        # 如果未来 freeze API 恢复 (rdp_full_hardening_sow.md 规划在后续批次):
        #   - 应改为: 若 ps_id 已是 frozen 则保留 frozen 语义、新增 released 状态
        #   - 或者: candidate → frozen → released 走双阶段, freeze 作为审批后冻结
        # 届时 validate_rollback_target 规则 2 的 frozen 分支会自动生效，
        # Bug 8 的 deprecated 时间门控退为备选路径。
        #
        # Bug 8 (Layer 0) 已放宽 rollback 接受 deprecated (≤30d)，所以本路径
        # 把旧 released 降级 deprecated 的 invariant 不会再让 rollback 卡死。
        #
        # 修复语义：apply 本身就是 "release" 动作。apply 到 active 的 parameter_set
        # 应该在 parameter_sets 表同步标记为 `released`。同 combo 下其他 released
        # 的降级为 deprecated（每个 combo 任一时刻最多 1 个 released）。
        #
        # frozen_at 字段用途扩展：原设计是"冻结、停止修改"的时间戳，现在同时承载
        # "首次 release" 的时间戳（已 release 的参数隐含"不再修改"）。
        ps_status_result = session.execute(
            text(
                """
                UPDATE governance.parameter_sets
                SET status = 'released',
                    frozen_at = COALESCE(frozen_at, now())
                WHERE parameter_set_id = :pid
                  AND status IN ('candidate', 'frozen')
                """,
            ),
            {"pid": ps_id},
        )
        ps_demoted_result = session.execute(
            text(
                """
                UPDATE governance.parameter_sets
                SET status = 'deprecated',
                    deprecated_at = now()
                WHERE family = :family
                  AND timeframe = :tf
                  AND status = 'released'
                  AND parameter_set_id != :pid
                """,
            ),
            {"family": family, "tf": timeframe.lower(), "pid": ps_id},
        )
        ps_released_count = ps_status_result.rowcount or 0
        ps_demoted_count = ps_demoted_result.rowcount or 0
        if ps_released_count != 1:
            raise RuntimeError(
                "locked parameter set lifecycle transition affected "
                f"{ps_released_count} rows instead of 1"
            )
        if ps_released_count or ps_demoted_count:
            log.info(
                "apply_promoted_parameter_set_status family=%s timeframe=%s "
                "parameter_set_id=%s released_transitions=%d demoted_count=%d",
                family, timeframe.lower(), ps_id, ps_released_count, ps_demoted_count,
            )

        canonical_release = None
        if release_id is not None:
            from aats.data_platform.governance.operational_state_db import (
                db_upsert_parameter_release,
            )

            applied_at = datetime.now(timezone.utc).isoformat()
            canonical_release = db_upsert_parameter_release(
                session,
                {
                    "release_id": release_id,
                    "family": family,
                    "timeframe": timeframe.lower(),
                    "combo_key": combo_key,
                    "recommendation_id": recommendation_id,
                    "parameter_set_id": ps_id,
                    # 这是在同一 combo lock 下读取的真实 predecessor；事务外
                    # 创建 pending anchor 时的快照不得覆盖它。
                    "previous_parameter_set_id": from_ps_id,
                    "gate_result_ref": locked_release.get("gate_result_ref"),
                    "apply_result": "success",
                    "observation_status": "observing",
                    "observation_window_hours": locked_release.get(
                        "observation_window_hours"
                    ),
                    "actor": locked_release.get("actor", actor),
                    "notes": locked_release.get("notes", notes),
                    "created_at": locked_release.get("created_at"),
                    "apply_operation_id": op_id,
                    # 观察期只能从资本状态真正生效的时刻起算；pending
                    # release 的 created_at 只是授权锚点，不能冒充 apply 时间。
                    "applied_at": applied_at,
                },
                allow_apply_success_transition=True,
            )
            if not (
                canonical_release.get("release_id") == release_id
                and canonical_release.get("recommendation_id")
                == recommendation_id
                and canonical_release.get("parameter_set_id") == ps_id
                and canonical_release.get("previous_parameter_set_id")
                == from_ps_id
                and canonical_release.get("apply_result") == "success"
                and canonical_release.get("observation_status") == "observing"
                and canonical_release.get("apply_operation_id") == op_id
                and canonical_release.get("applied_at") == applied_at
            ):
                raise RuntimeError(
                    "canonical release apply transition was not persisted"
                )
        # session 退出 with 块时自动 commit

    result["operation_id"] = op_id
    result["from_parameter_set_id"] = from_ps_id
    result["superseded_count"] = superseded_count
    result["ps_released_transitions"] = ps_released_count
    result["ps_demoted_count"] = ps_demoted_count
    if canonical_release is not None:
        result["release"] = canonical_release
    result["message"] = f"已 apply {ps_id} 到 {combo_key}"
    return result


# ── Rollback 操作 ──────────────────────────────────────────────────


def _log_rollback_rejection(
    *,
    family: str,
    timeframe: str,
    target_parameter_set_id: str,
    reason: str,
    actor: str,
) -> None:
    """结构化日志审计：rollback 请求被强校验拒绝。

    设计文档 §2.3 曾建议写入 ``rollback_recommendations`` 表，但该表的
    ``ck_rollback_severity`` 只允许 ``none/medium/high``。为避免在刚落地的
    batch A CHECK 上再打补丁，这里改走 structured log —— Loki/Grafana
    已是既有审计通道，足以检索被拒绝的尝试。
    """
    log.warning(
        "rollback_rejected family=%s timeframe=%s target=%s reason=%s actor=%s",
        family,
        timeframe.lower(),
        target_parameter_set_id,
        reason,
        actor,
    )


def rollback_active_parameter_set(
    project_root: Path,
    *,
    family: str,
    timeframe: str,
    to_parameter_set_id: str | None = None,
    expected_from_parameter_set_id: str | None = None,
    expected_from_recommendation_id: str | None = None,
    expected_previous_parameter_set_id: str | None = None,
    trigger_release_id: str | None = None,
    actor: str = "operator",
    notes: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """回滚 active parameter set 到上一版本.

    A-0.1 收口后语义（见批次 A 详设 §2）：

    - 目标 ``values`` 从 ``governance.parameter_sets`` 表读，不再经过 JSON
      registry —— 消除"写 JSON → 读 JSON → 写 DB"的注入通道。
    - 接受任意 ``to_parameter_set_id`` 之前必须通过
      :func:`validate_rollback_target` 的 6 条校验（存在/状态/归属/历史凭证/
      非自回滚/批准链路），任何一条失败返回 ``code='VALIDATION_FAILED'``。
    - 整个推导 + 校验 + 写入在**单一事务**内完成（包含 ``FOR UPDATE`` 锁），
      确保校验到写入之间没有并发窗口。
    - 未提供 ``to_parameter_set_id`` 时从 history 推导前值；推导失败返回
      ``code='NO_PREVIOUS_TARGET'``。
    - 自动化调用方同时提供 release、recommendation、current set 与 predecessor
      四个 identity；它们会在 combo lock 内与 active row、parameter release 和
      唯一成功 apply history 逐项核对，防止同参数集不同发布、失败发布或旧前驱
      记录触发错误资本回滚。
    - 环境守卫失败返回 ``code='ENVIRONMENT_BLOCKED'``。
    - 任何 rejected 分支都通过 :func:`_log_rollback_rejection` 结构化留痕。

    Returns
    -------
    dict  ``{"ok": bool, "code": str | None, "message": str, ...}``。
    """
    from aats.data_platform.operations.environment_guard import (
        get_current_environment,
        guard_parameter_rollback,
    )

    env = get_current_environment()
    rollback_guard = guard_parameter_rollback(env)
    if not rollback_guard.allowed:
        return {
            "ok": False,
            "code": "ENVIRONMENT_BLOCKED",
            "message": rollback_guard.reason,
            "environment": env,
        }

    combo_key = f"{family}_{timeframe.lower()}"

    from aats.data_platform.db import get_session
    from aats.data_platform.governance.active_params_db import (
        db_append_history,
        db_get_parameter_set_values,
        db_get_previous_set_id,
        db_try_acquire_parameter_apply_lock,
        db_upsert_active_set,
        validate_rollback_target,
    )

    op_id = _make_operation_id()
    release_id_to_transition: str | None = None

    # ── 单一事务：推导 → 校验 → 写入（FOR UPDATE 锁住并发 rollback） ──
    with get_session() as session:
        if not db_try_acquire_parameter_apply_lock(
            session,
            family=family,
            timeframe=timeframe,
        ):
            return {
                "ok": False,
                "code": "parameter_mutation_conflict",
                "message": f"{combo_key} 正有另一个参数状态事务，请稍后重试",
                "combo_key": combo_key,
                "environment": env,
            }

        existing = session.execute(
            text(
                "SELECT parameter_set_id, approval_recommendation_id "
                "FROM governance.active_parameter_sets "
                "WHERE family = :f AND timeframe = :t FOR UPDATE"
            ),
            {"f": family, "t": timeframe.lower()},
        ).fetchone()
        from_ps_id = existing.parameter_set_id if existing else None
        from_recommendation_id = (
            getattr(existing, "approval_recommendation_id", None)
            if existing is not None
            else None
        )

        if not from_ps_id:
            return {
                "ok": False,
                "code": "NO_ACTIVE_SET",
                "message": f"{combo_key} 没有当前 active parameter set",
                "combo_key": combo_key,
            }

        if (
            expected_from_parameter_set_id is not None
            and from_ps_id != expected_from_parameter_set_id
        ):
            reason = "expected_current_parameter_set_mismatch"
            _log_rollback_rejection(
                family=family,
                timeframe=timeframe,
                target_parameter_set_id=to_parameter_set_id or "<derived>",
                reason=reason,
                actor=actor,
            )
            return {
                "ok": False,
                "code": "ACTIVE_SET_CHANGED",
                "reason": reason,
                "message": (
                    f"{combo_key} 当前参数集已从预期的 "
                    f"{expected_from_parameter_set_id} 变为 {from_ps_id}；"
                    "本次回滚未执行"
                ),
                "combo_key": combo_key,
                "expected_from_parameter_set_id": expected_from_parameter_set_id,
                "from_parameter_set_id": from_ps_id,
                "to_parameter_set_id": to_parameter_set_id,
                "environment": env,
            }

        if (
            expected_from_recommendation_id is not None
            and (
                not isinstance(from_recommendation_id, str)
                or not from_recommendation_id.strip()
            )
        ):
            reason = "current_active_recommendation_lineage_missing"
            _log_rollback_rejection(
                family=family,
                timeframe=timeframe,
                target_parameter_set_id=to_parameter_set_id or "<derived>",
                reason=reason,
                actor=actor,
            )
            return {
                "ok": False,
                "code": "RELEASE_LINEAGE_INVALID",
                "reason": reason,
                "message": (
                    f"{combo_key} 当前 active set 缺少 recommendation 血缘；"
                    "已零写入阻断并要求人工 reconciliation"
                ),
                "combo_key": combo_key,
                "expected_from_parameter_set_id": expected_from_parameter_set_id,
                "from_parameter_set_id": from_ps_id,
                "expected_from_recommendation_id": expected_from_recommendation_id,
                "from_recommendation_id": from_recommendation_id,
                "to_parameter_set_id": to_parameter_set_id,
                "environment": env,
            }

        if (
            expected_from_recommendation_id is not None
            and from_recommendation_id != expected_from_recommendation_id
        ):
            reason = "expected_current_recommendation_mismatch"
            _log_rollback_rejection(
                family=family,
                timeframe=timeframe,
                target_parameter_set_id=to_parameter_set_id or "<derived>",
                reason=reason,
                actor=actor,
            )
            return {
                "ok": False,
                "code": "ACTIVE_SET_CHANGED",
                "reason": reason,
                "message": (
                    f"{combo_key} 当前发布血缘已不是预期 recommendation "
                    f"{expected_from_recommendation_id}；本次回滚未执行"
                ),
                "combo_key": combo_key,
                "expected_from_parameter_set_id": expected_from_parameter_set_id,
                "from_parameter_set_id": from_ps_id,
                "expected_from_recommendation_id": expected_from_recommendation_id,
                "from_recommendation_id": from_recommendation_id,
                "to_parameter_set_id": to_parameter_set_id,
                "environment": env,
            }

        # 推导目标（如未指定）—— db_get_previous_set_id 内部已加 FOR UPDATE
        if to_parameter_set_id is None:
            to_parameter_set_id = db_get_previous_set_id(
                session, family, timeframe
            )
            if to_parameter_set_id is None:
                return {
                    "ok": False,
                    "code": "NO_PREVIOUS_TARGET",
                    "message": f"{combo_key} 没有可回滚的历史版本",
                    "combo_key": combo_key,
                }

        if trigger_release_id is not None:
            strict_ids = (
                expected_from_parameter_set_id,
                expected_from_recommendation_id,
                expected_previous_parameter_set_id,
                to_parameter_set_id,
            )
            if any(
                not isinstance(value, str) or not value.strip()
                for value in strict_ids
            ):
                return {
                    "ok": False,
                    "code": "RELEASE_LINEAGE_INVALID",
                    "reason": "automatic_rollback_lineage_identity_missing",
                    "message": "自动回滚缺少完整 release/apply 血缘，已零写入阻断",
                    "combo_key": combo_key,
                    "release_id": trigger_release_id,
                }

            lineage = session.execute(
                text(
                    """
                    SELECT r.release_id, r.family, r.timeframe, r.combo_key,
                           r.recommendation_id, r.parameter_set_id,
                           r.previous_parameter_set_id, r.apply_result,
                           h.lineage_count, h.history_family,
                           h.history_timeframe, h.history_from_parameter_set_id,
                           h.history_to_parameter_set_id
                    FROM governance.parameter_releases AS r
                    LEFT JOIN LATERAL (
                        SELECT COUNT(*) AS lineage_count,
                               MIN(ah.family) AS history_family,
                               MIN(ah.timeframe) AS history_timeframe,
                               MIN(ah.from_parameter_set_id)
                                   AS history_from_parameter_set_id,
                               MIN(ah.to_parameter_set_id)
                                   AS history_to_parameter_set_id
                        FROM governance.parameter_apply_history AS ah
                        WHERE ah.operation_type = 'apply'
                          AND ah.recommendation_id = r.recommendation_id
                    ) AS h ON TRUE
                    WHERE r.release_id = :release_id
                    FOR UPDATE OF r
                    """
                ),
                {"release_id": trigger_release_id},
            ).fetchone()
            requested_family = family.strip().lower()
            requested_timeframe = timeframe.strip().lower()
            requested_combo = f"{requested_family}_{requested_timeframe}"
            lineage_valid = bool(
                lineage is not None
                and str(lineage.apply_result or "") == "success"
                and str(lineage.family or "").strip().lower()
                == requested_family
                and str(lineage.timeframe or "").strip().lower()
                == requested_timeframe
                and str(lineage.combo_key or "").strip().lower()
                == requested_combo
                and lineage.recommendation_id
                == expected_from_recommendation_id
                and lineage.parameter_set_id
                == expected_from_parameter_set_id
                and lineage.previous_parameter_set_id
                == expected_previous_parameter_set_id
                and int(lineage.lineage_count or 0) == 1
                and str(lineage.history_family or "").strip().lower()
                == requested_family
                and str(lineage.history_timeframe or "").strip().lower()
                == requested_timeframe
                and lineage.history_from_parameter_set_id
                == expected_previous_parameter_set_id
                and lineage.history_to_parameter_set_id
                == expected_from_parameter_set_id
                and to_parameter_set_id
                == expected_previous_parameter_set_id
            )
            if not lineage_valid:
                reason = "release_apply_history_lineage_mismatch"
                _log_rollback_rejection(
                    family=family,
                    timeframe=timeframe,
                    target_parameter_set_id=to_parameter_set_id or "<missing>",
                    reason=reason,
                    actor=actor,
                )
                return {
                    "ok": False,
                    "code": "RELEASE_LINEAGE_INVALID",
                    "reason": reason,
                    "message": (
                        "release、active parameter 与成功 apply history 血缘不一致；"
                        "已零写入阻断并要求人工 reconciliation"
                    ),
                    "combo_key": combo_key,
                    "release_id": trigger_release_id,
                    "from_parameter_set_id": from_ps_id,
                    "to_parameter_set_id": to_parameter_set_id,
                }
            release_id_to_transition = trigger_release_id

        elif (
            isinstance(from_recommendation_id, str)
            and from_recommendation_id.strip()
        ):
            current_release = session.execute(
                text(
                    """
                    SELECT COUNT(*) AS release_count,
                           MIN(release_id) AS release_id
                    FROM governance.parameter_releases
                    WHERE recommendation_id = :recommendation_id
                      AND parameter_set_id = :parameter_set_id
                      AND apply_result = 'success'
                      AND lower(btrim(family)) = lower(btrim(:family))
                      AND lower(btrim(timeframe)) = lower(btrim(:timeframe))
                    """
                ),
                {
                    "recommendation_id": from_recommendation_id,
                    "parameter_set_id": from_ps_id,
                    "family": family,
                    "timeframe": timeframe,
                },
            ).fetchone()
            release_count = int(
                getattr(current_release, "release_count", 0) or 0
            )
            if release_count > 1:
                return {
                    "ok": False,
                    "code": "RELEASE_LINEAGE_INVALID",
                    "reason": "current_release_lineage_ambiguous",
                    "message": "当前 active recommendation 对应多个成功 release",
                    "combo_key": combo_key,
                    "from_parameter_set_id": from_ps_id,
                    "from_recommendation_id": from_recommendation_id,
                }
            if release_count == 1:
                release_id_to_transition = str(current_release.release_id)

        # 自回滚短路（也会被规则 5 捕获，但此处早一点返回更友好）
        if to_parameter_set_id == from_ps_id:
            _log_rollback_rejection(
                family=family,
                timeframe=timeframe,
                target_parameter_set_id=to_parameter_set_id,
                reason="target_is_currently_active",
                actor=actor,
            )
            return {
                "ok": False,
                "code": "VALIDATION_FAILED",
                "reason": "target_is_currently_active",
                "message": f"回滚目标与当前版本相同: {from_ps_id}",
                "combo_key": combo_key,
                "from_parameter_set_id": from_ps_id,
                "to_parameter_set_id": to_parameter_set_id,
            }

        # 6 条强校验 —— 失败即短路，不碰 active 表
        ok, reason = validate_rollback_target(
            session, family, timeframe, to_parameter_set_id
        )
        if not ok:
            _log_rollback_rejection(
                family=family,
                timeframe=timeframe,
                target_parameter_set_id=to_parameter_set_id,
                reason=reason,
                actor=actor,
            )
            return {
                "ok": False,
                "code": "VALIDATION_FAILED",
                "reason": reason,
                "message": f"rollback 目标校验失败: {reason}",
                "combo_key": combo_key,
                "from_parameter_set_id": from_ps_id,
                "to_parameter_set_id": to_parameter_set_id,
            }

        # 目标 values 直接从 DB 读，绕开 JSON registry
        target = db_get_parameter_set_values(
            session,
            to_parameter_set_id,
            family=family,
            timeframe=timeframe,
        )
        if target is None:
            # 理论上不会走到：validate_rollback_target 已证明 target 存在
            _log_rollback_rejection(
                family=family,
                timeframe=timeframe,
                target_parameter_set_id=to_parameter_set_id,
                reason="target_values_missing",
                actor=actor,
            )
            return {
                "ok": False,
                "code": "VALIDATION_FAILED",
                "reason": "target_values_missing",
                "message": "target parameter_set 存在但 values 读取失败",
                "combo_key": combo_key,
            }

        values = target["values"]
        result: dict[str, Any] = {
            "ok": True,
            "operation_type": "rollback",
            "combo_key": combo_key,
            "family": family,
            "timeframe": timeframe,
            "from_parameter_set_id": from_ps_id,
            "from_recommendation_id": from_recommendation_id,
            "to_parameter_set_id": to_parameter_set_id,
            "values": values,
            "environment": env,
        }

        if dry_run:
            result["message"] = (
                f"[DRY RUN] 将 rollback {combo_key}: "
                f"{from_ps_id} → {to_parameter_set_id}"
            )
            # dry_run 也退出事务，锁随 session 关闭自动释放
            return result

        db_upsert_active_set(
            session,
            family=family,
            timeframe=timeframe,
            parameter_set_id=to_parameter_set_id,
            values=values,
            source_round_id=target.get("source_round_id"),
            approval_recommendation_id=target.get("approval_recommendation_id"),
            applied_by=f"rdp_rollback ({actor})",
        )
        db_append_history(
            session,
            operation_id=op_id,
            operation_type="rollback",
            family=family,
            timeframe=timeframe,
            from_parameter_set_id=from_ps_id,
            to_parameter_set_id=to_parameter_set_id,
            actor=actor,
            notes=notes or f"Rollback from {from_ps_id}",
        )
        rollback_promote = session.execute(
            text(
                """
                UPDATE governance.parameter_sets
                SET status = 'released',
                    frozen_at = COALESCE(frozen_at, now()),
                    deprecated_at = NULL
                WHERE parameter_set_id = :target
                  AND family = :family
                  AND timeframe = :timeframe
                  AND status IN ('candidate', 'frozen', 'deprecated')
                """
            ),
            {
                "target": to_parameter_set_id,
                "family": family,
                "timeframe": timeframe.lower(),
            },
        )
        rollback_demote = session.execute(
            text(
                """
                UPDATE governance.parameter_sets
                SET status = 'deprecated', deprecated_at = now()
                WHERE parameter_set_id = :current
                  AND family = :family
                  AND timeframe = :timeframe
                  AND status = 'released'
                """
            ),
            {
                "current": from_ps_id,
                "family": family,
                "timeframe": timeframe.lower(),
            },
        )
        if (rollback_promote.rowcount or 0) != 1 or (
            rollback_demote.rowcount or 0
        ) != 1:
            raise RuntimeError(
                "rollback parameter-set lifecycle transition was not atomic"
            )
        if release_id_to_transition is not None:
            # 自动回滚的资本变更、apply-history 与 release 终态必须在同一
            # transaction 内提交。release 不能留到提交后再 best-effort 写，
            # 否则 active 已回滚而 canonical release 仍 observing。
            from aats.data_platform.governance.operational_state_db import (
                db_upsert_parameter_release,
            )

            rolled_back_at = datetime.now(timezone.utc).isoformat()
            canonical_release = db_upsert_parameter_release(
                session,
                {
                    "release_id": release_id_to_transition,
                    "family": family,
                    "timeframe": timeframe,
                    "combo_key": combo_key,
                    "recommendation_id": from_recommendation_id,
                    "parameter_set_id": from_ps_id,
                    "previous_parameter_set_id": expected_previous_parameter_set_id,
                    "apply_result": "success",
                    "observation_status": "rolled_back",
                    "rolled_back_at": rolled_back_at,
                    "rollback_to_parameter_set_id": to_parameter_set_id,
                    "rollback_operation_id": op_id,
                },
                allow_rollback_transition=True,
            )
            if (
                canonical_release.get("observation_status") != "rolled_back"
                or canonical_release.get("rollback_to_parameter_set_id")
                != to_parameter_set_id
                or canonical_release.get("rollback_operation_id") != op_id
            ):
                raise RuntimeError(
                    "canonical release rollback transition was not persisted"
                )
        # session 退出 with 自动 commit，FOR UPDATE 锁同时释放

    if release_id_to_transition is not None:
        result["release_id"] = release_id_to_transition

    # ── 文件审计副本（best-effort；canonical DB 已在资本事务内收口） ──
    try:
        from aats.data_platform.production_workflow.release_registry import (
            load_release_history,
            mark_release_rolled_back,
            save_release_record,
        )

        release_history = load_release_history(project_root)
        rolled_back_release = None
        for release in reversed(release_history.get("releases", [])):
            if release_id_to_transition is None:
                break
            if release.get("release_id") != release_id_to_transition:
                continue
            if release.get("family") != family:
                continue
            if str(release.get("timeframe") or "").lower() != timeframe.lower():
                continue
            if release.get("parameter_set_id") != from_ps_id:
                continue
            if release.get("recommendation_id") != from_recommendation_id:
                continue
            if release.get("apply_result") != "success":
                continue
            rolled_back_release = mark_release_rolled_back(
                release_history,
                str(release.get("release_id")),
                rollback_to_parameter_set_id=to_parameter_set_id,
                rollback_operation_id=op_id,
            )
            break
        if rolled_back_release is not None:
            save_release_record(rolled_back_release, project_root)
            result["release_id"] = rolled_back_release.get("release_id")
    except Exception as exc:
        log.warning(
            "rollback 后同步 release history 失败 (%s)",
            type(exc).__name__,
        )

    result["operation_id"] = op_id
    result["message"] = (
        f"已 rollback {combo_key}: {from_ps_id} → {to_parameter_set_id}"
    )
    return result


# ── 清除 active parameter set ──────────────────────────────────────


def clear_active_parameter_set(
    project_root: Path,
    *,
    family: str,
    timeframe: str,
    actor: str = "operator",
    notes: str | None = None,
) -> dict[str, Any]:
    """清除指定 combo 的 active parameter set（回退到 profile 默认值）."""
    combo_key = f"{family}_{timeframe.lower()}"
    op_id = None

    from aats.data_platform.db import get_session
    from aats.data_platform.governance.active_params_db import (
        db_append_history,
        db_clear_active_set,
        db_try_acquire_parameter_apply_lock,
    )

    with get_session() as session:
        if not db_try_acquire_parameter_apply_lock(
            session,
            family=family,
            timeframe=timeframe,
        ):
            return {
                "ok": False,
                "code": "parameter_mutation_conflict",
                "message": f"{combo_key} 正有另一个参数状态事务，请稍后重试",
                "combo_key": combo_key,
            }

        existing = session.execute(
            text(
                "SELECT parameter_set_id FROM governance.active_parameter_sets "
                "WHERE family = :f AND timeframe = :t FOR UPDATE"
            ),
            {"f": family, "t": timeframe.lower()},
        ).fetchone()
        from_ps_id = existing.parameter_set_id if existing else None

        db_clear_active_set(session, family, timeframe)

        if from_ps_id:
            op_id = _make_operation_id()
            db_append_history(
                session,
                operation_id=op_id,
                operation_type="clear",
                family=family,
                timeframe=timeframe,
                from_parameter_set_id=from_ps_id,
                actor=actor,
                notes=notes or f"Cleared {combo_key}",
            )

    result: dict[str, Any] = {"ok": True, "combo_key": combo_key, "message": f"已清除 {combo_key}"}
    if op_id:
        result["operation_id"] = op_id
    return result
