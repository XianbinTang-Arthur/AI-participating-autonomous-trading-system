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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import try_governance_db


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
        except Exception:
            pass
        finally:
            if engine is not None:
                engine.dispose()

    fp = _effectiveness_registry_path(root)
    if not fp.exists():
        return {"evaluations": [], "generated_at": None}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_effectiveness_registry(root: Path, data: dict) -> None:
    data["generated_at"] = datetime.now(timezone.utc).isoformat()

    # 顺序：DB 先、文件后。DB 写失败则文件保持旧状态，避免留下未 commit 的 ghost。
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_upsert_release_effectiveness,
            )

            with Session(engine) as session, session.begin():
                for evaluation in data.get("evaluations", []):
                    if isinstance(evaluation, dict):
                        db_upsert_release_effectiveness(session, evaluation)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "release effectiveness DB 同步失败，保存未完成",
            )
            raise RuntimeError(
                f"release effectiveness DB 同步失败，状态未持久化到真源: {exc}"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()

    _atomic_write_json(_effectiveness_registry_path(root), data)


# ── 维度评估函数 ──────────────────────────────────────────────

def _evaluate_behavior(root: Path, release: dict) -> dict:
    """行为层: 检查 observation 中的 attribution 和 decision status."""
    release_id = release.get("release_id", "")
    from aats.data_platform.production_workflow.observation_window import (
        load_observation_result,
    )

    obs = load_observation_result(root, release_id)
    if not obs:
        return {
            "dimension": "behavior",
            "score": "unknown",
            "detail": "no observation data",
        }

    checklist = {c["name"]: c for c in obs.get("checklist", [])}

    attr_check = checklist.get("attribution", {})
    decision_check = checklist.get("decision_status", {})

    issues = []
    if attr_check.get("status") == "regression":
        issues.append("attribution regression detected")
    if decision_check.get("status") == "regression":
        issues.append("decision status regression")

    if issues:
        return {"dimension": "behavior", "score": "negative", "detail": "; ".join(issues)}
    if attr_check.get("status") == "unknown" and decision_check.get("status") == "unknown":
        return {"dimension": "behavior", "score": "unknown", "detail": "insufficient data"}
    return {"dimension": "behavior", "score": "positive", "detail": "no regression detected"}


def _evaluate_execution(root: Path, release: dict) -> dict:
    """执行层: 检查 execution realism 是否恶化."""
    release_id = release.get("release_id", "")
    from aats.data_platform.production_workflow.observation_window import (
        load_observation_result,
    )

    obs = load_observation_result(root, release_id)
    if not obs:
        return {
            "dimension": "execution",
            "score": "unknown",
            "detail": "no observation data",
        }

    checklist = {c["name"]: c for c in obs.get("checklist", [])}
    exec_check = checklist.get("execution_realism", {})

    if exec_check.get("status") == "regression":
        return {
            "dimension": "execution",
            "score": "negative",
            "detail": exec_check.get("detail", "execution regression"),
        }
    if exec_check.get("status") == "unknown":
        return {"dimension": "execution", "score": "unknown", "detail": "no execution data"}
    return {"dimension": "execution", "score": "positive", "detail": "execution stable or improved"}


def _evaluate_operations(root: Path, release: dict) -> dict:
    """运营层: 检查 rollback 和 observation 完成."""
    release_id = release.get("release_id", "")

    # rollback recommendation
    from aats.data_platform.production_workflow.rollback_policy import (
        load_rollback_recommendation,
    )

    rb = load_rollback_recommendation(root, release_id)
    rollback_recommended = rb.get("rollback_recommended", False) if rb else False

    if rollback_recommended:
        return {
            "dimension": "operations",
            "score": "negative",
            "detail": f"rollback recommended (severity={rb.get('severity', '?')})",
            "rollback_related": True,
        }

    obs_status = release.get("observation_status", "unknown")
    if obs_status == "rolled_back":
        return {
            "dimension": "operations",
            "score": "negative",
            "detail": "rollback executed after apply",
            "rollback_related": True,
        }
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
    return {"dimension": "operations", "score": "unknown", "detail": f"observation_status={obs_status}"}


def _evaluate_governance(root: Path, release: dict) -> dict:
    """治理层: evidence freshness + unresolved alerts."""
    # gate status
    gate_status = release.get("gate_status", "unknown")

    # 当前 alerts
    alerts_data = _load_json(
        root / "artifacts" / "operations" / "alerts" / "current_alerts.json"
    )
    alert_count = 0
    if alerts_data:
        alert_count = sum(
            1 for a in alerts_data.get("alerts", [])
            if not a.get("acknowledged") and a.get("severity") == "critical"
        )

    issues = []
    if gate_status == "block":
        issues.append("gate was blocked")
    if alert_count > 0:
        issues.append(f"{alert_count} unresolved critical alert(s)")

    if issues:
        return {"dimension": "governance", "score": "negative", "detail": "; ".join(issues)}
    if gate_status == "warn":
        return {"dimension": "governance", "score": "mixed", "detail": "gate passed with warnings"}
    return {"dimension": "governance", "score": "positive", "detail": "governance healthy"}


# ── 综合评估 ────────���─────────────────────────────────────────

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
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )

    rel_data = load_release_history(root)
    release = None
    for r in (rel_data.get("releases", []) if rel_data else []):
        if r.get("release_id") == release_id:
            release = r
            break

    if release is None:
        return {"error": f"release {release_id} not found"}

    # 评估各维度
    dimensions = [
        _evaluate_behavior(root, release),
        _evaluate_execution(root, release),
        _evaluate_operations(root, release),
        _evaluate_governance(root, release),
    ]

    # 综合结论
    conclusion = _derive_effectiveness(dimensions, release)

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
        "evaluated_at": now.isoformat(),
        "dimensions": dimensions,
        "baseline_comparison_conclusion": comparison_conclusion,
        "conclusion": conclusion,
        "detail": _effectiveness_detail(dimensions, conclusion),
    }

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
            "rollback_attempts",
            "last_rollback_error",
            "rollback_soft_pause_applied",
        )
        previous = next(
            (e for e in registry["evaluations"] if e.get("release_id") == release_id),
            None,
        )
        if previous is not None:
            for field in _ACTION_STATE_FIELDS:
                if field in previous and field not in evaluation:
                    evaluation[field] = previous[field]
        registry["evaluations"] = [
            e for e in registry["evaluations"]
            if e.get("release_id") != release_id
        ]
        registry["evaluations"].append(evaluation)
        save_effectiveness_registry(root, registry)

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
    if negatives == 0 and positives >= 2:
        return "effective"
    if unknowns >= 3:
        return "insufficient_evidence"
    return "mixed"


