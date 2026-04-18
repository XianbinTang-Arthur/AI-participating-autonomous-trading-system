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
from datetime import datetime, timezone
from pathlib import Path

from aats.data_platform.governance._time_util import parse_iso_datetime_utc
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import try_governance_db

log = logging.getLogger(__name__)


# ── 路径常量 ───────────────────────────────────────────────────────

PARAMETER_APPLY_HISTORY_FILENAME = "parameter_apply_history.json"
DECISION_SYSTEM_DIR = "artifacts/decision_system"
GOVERNANCE_DIR = "artifacts/governance"


def _make_operation_id() -> str:
    return f"op_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


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
            log.warning("无法从 DB 加载 apply history: %s", exc)
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
            log.warning("apply history DB 同步失败: %s", exc)
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

    env = get_current_environment()
    apply_guard = guard_parameter_apply(env)
    if not apply_guard.allowed:
        return {"ok": False, "message": apply_guard.reason, "environment": env}

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

    if policy["require_approval"] and not (
        rec.get("approved_by") or rec.get("approved_at")
    ):
        return {
            "ok": False,
            "message": f"{env} environment requires recorded approval metadata before apply",
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
        if not gate_result.get("allow_apply"):
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
    gov_registry = load_registry(gov_reg_path)

    target_ps = None
    for ps in gov_registry.get("parameter_sets", []):
        if ps["parameter_set_id"] == ps_id:
            target_ps = ps
            break

    if target_ps is None:
        return {"ok": False, "message": f"parameter_registry 中未找到 {ps_id}"}

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
        db_upsert_active_set,
    )

    with get_session() as session:
        # 查当前 active（用于 from_parameter_set_id）
        existing = session.execute(
            text("SELECT parameter_set_id FROM governance.active_parameter_sets WHERE family = :f AND timeframe = :t"),
            {"f": family, "t": timeframe.lower()},
        ).fetchone()
        from_ps_id = existing.parameter_set_id if existing else None

        db_upsert_active_set(
            session,
            family=family,
            timeframe=timeframe,
            parameter_set_id=ps_id,
            values=values,
            source_round_id=target_ps.get("source_round_id"),
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
                  AND status != 'released'
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
        if ps_released_count or ps_demoted_count:
            log.info(
                "apply_promoted_parameter_set_status family=%s timeframe=%s "
                "parameter_set_id=%s released_transitions=%d demoted_count=%d",
                family, timeframe.lower(), ps_id, ps_released_count, ps_demoted_count,
            )
        # session 退出 with 块时自动 commit

    result["operation_id"] = op_id
    result["from_parameter_set_id"] = from_ps_id
    result["superseded_count"] = superseded_count
    result["ps_released_transitions"] = ps_released_count
    result["ps_demoted_count"] = ps_demoted_count
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
        db_upsert_active_set,
        validate_rollback_target,
    )

    op_id = _make_operation_id()

    # ── 单一事务：推导 → 校验 → 写入（FOR UPDATE 锁住并发 rollback） ──
    with get_session() as session:
        existing = session.execute(
            text(
                "SELECT parameter_set_id FROM governance.active_parameter_sets "
                "WHERE family = :f AND timeframe = :t FOR UPDATE"
            ),
            {"f": family, "t": timeframe.lower()},
        ).fetchone()
        from_ps_id = existing.parameter_set_id if existing else None

        if not from_ps_id:
            return {
                "ok": False,
                "code": "NO_ACTIVE_SET",
                "message": f"{combo_key} 没有当前 active parameter set",
                "combo_key": combo_key,
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
        # session 退出 with 自动 commit，FOR UPDATE 锁同时释放

    # ── 文件审计副本（best-effort，失败仅 warn，不回滚 DB） ──
    try:
        from aats.data_platform.production_workflow.release_registry import (
            load_release_history,
            mark_release_rolled_back,
            save_release_history,
        )

        release_history = load_release_history(project_root)
        rolled_back_release = None
        for release in reversed(release_history.get("releases", [])):
            if release.get("family") != family:
                continue
            if str(release.get("timeframe") or "").lower() != timeframe.lower():
                continue
            if release.get("parameter_set_id") != from_ps_id:
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
            save_release_history(release_history, project_root)
            result["release_id"] = rolled_back_release.get("release_id")
    except Exception as exc:
        log.warning("rollback 后同步 release history 失败: %s", exc)

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
    )

    with get_session() as session:
        existing = session.execute(
            text("SELECT parameter_set_id FROM governance.active_parameter_sets WHERE family = :f AND timeframe = :t"),
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
