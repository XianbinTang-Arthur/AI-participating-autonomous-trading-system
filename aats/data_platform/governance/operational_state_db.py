"""DB helpers for RDP operational state.

These tables back the operational control plane state that used to be
stored only in artifact JSON files. Callers should keep their existing
registry-like payload shapes; this module only persists and restores them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import ADVISORY_LOCK_KEYS, json_dumps, parse_dt

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
                workflow, report, started_at, finished_at
            FROM governance.workflow_run_reports
            ORDER BY workflow, finished_at DESC NULLS LAST, started_at DESC NULLS LAST
            """
        ),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _with_payload(
            row.report,
            workflow=row.workflow,
            started_at=row.started_at.isoformat() if row.started_at else None,
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
        )
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
            SELECT workflow, report, started_at, finished_at
            FROM governance.workflow_run_reports
            {where_sql}
            ORDER BY COALESCE(started_at, finished_at, created_at) DESC
            {limit_sql}
            """
        ),
        params,
    ).fetchall()
    return [
        _with_payload(
            row.report,
            workflow=row.workflow,
            started_at=row.started_at.isoformat() if row.started_at else None,
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
        )
        for row in rows
    ]


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
            SELECT payload, created_at
            FROM governance.pre_apply_gate_results
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).fetchall()
    return [
        _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)
        for row in rows
    ]


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
            SELECT payload, created_at
            FROM governance.pre_apply_gate_results
            WHERE gate_run_id = :gate_run_id
            """
        ),
        {"gate_run_id": gate_run_id},
    ).fetchone()
    if row is None:
        return None
    return _with_payload(
        row.payload,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


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
            SELECT payload, created_at
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
    return _with_payload(
        row.payload,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


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
            SELECT payload, created_at
            FROM governance.pre_apply_gate_results
            WHERE recommendation_id = :rec_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"rec_id": recommendation_id, "limit": limit},
    ).fetchall()
    return [
        _with_payload(
            row.payload,
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in rows
    ]


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
            SELECT payload, created_at
            FROM governance.pre_apply_gate_results
            WHERE release_id = :release_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"release_id": release_id, "limit": limit},
    ).fetchall()
    return [
        _with_payload(
            row.payload,
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in rows
    ]


def db_upsert_parameter_release(session: Session, release: dict[str, Any]) -> None:
    # M5：column 与 payload JSON 必须归一到同一份 timeframe / combo_key。
    # 统一走 ``_normalize_combo_fields`` 与 observation_results /
    # rollback_recommendations / release_effectiveness 三张表共享同一份归一化路径。
    timeframe_norm, combo_key_norm, release_for_payload = _normalize_combo_fields(release)
    family_norm = str(release.get("family") or "")

    session.execute(
        text(
            """
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
            ON CONFLICT (release_id) DO UPDATE SET
                family = EXCLUDED.family,
                timeframe = EXCLUDED.timeframe,
                combo_key = EXCLUDED.combo_key,
                recommendation_id = EXCLUDED.recommendation_id,
                parameter_set_id = EXCLUDED.parameter_set_id,
                previous_parameter_set_id = EXCLUDED.previous_parameter_set_id,
                actor = EXCLUDED.actor,
                gate_result_ref = EXCLUDED.gate_result_ref,
                gate_status = EXCLUDED.gate_status,
                apply_result = EXCLUDED.apply_result,
                observation_status = EXCLUDED.observation_status,
                observation_window_hours = EXCLUDED.observation_window_hours,
                notes = EXCLUDED.notes,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "release_id": release.get("release_id"),
            "family": family_norm or release.get("family"),
            "timeframe": timeframe_norm,
            "combo_key": combo_key_norm or release.get("combo_key"),
            "recommendation_id": release.get("recommendation_id"),
            "parameter_set_id": release.get("parameter_set_id"),
            "previous_parameter_set_id": release.get("previous_parameter_set_id"),
            "actor": release.get("actor", "operator"),
            "gate_result_ref": release.get("gate_result_ref"),
            "gate_status": release.get("gate_status"),
            "apply_result": release.get("apply_result", "pending"),
            "observation_status": release.get("observation_status", "pending"),
            "observation_window_hours": int(release.get("observation_window_hours") or 24),
            "notes": release.get("notes"),
            "payload": json_dumps(release_for_payload),
            "created_at": parse_dt(release.get("created_at")) or _utcnow(),
            "updated_at": _utcnow(),
        },
    )


def db_load_release_history(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT payload, created_at
            FROM governance.parameter_releases
            ORDER BY created_at ASC
            """
        ),
    ).fetchall()
    return {
        "generated_at": _utcnow().isoformat(),
        "releases": [
            _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)
            for row in rows
        ],
    }


