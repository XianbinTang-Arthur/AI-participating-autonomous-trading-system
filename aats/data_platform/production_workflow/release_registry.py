"""Parameter Release Record 管理.

工作包 B: 让每次 parameter apply 成为可追踪、可审计的 release 事件。

release record 至少记录:
  - release_id
  - created_at
  - family / timeframe
  - recommendation_id
  - parameter_set_id
  - actor
  - gate_result_ref
  - apply_result
  - previous_parameter_set_id
  - notes
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import try_governance_db
from aats.data_platform.governance._exceptions import (
    DBConstraintViolation,
    DBUnavailableError,
)

log = logging.getLogger(__name__)

_RELEASE_HISTORY_PATH = "artifacts/production_workflow/parameter_release_history.json"


def _make_release_id() -> str:
    return f"rel_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


# ── 加载 / 保存 ───────────────────────────────────────────────────


def load_release_history(project_root: Path) -> dict[str, Any]:
    """加载 release history。

    返回体附带:
      - ``source``: ``"db"`` / ``"json"`` / ``"empty"``
      - ``stale``: ``True`` 表示数据来自 JSON 副本（DB 不可达或读取失败），
        消费方需把这个信号透传到 UI / 监控，避免运营者误把 stale JSON 当成真源。

    真源是 governance DB；JSON 副本仅作单机兼容 / DB 抖动时的 last-known 副本。
    DB 读成功即返回（即使 releases 为空），不做"空就回退 JSON"的兜底——否则
    旧 JSON 会把已淘汰的 release 重新注入运行链。
    """
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_load_release_history,
            )

            with Session(engine) as session:
                history = db_load_release_history(session)
            if not isinstance(history, dict):
                history = {"generated_at": None, "releases": []}
            history.setdefault("releases", [])
            history["source"] = "db"
            history["stale"] = False
            return history
        except Exception as exc:
            log.warning(
                "release history: DB 读取失败 (%s)，退化到文件（stale 风险）", exc,
            )
            stale_reason = f"db_read_error: {exc}"
        finally:
            if engine is not None:
                engine.dispose()
    else:
        stale_reason = "db_unreachable"

    path = project_root / _RELEASE_HISTORY_PATH
    if not path.exists():
        return {
            "generated_at": None,
            "releases": [],
            "source": "empty",
            "stale": False,
        }
    try:
        with path.open(encoding="utf-8") as f:
            history = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("无法加载 release history: %s", exc)
        return {
            "generated_at": None,
            "releases": [],
            "source": "empty",
            "stale": True,
            "stale_reason": f"json_read_error: {exc}",
        }
    if not isinstance(history, dict):
        history = {"generated_at": None, "releases": []}
    history.setdefault("releases", [])
    history["source"] = "json"
    history["stale"] = True
    history["stale_reason"] = stale_reason
    return history


def save_release_history(
    history: dict[str, Any], project_root: Path,
) -> Path:
    """持久化 release history 到 DB + JSON 副本。

    A-0.3 硬纪律：DB 是真源，JSON 只是审计副本。DB 不可达时统一抛
    :class:`DBUnavailableError`，不允许"仅写 JSON 假装成功"——否则 loader
    下次 fallback 到 JSON 时会把未成功入真源的写入重新注入系统（上一次
    split-brain 事故的根因）。之前的 ``AATS_P0_RELEASE_FAIL_LOUD`` 环境
    开关已废除，所有调用方无条件走 fail-loud 路径。
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    from aats.data_platform.governance._atomic_io import atomic_json_write

    path = project_root / _RELEASE_HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    history["generated_at"] = datetime.now(timezone.utc).isoformat()

    # 顺序：DB 先、文件后。若 DB 写失败，文件保持旧状态，避免留下
    # "DB 未 commit、但文件已更新"的 ghost —— 否则 DB 暂时不可达时 loader
    # 会回落到这份从未成功入真源的文件，把失败写入重新注入系统。
    engine, ok = try_governance_db()
    if not ok:
        raise DBUnavailableError(
            "governance DB unavailable while saving release history; "
            "refusing to write JSON-only fallback to prevent split-brain"
        )
    try:
        from aats.data_platform.governance.operational_state_db import (
            db_set_gate_result_release_id,
            db_upsert_parameter_release,
        )

        with Session(engine) as session, session.begin():
            for release in history.get("releases", []):
                if not isinstance(release, dict):
                    continue
                db_upsert_parameter_release(session, release)
                # 把 release_id 回填到 gate 行，让 db_list_gate_results_for_release
                # 能在 release_id 索引上直接命中，而不是回落到 parameter_releases JOIN。
                # 同事务内做，确保 release upsert 与 gate 回填一起 commit / 一起回滚。
                gate_ref = release.get("gate_result_ref")
                rel_id = release.get("release_id")
                if gate_ref and rel_id:
                    matched = db_set_gate_result_release_id(
                        session,
                        gate_run_id=gate_ref,
                        release_id=rel_id,
                    )
                    if not matched:
                        # gate 行缺失不该阻塞 release 落库（历史 release、单机
                        # 模式等场景下 gate 表可能没写入），只打 warning 让运维可见。
                        log.warning(
                            "release %s 回填 gate_run_id=%s 未命中任何行",
                            rel_id,
                            gate_ref,
                        )
    except IntegrityError as exc:
        log.exception("release history DB 约束违反")
        raise DBConstraintViolation(
            f"release history violated DB constraint: {exc}"
        ) from exc
    except OperationalError as exc:
        log.exception("release history DB 运行时异常")
        raise DBUnavailableError(
            f"DB operational error while saving release history: {exc}"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()

    # DB 写成功后才写文件副本
    atomic_json_write(history, path)
    log.info("已保存 release history: %d releases", len(history.get("releases", [])))
    return path


# ── Release 创建 ──────────────────────────────────────────────────


def create_release_record(
    *,
    family: str,
    timeframe: str,
    recommendation_id: str,
    parameter_set_id: str,
    actor: str = "operator",
    gate_result_ref: str | None = None,
    gate_status: str | None = None,
    previous_parameter_set_id: str | None = None,
    observation_window_hours: int = 24,
    notes: str | None = None,
) -> dict[str, Any]:
    """创建一条 release record."""
    return {
        "release_id": _make_release_id(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "family": family,
        "timeframe": timeframe,
        "combo_key": f"{family}_{timeframe.lower()}",
        "recommendation_id": recommendation_id,
        "parameter_set_id": parameter_set_id,
        "previous_parameter_set_id": previous_parameter_set_id,
        "actor": actor,
        "gate_result_ref": gate_result_ref,
        "gate_status": gate_status,
        "apply_result": "pending",
        "observation_status": "pending",
        "observation_window_hours": observation_window_hours,
        "notes": notes,
    }


def add_release(
    history: dict[str, Any], release: dict[str, Any],
) -> None:
    history.setdefault("releases", []).append(release)


def find_release(
    history: dict[str, Any], release_id: str,
) -> dict[str, Any] | None:
    for rel in history.get("releases", []):
        if rel.get("release_id") == release_id:
            return rel
    return None


def update_release_status(
    history: dict[str, Any],
    release_id: str,
    *,
    apply_result: str | None = None,
    observation_status: str | None = None,
) -> dict[str, Any] | None:
    """更新 release 的 apply_result 或 observation_status."""
    rel = find_release(history, release_id)
    if rel is None:
        return None
    if apply_result:
        rel["apply_result"] = apply_result
    if observation_status:
        rel["observation_status"] = observation_status
    return rel


def mark_release_rolled_back(
    history: dict[str, Any],
    release_id: str,
    *,
    rollback_to_parameter_set_id: str,
    rollback_operation_id: str | None = None,
    rolled_back_at: str | None = None,
) -> dict[str, Any] | None:
    """标记 release 已被回滚，避免后续仍被当成 observing release。"""
    rel = find_release(history, release_id)
    if rel is None:
        return None
    rel["observation_status"] = "rolled_back"
    rel["rolled_back_at"] = rolled_back_at or datetime.now(timezone.utc).isoformat()
    rel["rollback_to_parameter_set_id"] = rollback_to_parameter_set_id
    if rollback_operation_id:
        rel["rollback_operation_id"] = rollback_operation_id
    return rel


def get_latest_release_for_combo(
    history: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """获取指定 combo 最近的 release."""
    combo_key = f"{family}_{timeframe.lower()}"
    for rel in reversed(history.get("releases", [])):
        if rel.get("combo_key") == combo_key:
            return rel
    return None


# ── 完整 Release 流程 ─────────────────────────────────────────────


def create_parameter_release(
    project_root: Path,
    *,
    recommendation_id: str,
    actor: str = "operator",
    observation_window_hours: int = 24,
    notes: str | None = None,
    run_gate: bool = True,
    run_apply: bool = True,
) -> dict[str, Any]:
    """完整的 parameter release 流程.

    流程:
      1. 读取 approved recommendation
      2. 运行 pre-apply gate（可选）
      3. 生成 release record
      4. 调用 apply 逻辑（可选）
      5. 写入 release history

    Returns
    -------
    dict  {"ok": bool, "release": dict, "gate_result": dict, "apply_result": dict}
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        find_recommendation,
        load_recommendation_registry,
    )
    from aats.data_platform.governance.parameter_registry import load_registry
    from aats.data_platform.operations.environment_guard import (
        guard_release_creation,
    )

    result: dict[str, Any] = {"ok": False}
    release_guard = guard_release_creation(
        run_gate=run_gate,
        run_apply=run_apply,
        observation_window_hours=observation_window_hours,
    )
    if not release_guard.allowed:
        result["message"] = release_guard.reason
        result["environment"] = release_guard.environment
        result["release_guard"] = {
            "environment": release_guard.environment,
            "requested_observation_window_hours": (
                release_guard.requested_observation_window_hours
            ),
            "resolved_observation_window_hours": (
                release_guard.resolved_observation_window_hours
            ),
            "run_gate": release_guard.run_gate,
            "run_apply": release_guard.run_apply,
        }
        return result

    observation_window_hours = release_guard.resolved_observation_window_hours

    # 1. 加载 recommendation
    rec_path = project_root / "artifacts/decision_system/recommendation_registry.json"
    rec_reg = load_recommendation_registry(rec_path)
    rec = find_recommendation(rec_reg, recommendation_id)

    if rec is None:
        result["message"] = f"未找到 recommendation: {recommendation_id}"
        return result

    if rec["status"] != "approved":
        result["message"] = f"recommendation 状态为 '{rec['status']}'，必须为 approved"
        return result

    ps_id = rec.get("target_parameter_set_id")
    if not ps_id:
        result["message"] = "recommendation 无 target_parameter_set_id"
        return result

    # 获取 parameter set 信息
    gov_path = project_root / "artifacts/governance/current_parameter_registry.json"
    gov_reg = load_registry(gov_path)
    target_ps = None
    for ps in gov_reg.get("parameter_sets", []):
        if ps["parameter_set_id"] == ps_id:
            target_ps = ps
            break
    if target_ps is None:
        result["message"] = f"parameter_registry 中未找到 {ps_id}"
        return result

    family = target_ps["family"]
    timeframe = target_ps["timeframe"]

    # 查找当前 active set
    from aats.bootstrap.active_parameters import load_active_parameter_registry
    active_reg = load_active_parameter_registry(project_root=project_root)
    combo_key = f"{family}_{timeframe.lower()}"
    current_entry = active_reg.get("active_sets", {}).get(combo_key, {})
    prev_ps_id = current_entry.get("parameter_set_id")

    # 2. Gate（可选）
    gate_result = None
    gate_ref = None
    gate_status_str = None
    if run_gate:
        from aats.data_platform.production_workflow.pre_apply_gate import (
            run_pre_apply_gate,
        )
        gate_result = run_pre_apply_gate(project_root, recommendation_id)
        gate_ref = gate_result.get("gate_run_id")
        gate_status_str = gate_result.get("gate_status")
        result["gate_result"] = gate_result

        if not gate_result.get("allow_apply"):
            result["message"] = f"Gate blocked: {gate_result.get('blocking_reasons')}"
            # 仍然创建 release record 但标记为 blocked
            release = create_release_record(
                family=family,
                timeframe=timeframe,
                recommendation_id=recommendation_id,
                parameter_set_id=ps_id,
                actor=actor,
                gate_result_ref=gate_ref,
                gate_status=gate_status_str,
                previous_parameter_set_id=prev_ps_id,
                observation_window_hours=observation_window_hours,
                notes=notes,
            )
            release["apply_result"] = "blocked_by_gate"
            history = load_release_history(project_root)
            add_release(history, release)
            save_release_history(history, project_root)
            result["release"] = release
            return result

    # 3. 创建 release record
    release = create_release_record(
        family=family,
        timeframe=timeframe,
        recommendation_id=recommendation_id,
        parameter_set_id=ps_id,
        actor=actor,
        gate_result_ref=gate_ref,
        gate_status=gate_status_str,
        previous_parameter_set_id=prev_ps_id,
        observation_window_hours=observation_window_hours,
        notes=notes,
    )

    # 4. Apply（可选）
    apply_result_data = None
    if run_apply:
        from aats.data_platform.decision_system.active_parameter_apply import (
            apply_approved_recommendation,
        )
        apply_result_data = apply_approved_recommendation(
            project_root,
            recommendation_id=recommendation_id,
            actor=actor,
            notes=f"Release {release['release_id']}",
            release_id=release["release_id"],
            gate_result=gate_result,
        )
        result["apply_result"] = apply_result_data

        if apply_result_data.get("ok"):
            release["apply_result"] = "success"
            release["observation_status"] = "observing"
        else:
            release["apply_result"] = "failed"
            release["observation_status"] = "not_started"

    # 5. 写入 release history
    history = load_release_history(project_root)
    add_release(history, release)
    save_release_history(history, project_root)

    result["ok"] = release["apply_result"] == "success" or not run_apply
    result["release"] = release
    result["message"] = f"Release {release['release_id']} created"
    return result