def _effectiveness_detail(dimensions: list[dict], conclusion: str) -> str:
    """生成 effectiveness 描��."""
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


def pending_rollback_combos(root: Path) -> dict[str, str]:
    """返回所有 rollback_triggered 但未执行回滚的 combo → release_id 映射.

    供 apply/rollback 入口做安全检查。
    """
    registry = load_effectiveness_registry(root)
    result: dict[str, str] = {}
    for ev in registry.get("evaluations", []):
        if (
            ev.get("conclusion") == "rollback_triggered"
            and not ev.get("rollback_enforced")
            and not ev.get("rollback_cancelled")
        ):
            combo = f"{ev.get('family')}_{ev.get('timeframe', '').lower()}"
            result[combo] = ev.get("release_id", "?")
    return result


def enforce_pending_rollbacks(root: Path) -> list[dict]:
    """检查并执行所有 pending 的 rollback_triggered 结论.

    针对每个 rollback_triggered 且未标记 rollback_enforced 的评估：
      1. 从 release history 查找对应 release 的 previous_parameter_set_id
      2. 调用 rollback_active_parameter_set() 回滚到上一版本
      3. 标记 evaluation 为 rollback_enforced

    Returns
    -------
    list[dict]  每个回滚操作的结果
    """
    registry = load_effectiveness_registry(root)
    results: list[dict] = []
    modified = False

    # Fix P1: 将重复文件 I/O 移到循环外，避免每次评估都重新加载
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )

    rel_data = load_release_history(root)
    all_releases = rel_data.get("releases", []) if rel_data else []

    for ev in registry.get("evaluations", []):
        if ev.get("conclusion") != "rollback_triggered":
            continue
        if ev.get("rollback_enforced"):
            continue
        if ev.get("rollback_cancelled"):
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

        # 从预加载的 release history 查找 release
        release = None
        releases = all_releases
        release_index = None
        for idx, r in enumerate(releases):
            if r.get("release_id") == release_id:
                release = r
                release_index = idx
                break

        if release is None:
            results.append({
                "release_id": release_id,
                "ok": False,
                "error": f"release {release_id} not found in history",
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

        later_successful_release = None
        if release_index is not None:
            for newer in releases[release_index + 1:]:
                if (
                    newer.get("combo_key") == combo_key
                    and newer.get("apply_result") == "success"
                ):
                    later_successful_release = newer
                    break
        if later_successful_release is not None:
            ev["rollback_cancelled"] = True
            ev["rollback_cancelled_at"] = datetime.now(timezone.utc).isoformat()
            ev["rollback_cancelled_reason"] = (
                "superseded by later successful release "
                f"{later_successful_release.get('release_id')}"
            )
            modified = True
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "error": ev["rollback_cancelled_reason"],
            })
            continue

        from aats.bootstrap.active_parameters import load_active_parameter_registry

        # 注意：active_registry 必须在循环内重新加载，因为前面的
        # rollback_active_parameter_set 调用会修改文件内容。
        active_registry = load_active_parameter_registry(project_root=root)
        active_entry = active_registry.get("active_sets", {}).get(combo_key) or {}
        current_ps_id = active_entry.get("parameter_set_id")
        if not current_ps_id:
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "error": (
                    "current active parameter set unavailable; "
                    "cannot verify release still controls production"
                ),
            })
            continue
        if current_ps_id != release_ps_id:
            ev["rollback_cancelled"] = True
            ev["rollback_cancelled_at"] = datetime.now(timezone.utc).isoformat()
            ev["rollback_cancelled_reason"] = (
                f"release {release_id} is no longer active; "
                f"current active parameter set is {current_ps_id}"
            )
            modified = True
            results.append({
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "ok": False,
                "skipped": True,
                "error": ev["rollback_cancelled_reason"],
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

        # 执行回滚
        from aats.data_platform.decision_system.active_parameter_apply import (
            rollback_active_parameter_set,
        )

        rb_result = rollback_active_parameter_set(
            root,
            family=family,
            timeframe=timeframe,
            to_parameter_set_id=prev_ps_id,
            actor="release_effectiveness_auto_rollback",
            notes=(
                f"自动回滚: release {release_id} 的 effectiveness 评估结论为"
                f" rollback_triggered"
            ),
        )

        rb_ok = rb_result.get("ok", False)
        rb_reason = rb_result.get("reason") or ""
        # Bug 8 Layer 2: 识别 "无合法 rollback target" 类型的失败 (而非临时锁/IO
        # 错误)，降级为 soft pause —— 不硬回滚、不重试，通过 active_decisions
        # pause 阻止未来新 apply，实盘当前参数保持不变。
        _SOFT_PAUSE_REASONS = {
            "target_deprecated_too_old",
            "target_deprecated_without_timestamp",
            "no_apply_history_for_target",
            "target_not_found_or_wrong_combo",
        }
        rb_reason_head = rb_reason.split(":", 1)[0]
        is_permanent_no_target = rb_reason_head in _SOFT_PAUSE_REASONS

        if rb_ok:
            # 仅在回滚成功时标记为已执行；失败时保留 pending 状态，
            # 下次调用 enforce_pending_rollbacks 会重试。
            ev["rollback_enforced"] = True
            ev["rollback_enforced_at"] = datetime.now(timezone.utc).isoformat()
            ev["rollback_to_parameter_set_id"] = prev_ps_id
            modified = True
        elif is_permanent_no_target:
            # Layer 2 soft pause: 无合法 rollback target → 写 combo pause +
            # 标记 evaluation cancelled (带 soft_paused reason)，避免无限重试。
            from aats.data_platform.governance._db_util import try_governance_db
            from aats.data_platform.governance.recommendations_db import (
                db_set_combo_pause,
            )
            from sqlalchemy.orm import Session as _SQLSession

            pause_ok = False
            engine, db_ok = try_governance_db()
            if db_ok and engine is not None:
                try:
                    with _SQLSession(engine) as pause_sess:
                        pause_ok = db_set_combo_pause(
                            pause_sess,
                            family=family,
                            timeframe=timeframe,
                            reason=(
                                f"soft_pause_auto_rollback_no_valid_target: "
                                f"release={release_id} reason={rb_reason}"
                            ),
                        )
                        pause_sess.commit()
                finally:
                    engine.dispose()

            ev["rollback_cancelled"] = True
            ev["rollback_cancelled_at"] = datetime.now(timezone.utc).isoformat()
            ev["rollback_cancelled_reason"] = (
                f"soft_paused_no_valid_rollback_target: {rb_reason}"
            )
            ev["rollback_soft_pause_applied"] = pause_ok
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
            # 临时失败 (锁失败 / 网络 / 数据库连接等) → 保留 pending 重试
            ev.setdefault("rollback_attempts", 0)
            ev["rollback_attempts"] += 1
            ev["last_rollback_error"] = rb_result.get("message", "unknown error")
            modified = True

        results.append({
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe,
            "ok": rb_ok,
            "soft_paused": (not rb_ok) and is_permanent_no_target,
            "rollback_result": rb_result,
        })

    if modified:
        save_effectiveness_registry(root, registry)

    return results