def db_find_parameter_release(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload, created_at
            FROM governance.parameter_releases
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)


def db_update_parameter_release_status(
    session: Session,
    release_id: str,
    *,
    apply_result: str | None = None,
    observation_status: str | None = None,
) -> bool:
    """按 release_id 更新 apply_result / observation_status，并同步 payload JSON。

    M6：历史上这里跑 SELECT → Python 拼 → 走 ``db_upsert_parameter_release``
    的 INSERT ... ON CONFLICT，等于"读 + 整行重写 + 重算所有 EXCLUDED 字段"，
    2 次往返 + 一次全列覆盖。现在改用单条 UPDATE ... RETURNING：
      * 仅更新需要变的列 + payload JSON（用 jsonb_set 就地打 patch）
      * RETURNING release_id 让我们直接拿到"行是否存在"的信号
      * 一次往返，事务也更短
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

    # jsonb_set 是原子 patch：即使 apply_result / observation_status 并发被其他
    # 写路径覆盖，也不会整行回滚——我们只修两把 key，其余字段原样保留。
    # 使用两层 jsonb_set（COALESCE 兼容旧行 payload 为 NULL 的边界情况）。
    sql = """
        UPDATE governance.parameter_releases
           SET apply_result = COALESCE(:apply_result, apply_result),
               observation_status = COALESCE(:observation_status, observation_status),
               payload = jsonb_set(
                   jsonb_set(
                       COALESCE(payload, '{}'::jsonb),
                       '{apply_result}',
                       to_jsonb(COALESCE(:apply_result, payload->>'apply_result')),
                       true
                   ),
                   '{observation_status}',
                   to_jsonb(COALESCE(:observation_status, payload->>'observation_status')),
                   true
               ),
               updated_at = :updated_at
         WHERE release_id = :release_id
         RETURNING release_id
    """
    row = session.execute(
        text(sql),
        {
            "release_id": release_id,
            "apply_result": apply_result,
            "observation_status": observation_status,
            "updated_at": _utcnow(),
        },
    ).fetchone()
    return row is not None


def db_get_latest_release_for_combo(
    session: Session,
    *,
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload, created_at
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
    return _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)


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
            """
        ),
        {
            "release_id": result.get("release_id"),
            "family": result.get("family"),
            "timeframe": timeframe_norm,
            "combo_key": combo_key_norm or result.get("combo_key"),
            "status": status_val,
            "recommendation": result.get("recommendation", "review"),
            "observation_window_hours": int(result.get("observation_window_hours") or 24),
            "window_active": bool(result.get("window_active")),
            "started_at": parse_dt(result.get("started_at")),
            "evaluated_at": parse_dt(result.get("evaluated_at")) or _utcnow(),
            "payload": json_dumps(result_for_payload),
            "updated_at": _utcnow(),
        },
    )


def db_get_observation_result(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload, evaluated_at
            FROM governance.observation_results
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _with_payload(row.payload, evaluated_at=row.evaluated_at.isoformat() if row.evaluated_at else None)


def db_list_observation_results(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT payload, evaluated_at
            FROM governance.observation_results
            ORDER BY evaluated_at DESC
            """
        ),
    ).fetchall()
    return [
        _with_payload(row.payload, evaluated_at=row.evaluated_at.isoformat() if row.evaluated_at else None)
        for row in rows
    ]


def db_upsert_rollback_recommendation(session: Session, result: dict[str, Any]) -> None:
    # M5 归一：同 observation_results 的修复动机，避免 column 与 payload 的
    # timeframe / combo_key 大小写不一致。
    timeframe_norm, combo_key_norm, result_for_payload = _normalize_combo_fields(result)
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
            """
        ),
        {
            "release_id": result.get("release_id"),
            "family": result.get("family"),
            "timeframe": timeframe_norm,
            "combo_key": combo_key_norm or result.get("combo_key"),
            "rollback_recommended": bool(result.get("rollback_recommended")),
            "severity": result.get("severity", "none"),
            "suggested_target_parameter_set_id": result.get("suggested_target_parameter_set_id"),
            "evaluated_at": parse_dt(result.get("evaluated_at")) or _utcnow(),
            "payload": json_dumps(result_for_payload),
            "updated_at": _utcnow(),
        },
    )


def db_get_rollback_recommendation(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload, evaluated_at
            FROM governance.rollback_recommendations
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _with_payload(row.payload, evaluated_at=row.evaluated_at.isoformat() if row.evaluated_at else None)


def db_upsert_release_effectiveness(session: Session, evaluation: dict[str, Any]) -> None:
    # M5 归一：该表列里没有 combo_key，但 payload 归一仍有价值——下游读 JSON
    # 时会拿到一致的 timeframe。_normalize_combo_fields 会同时计算 combo_key，
    # 这里只使用 timeframe 部分。
    timeframe_norm, _combo_key_norm, eval_for_payload = _normalize_combo_fields(evaluation)
    session.execute(
        text(
            """
            INSERT INTO governance.release_effectiveness
                (evaluation_id, release_id, family, timeframe, conclusion,
                 evaluated_at, payload, updated_at)
            VALUES
                (:evaluation_id, :release_id, :family, :timeframe, :conclusion,
                 :evaluated_at, CAST(:payload AS jsonb), :updated_at)
            ON CONFLICT (release_id) DO UPDATE SET
                evaluation_id = EXCLUDED.evaluation_id,
                family = EXCLUDED.family,
                timeframe = EXCLUDED.timeframe,
                conclusion = EXCLUDED.conclusion,
                evaluated_at = EXCLUDED.evaluated_at,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "evaluation_id": evaluation.get("evaluation_id"),
            "release_id": evaluation.get("release_id"),
            "family": evaluation.get("family"),
            "timeframe": timeframe_norm or None,
            "conclusion": evaluation.get("conclusion", "unknown"),
            "evaluated_at": parse_dt(evaluation.get("evaluated_at")) or _utcnow(),
            "payload": json_dumps(eval_for_payload),
            "updated_at": _utcnow(),
        },
    )


def db_load_effectiveness_registry(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT payload, evaluated_at
            FROM governance.release_effectiveness
            ORDER BY evaluated_at ASC
            """
        ),
    ).fetchall()
    return {
        "generated_at": _utcnow().isoformat(),
        "evaluations": [
            _with_payload(row.payload, evaluated_at=row.evaluated_at.isoformat() if row.evaluated_at else None)
            for row in rows
        ],
    }


def db_find_release_effectiveness(session: Session, release_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload, evaluated_at
            FROM governance.release_effectiveness
            WHERE release_id = :release_id
            """
        ),
        {"release_id": release_id},
    ).fetchone()
    if row is None:
        return None
    return _with_payload(row.payload, evaluated_at=row.evaluated_at.isoformat() if row.evaluated_at else None)


def db_upsert_decision_evidence_bundle(session: Session, entry: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO governance.decision_evidence_bundles
                (round_id, evidence_summary_path, phases_with_data, completeness_ratio,
                 payload, updated_at)
            VALUES
                (:round_id, :evidence_summary_path, CAST(:phases_with_data AS jsonb), :completeness_ratio,
                 CAST(:payload AS jsonb), :updated_at)
            ON CONFLICT (round_id) DO UPDATE SET
                evidence_summary_path = EXCLUDED.evidence_summary_path,
                phases_with_data = EXCLUDED.phases_with_data,
                completeness_ratio = EXCLUDED.completeness_ratio,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "round_id": entry.get("round_id"),
            "evidence_summary_path": entry.get("evidence_summary_path"),
            "phases_with_data": json_dumps(entry.get("phases_with_data") or []),
            "completeness_ratio": float(entry.get("completeness_ratio") or 0.0),
            "payload": json_dumps(entry),
            "updated_at": _utcnow(),
        },
    )


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

