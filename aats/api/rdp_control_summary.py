from __future__ import annotations

import copy
import logging
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import quote as _url_quote

from fastapi import Request
from sqlalchemy.orm import Session

from aats.api._governance_db import governance_session as _governance_session
from aats.data_platform.governance._db_util import try_governance_db
from aats.data_platform.operations.environment_guard import (
    get_current_environment,
    get_observation_window_hours,
    get_policy,
)
from aats.services.operator.rdp_queries import query_rdp_health
from aats.services.operator.rdp_queries import (
    query_active_parameter_sets,
    query_latest_attribution,
    query_latest_decision_round,
    query_latest_decisions,
    query_latest_execution_realism,
    query_latest_recommendations,
    query_parameter_registry,
)

logger = logging.getLogger(__name__)

_PENDING_RECOMMENDATION_STATUSES = {"draft", "approved"}
_RDP_CONTROL_SUMMARY_SNAPSHOT_CACHE_TTL_SECONDS = 5.0
_RDP_CONTROL_SUMMARY_SNAPSHOT_CACHE_MAX_ENTRIES = 16
_rdp_control_summary_snapshot_cache_lock = Lock()
_rdp_control_summary_snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _project_root(request: Request) -> Path:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        try:
            from aats.data_platform.config import get_settings as get_rdp_settings

            root = Path(get_rdp_settings().project_root).resolve()
            if root.exists():
                return root
        except Exception as exc:
            logger.warning("control-summary: failed to resolve project root: %s", exc)
    return Path(".").resolve()


def _combo_key(family: str | None, timeframe: str | None) -> str:
    if not family or not timeframe:
        return ""
    return f"{family}_{str(timeframe).lower()}"


def _snapshot_summary_cache_runtime(request: Request) -> Any | None:
    request_state = getattr(request, "state", None)
    if not bool(getattr(request_state, "_dashboard_snapshot_loader", False)):
        return None
    app_state = getattr(getattr(request, "app", None), "state", None)
    return getattr(app_state, "runtime", None)


def _snapshot_summary_cache_key(root: Path, runtime: Any) -> str:
    return f"{root}|runtime={id(runtime)}"


def _sweep_snapshot_summary_cache_locked(now: float) -> None:
    expired_keys = [
        key
        for key, (cached_at, _) in _rdp_control_summary_snapshot_cache.items()
        if now - cached_at > _RDP_CONTROL_SUMMARY_SNAPSHOT_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        _rdp_control_summary_snapshot_cache.pop(key, None)

    overflow = len(_rdp_control_summary_snapshot_cache) - _RDP_CONTROL_SUMMARY_SNAPSHOT_CACHE_MAX_ENTRIES
    if overflow <= 0:
        return
    oldest_keys = sorted(
        _rdp_control_summary_snapshot_cache,
        key=lambda key: _rdp_control_summary_snapshot_cache[key][0],
    )[:overflow]
    for key in oldest_keys:
        _rdp_control_summary_snapshot_cache.pop(key, None)


def _get_snapshot_summary_cache(root: Path, runtime: Any) -> dict[str, Any] | None:
    cache_key = _snapshot_summary_cache_key(root, runtime)
    now = monotonic()
    with _rdp_control_summary_snapshot_cache_lock:
        _sweep_snapshot_summary_cache_locked(now)
        entry = _rdp_control_summary_snapshot_cache.get(cache_key)
        if entry is None:
            return None
        cached_at, payload = entry
        if now - cached_at > _RDP_CONTROL_SUMMARY_SNAPSHOT_CACHE_TTL_SECONDS:
            _rdp_control_summary_snapshot_cache.pop(cache_key, None)
            return None
        return copy.deepcopy(payload)


def _put_snapshot_summary_cache(root: Path, runtime: Any, payload: dict[str, Any]) -> None:
    cache_key = _snapshot_summary_cache_key(root, runtime)
    with _rdp_control_summary_snapshot_cache_lock:
        _sweep_snapshot_summary_cache_locked(monotonic())
        _rdp_control_summary_snapshot_cache[cache_key] = (monotonic(), copy.deepcopy(payload))
        _sweep_snapshot_summary_cache_locked(monotonic())


def _build_applied_recommendation_ids(active_parameters: dict[str, Any]) -> set[str]:
    # 防御：active_parameters 若 JSON 落地坏了（比如被反序列化成 str 或 list），
    # `.values()` 会抛 AttributeError 让整个 control-summary 退化成裸 500。
    # 这里把非 dict 输入统一视作"没有已应用 recommendation"。
    if not isinstance(active_parameters, dict):
        return set()
    result: set[str] = set()
    for item in active_parameters.values():
        if not isinstance(item, dict):
            continue
        recommendation_id = str(item.get("approval_recommendation_id") or "").strip()
        if recommendation_id:
            result.add(recommendation_id)
    return result


def _iso_sort_key(value: str | None) -> str:
    return value or ""


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """Summary-render timestamp parse; illegal → None to keep endpoint soft-failing.

    The control-summary API should not 500 on a single bad artefact timestamp;
    individual rows just get sorted last. Gate-critical callers must use
    :func:`parse_iso_datetime_utc` directly.
    """
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        return parse_iso_datetime_utc(value, context="rdp_control_summary")
    except ValueError:
        return None


def _load_recent_gate_results(project_root: Path, *, limit: int = 8) -> list[dict[str, Any]]:
    """P0-2 阶段 D：只从 governance DB 读 pre-apply gate 历史.

    DB 不可达或查询异常会抛出 ``RuntimeError`` / 原异常，调用方需要决定如何
    向用户呈现（500、503 或显式 "gate 模块暂不可用" 状态码），不再伪造成
    "没有 gate 历史" 这种误导性状态。``project_root`` 仅作签名保持，用于
    将来扩展，不再用于扫描 ``artifacts/`` 目录。
    """
    del project_root  # artifacts/gates JSON 副本已退出读路径
    engine, ok = try_governance_db()
    if not ok:
        raise RuntimeError("governance DB 不可达，无法加载 pre-apply gate 历史")
    try:
        from aats.data_platform.governance.operational_state_db import (
            db_list_pre_apply_gate_results,
        )

        with Session(engine) as session:
            results = db_list_pre_apply_gate_results(session, limit=limit)
    finally:
        engine.dispose()

    return [
        {
            "gate_run_id": payload.get("gate_run_id"),
            "recommendation_id": payload.get("recommendation_id"),
            "created_at": payload.get("created_at"),
            "gate_status": payload.get("gate_status"),
            "allow_apply": bool(payload.get("allow_apply")),
            "blocking_reasons": payload.get("blocking_reasons") or [],
            "warnings": payload.get("warnings") or [],
            "checks": payload.get("checks") or [],
        }
        for payload in results
        if isinstance(payload, dict)
    ]


def _load_recent_releases(
    project_root: Path,
    *,
    limit: int | None = 10,
) -> list[dict[str, Any]]:
    from aats.data_platform.production_workflow.release_registry import load_release_history

    history = load_release_history(project_root)
    releases = [
        item for item in (history.get("releases") or [])
        if isinstance(item, dict)
    ]
    releases.sort(key=lambda item: _iso_sort_key(item.get("created_at")), reverse=True)
    if limit is None:
        return releases
    return releases[:limit]


def _build_observation_queue(
    project_root: Path,
    *,
    releases: list[dict[str, Any]],
    active_parameters: dict[str, Any],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    if not isinstance(active_parameters, dict):
        active_parameters = {}

    candidate_releases: list[tuple[dict[str, Any], str, str | None, str]] = []
    for release in releases:
        release_id = str(release.get("release_id") or "").strip()
        if not release_id:
            continue
        observation_status = str(release.get("observation_status") or "unknown")
        # M-A2-4 修复：白名单要覆盖前端 OBSERVATION_STATUS_LABELS 里的全部有效
        # 状态，否则像 "rolled_back"（前端已经有展示标签"已回滚"）会被这里静
        # 默丢弃，dashboard 的观察队列看不到已回滚发布，运营者就失去了"刚发
        # 生了一次回滚"的可见信号。保留 "unknown" 跳过，避免脏数据污染 UI。
        if observation_status not in {
            "pending",
            "observing",
            "rollback_recommended",
            "completed",
            "not_started",
            "rolled_back",
        }:
            continue

        combo_key = str(release.get("combo_key") or "").strip()
        active_entry = active_parameters.get(combo_key) if combo_key else None
        current_active_parameter_set_id = None
        if isinstance(active_entry, dict):
            current_active_parameter_set_id = active_entry.get("parameter_set_id")
        is_current_active_release = (
            bool(current_active_parameter_set_id)
            and current_active_parameter_set_id == release.get("parameter_set_id")
        )
        if not is_current_active_release:
            continue
        candidate_releases.append((
            release,
            combo_key,
            current_active_parameter_set_id,
            observation_status,
        ))

    if not candidate_releases:
        return queue

    effectiveness_by_release: dict[str, dict[str, Any]] = {}
    try:
        from aats.data_platform.metrics.release_effectiveness import (
            load_effectiveness_registry,
        )

        registry = load_effectiveness_registry(project_root)
        effectiveness_by_release = {
            item.get("release_id"): item
            for item in (registry.get("evaluations") or [])
            if isinstance(item, dict) and item.get("release_id")
        }
    except Exception:
        effectiveness_by_release = {}

    for release, combo_key, current_active_parameter_set_id, observation_status in candidate_releases:
        release_id = str(release.get("release_id") or "").strip()

        from aats.data_platform.production_workflow.observation_window import (
            load_observation_result,
        )

        observation = load_observation_result(project_root, release_id) or {}

        queue.append({
            "release_id": release_id,
            "family": release.get("family"),
            "timeframe": release.get("timeframe"),
            "combo_key": combo_key,
            "recommendation_id": release.get("recommendation_id"),
            "parameter_set_id": release.get("parameter_set_id"),
            "previous_parameter_set_id": release.get("previous_parameter_set_id"),
            "created_at": release.get("created_at"),
            "actor": release.get("actor"),
            "gate_status": release.get("gate_status"),
            "apply_result": release.get("apply_result"),
            "observation_status": observation_status,
            "observation_window_hours": release.get("observation_window_hours"),
            "notes": release.get("notes"),
            "observation": observation,
            "effectiveness": effectiveness_by_release.get(release_id),
            "current_active_parameter_set_id": current_active_parameter_set_id,
            "is_current_active_release": True,
        })

    def _observation_sort_key(item: dict[str, Any]) -> tuple[int, float]:
        status = str(item.get("observation_status") or "")
        status_priority = {
            "rollback_recommended": 0,
            "observing": 1,
            "pending": 2,
            "not_started": 3,
            "completed": 4,
            "rolled_back": 5,
        }.get(status, 9)
        parsed = _parse_iso_datetime(item.get("created_at"))
        if parsed is None:
            return (status_priority, 0.0)
        # Windows 上 datetime.timestamp() 对 1970-01-01 之前的时间会抛 OSError。
        # 实际业务数据里 created_at 都是发布当下的时间戳（近期），理论上不会撞
        # 这条边界；但以防 mock / 回填数据越过阈值破坏排序，加一层 guard。
        try:
            return (status_priority, -parsed.timestamp())
        except (OSError, ValueError, OverflowError):
            return (status_priority, 0.0)

    queue.sort(key=_observation_sort_key, reverse=False)
    return queue


def _environment_summary() -> dict[str, Any]:
    env = get_current_environment()
    policy = get_policy(env)
    return {
        "name": env,
        "strict_environment": env in {"staging", "prod"},
        "description": policy.get("description"),
        "require_gate_pass": bool(policy.get("require_gate_pass")),
        "require_approval": bool(policy.get("require_approval")),
        "allow_parameter_rollback": bool(policy.get("allow_parameter_rollback")),
        "direct_apply_allowed": env != "prod",
        "required_observation_window_hours": get_observation_window_hours(env),
    }


def _classify_runtime_source(
    *,
    combo_key: str,
    active_parameters: dict[str, Any],
    governance_managed: bool,
    paused_combos: set[str],
) -> str:
    if combo_key in active_parameters:
        return "active_parameters"
    if governance_managed and combo_key in paused_combos:
        return "governance_pause"
    return "profile_defaults"


def _overall_runtime_mode(combo_sources: list[str]) -> str:
    unique_sources = {item for item in combo_sources if item}
    if not unique_sources:
        return "profile_defaults"
    if len(unique_sources) == 1:
        return next(iter(unique_sources))
    return "mixed"


def _build_governance_state(
    *,
    active_params_data: dict[str, Any],
    parameter_registry_data: dict[str, Any],
    latest_research_conclusions: list[dict[str, Any]],
    round_upgrade_candidates: list[dict[str, Any]],
    latest_decision_state: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> dict[str, Any]:
    active_parameters = active_params_data.get("active_sets", {}) or {}
    governance_managed = bool(active_params_data.get("governance_managed", False))
    paused_combos = set(active_params_data.get("paused_combos", []) or [])

    parameter_sets = parameter_registry_data.get("parameter_sets", []) or []
    parameter_by_id = {
        item.get("parameter_set_id"): item
        for item in parameter_sets
        if isinstance(item, dict) and item.get("parameter_set_id")
    }

    latest_round_by_combo = {
        item.get("combo_key"): item
        for item in latest_research_conclusions
        if isinstance(item, dict) and item.get("combo_key")
    }
    round_candidate_by_combo = {
        item.get("combo_key"): item
        for item in round_upgrade_candidates
        if isinstance(item, dict) and item.get("combo_key")
    }
    decision_by_combo = {
        item.get("combo_key"): item
        for item in (latest_decision_state.get("decisions") or [])
        if isinstance(item, dict) and item.get("combo_key")
    }

    recommendations_sorted = sorted(
        [item for item in recommendations if isinstance(item, dict)],
        key=lambda item: _iso_sort_key(item.get("created_at")),
        reverse=True,
    )
    latest_recommendation_by_combo: dict[str, dict[str, Any]] = {}
    pending_upgrade_by_combo: dict[str, dict[str, Any]] = {}
    for rec in recommendations_sorted:
        combo_key = rec.get("combo_key") or _combo_key(
            rec.get("family"), rec.get("timeframe"),
        )
        if not combo_key:
            continue
        latest_recommendation_by_combo.setdefault(combo_key, rec)
        if (
            rec.get("recommendation_type") == "parameter_upgrade"
            and rec.get("status") in _PENDING_RECOMMENDATION_STATUSES
        ):
            pending_upgrade_by_combo.setdefault(combo_key, rec)

    combo_meta: dict[str, dict[str, str]] = {}
    ordered_known_combos = active_params_data.get("known_combos", []) or []
    for combo_key in ordered_known_combos:
        family, _, timeframe = combo_key.partition("_")
        combo_meta[combo_key] = {
            "family": family,
            "timeframe": timeframe,
        }

    all_combo_keys = set(combo_meta)
    all_combo_keys.update(active_parameters)
    all_combo_keys.update(paused_combos)
    all_combo_keys.update(latest_round_by_combo)
    all_combo_keys.update(round_candidate_by_combo)
    all_combo_keys.update(decision_by_combo)
    all_combo_keys.update(latest_recommendation_by_combo)

    for combo_key in all_combo_keys:
        if combo_key in combo_meta:
            continue
        family, _, timeframe = combo_key.partition("_")
        combo_meta[combo_key] = {
            "family": family,
            "timeframe": timeframe,
        }

    combo_order = {combo_key: index for index, combo_key in enumerate(ordered_known_combos)}
    combo_states: list[dict[str, Any]] = []
    combo_sources: list[str] = []

    for combo_key in sorted(all_combo_keys, key=lambda item: (combo_order.get(item, 999), item)):
        meta = combo_meta.get(combo_key, {})
        active_info = active_parameters.get(combo_key, {}) or {}
        latest_round = latest_round_by_combo.get(combo_key, {}) or {}
        round_candidate = round_candidate_by_combo.get(combo_key, {}) or {}
        decision = decision_by_combo.get(combo_key, {}) or {}
        latest_recommendation = latest_recommendation_by_combo.get(combo_key, {}) or {}
        pending_upgrade = pending_upgrade_by_combo.get(combo_key, {}) or {}

        runtime_source = _classify_runtime_source(
            combo_key=combo_key,
            active_parameters=active_parameters,
            governance_managed=governance_managed,
            paused_combos=paused_combos,
        )
        combo_sources.append(runtime_source)

        runtime_active_parameter_set_id = active_info.get("parameter_set_id")
        governance_target_parameter_set_id = decision.get("active_parameter_set_id")
        candidate_parameter_set_id = (
            pending_upgrade.get("target_parameter_set_id")
            or round_candidate.get("parameter_set_id")
            or governance_target_parameter_set_id
        )
        active_parameter_status = None
        if runtime_active_parameter_set_id:
            active_parameter_status = (
                parameter_by_id.get(runtime_active_parameter_set_id, {}).get("status")
                or "unknown"
            )
        candidate_parameter_status = None
        if candidate_parameter_set_id:
            candidate_parameter_status = (
                parameter_by_id.get(candidate_parameter_set_id, {}).get("status")
                or "unknown"
            )

        pending_operator_action = False
        latest_recommendation_status = latest_recommendation.get("status")
        latest_recommendation_type = latest_recommendation.get("recommendation_type")
        if latest_recommendation_status == "draft":
            pending_operator_action = True
        elif (
            pending_upgrade.get("target_parameter_set_id")
            and pending_upgrade.get("target_parameter_set_id")
            != runtime_active_parameter_set_id
        ):
            pending_operator_action = True

        inconsistencies: list[str] = []
        if decision.get("current_status") == "pause" and runtime_source == "active_parameters":
            inconsistencies.append("pause 决策仍有 active 参数在运行")
        if (
            governance_managed
            and decision.get("current_status") in {"keep_active", "lower_priority"}
            and runtime_source != "active_parameters"
        ):
            inconsistencies.append("治理要求继续运行，但当前未加载 active 参数")
        if (
            governance_target_parameter_set_id
            and runtime_active_parameter_set_id
            and governance_target_parameter_set_id != runtime_active_parameter_set_id
        ):
            inconsistencies.append("治理目标参数与当前实盘 active 参数不一致")
        if (
            latest_recommendation.get("recommendation_type") == "parameter_upgrade"
            and latest_recommendation.get("status") == "approved"
            and latest_recommendation.get("target_parameter_set_id")
            and latest_recommendation.get("target_parameter_set_id")
            != runtime_active_parameter_set_id
        ):
            inconsistencies.append("参数建议已审批但尚未应用到实盘")

        combo_states.append({
            "combo_key": combo_key,
            "family": meta.get("family") or latest_round.get("family") or decision.get("family"),
            "timeframe": meta.get("timeframe") or latest_round.get("timeframe") or decision.get("timeframe"),
            "runtime_source": runtime_source,
            "decision_status": decision.get("current_status"),
            "decision_updated_at": decision.get("last_updated_at"),
            "decision_target_parameter_set_id": governance_target_parameter_set_id,
            "latest_round_decision": latest_round.get("decision"),
            "latest_round_confidence": latest_round.get("confidence"),
            "latest_round_reasons": latest_round.get("reasons", []),
            "runtime_active_parameter_set_id": runtime_active_parameter_set_id,
            "runtime_active_parameter_status": active_parameter_status,
            "runtime_active_applied_at": active_info.get("applied_at"),
            "runtime_active_approval_recommendation_id": active_info.get("approval_recommendation_id"),
            "candidate_parameter_set_id": candidate_parameter_set_id,
            "candidate_parameter_status": candidate_parameter_status,
            "candidate_source": (
                "pending_recommendation"
                if pending_upgrade.get("target_parameter_set_id")
                else "latest_research_round"
                if round_candidate.get("parameter_set_id")
                else "decision_registry"
                if governance_target_parameter_set_id
                else None
            ),
            "latest_round_candidate_decision": round_candidate.get("decision"),
            "latest_recommendation_id": latest_recommendation.get("recommendation_id"),
            "latest_recommendation_type": latest_recommendation_type,
            "latest_recommendation_status": latest_recommendation_status,
            "latest_recommendation_created_at": latest_recommendation.get("created_at"),
            "pending_operator_action": pending_operator_action,
            "inconsistencies": inconsistencies,
        })

    return {
        "available": bool(combo_states),
        "governance_managed": governance_managed,
        "paused_combos": sorted(paused_combos),
        "parameter_source_mode": _overall_runtime_mode(combo_sources),
        "status_distribution": latest_decision_state.get("status_distribution", {}),
        "combo_states": combo_states,
    }


def build_rdp_control_summary(request: Request) -> dict[str, Any]:
    # M6 修复：/auth/dashboard/bundle 一次请求会依次渲染
    # rdpWorkbenchOverview / rdpWorkbenchItems / rdpWorkbenchAlerts /
    # rdpTuningOverview / rdpTuningProposals，每个 build_rdp_workbench_*
    # 内部都会调一次 build_rdp_control_summary —— 同一 request 里最多 5 次
    # 重复 DB/文件 IO，数据还完全一样。在 request.state 上做 per-request
    # memoize：key 用固定常量即可，因为 request 本身就是天然隔离边界。
    #
    # M-A2-3 加强：返回的是深拷贝而不是引用。否则如果某个下游 builder（或
    # 未来的维护者）不小心对返回的 dict 做 setitem / update，后续 builder
    # 会读到被污染的 cache 值。deepcopy 一次 summary 约 1–10ms，远低于我们
    # 省掉的多次 DB/文件 IO（50–500ms），划算。
    #
    # 注意：单元测试常用 SimpleNamespace mock request，没有 ``state`` 属性。
    # getattr(request, "state", None) 先拿，再从里面取 cache，测试场景下拿不到
    # 就当 cache miss，继续走正常路径。
    request_state = getattr(request, "state", None)
    cached = getattr(request_state, "_rdp_control_summary_cache", None) if request_state is not None else None
    if cached is not None:
        return copy.deepcopy(cached)

    root = _project_root(request)
    snapshot_cache_runtime = _snapshot_summary_cache_runtime(request)
    if snapshot_cache_runtime is not None:
        cached_snapshot = _get_snapshot_summary_cache(root, snapshot_cache_runtime)
        if cached_snapshot is not None:
            try:
                if request_state is not None:
                    request_state._rdp_control_summary_cache = copy.deepcopy(cached_snapshot)
            except Exception:
                pass
            return cached_snapshot

    environment = _environment_summary()

    tasks_by_workflow: dict[str, Any] = {}
    try:
        from aats.data_platform.governance.rdp_task_db import db_get_recent_tasks

        with _governance_session() as session:
            recent = db_get_recent_tasks(session, limit=50)
        for task in recent:
            workflow = str(task.get("workflow") or "").strip()
            if not workflow:
                continue
            summary = tasks_by_workflow.setdefault(
                workflow,
                {
                    "latest_task": None,
                    "running_task": None,
                    "pending_task": None,
                },
            )
            if summary["latest_task"] is None:
                summary["latest_task"] = task
            if task.get("status") == "running" and summary["running_task"] is None:
                summary["running_task"] = task
            if task.get("status") == "pending" and summary["pending_task"] is None:
                summary["pending_task"] = task

        for workflow, summary in tasks_by_workflow.items():
            display_task = (
                summary.get("running_task")
                or summary.get("pending_task")
                or summary.get("latest_task")
            )
            summary["display_task"] = display_task
            # M1 修复：不再 `summary.update(display_task)`。这行把 task 的
            # status/started_at/finished_at/error_message 拍到 summary 顶层，
            # 让外层消费者误以为 summary.status 代表整个 lane 状态，但实际
            # display_task 可能只是 latest_task（已 done），而 running 或
            # pending lane 仍有任务。前端全部从子字段（running_task /
            # pending_task / latest_task / display_task）读，不需要这层平铺。
            summary["workflow"] = workflow
    except Exception as exc:
        logger.warning("control-summary: task query failed: %s", exc)

    recommendations: list[dict[str, Any]] = []
    try:
        recommendations_data = query_latest_recommendations(
            root, limit=200, status_filter=None,
        )
        recommendations = [
            {
                "recommendation_id": rec.get("recommendation_id"),
                "symbol": rec.get("symbol"),
                "family": rec.get("family"),
                "timeframe": rec.get("timeframe"),
                "combo_key": _combo_key(rec.get("family"), rec.get("timeframe")),
                "scope": rec.get("scope") or "combo",
                "scope_ref": rec.get("scope_ref"),
                "recommendation_type": rec.get("recommendation_type"),
                "confidence": rec.get("confidence"),
                "reason": rec.get("reason"),
                "status": rec.get("status"),
                "target_parameter_set_id": rec.get("target_parameter_set_id"),
                "source_round_id": rec.get("source_round_id"),
                "created_at": rec.get("created_at"),
            }
            for rec in (recommendations_data.get("recommendations") or [])
            if isinstance(rec, dict)
        ]
    except Exception as exc:
        logger.warning("control-summary: recommendations query failed: %s", exc)

    active_params_data: dict[str, Any] = {
        "generated_at": None,
        "governance_managed": False,
        "paused_combos": [],
        "known_combos": [],
        "active_sets": {},
        "parameter_sets": [],
    }
    try:
        active_params_data = query_active_parameter_sets(root)
    except Exception as exc:
        logger.warning("control-summary: active params query failed: %s", exc)
    active_parameters = active_params_data.get("active_sets", {}) or {}
    applied_recommendation_ids = _build_applied_recommendation_ids(active_parameters)
    released_success_recommendation_ids: set[str] = set()
    # release_history_status 默认未知；若下面 load_release_history 成功，这里会被覆盖
    # 成 {"source": "db"|"json"|"empty", "stale": bool, ...}。UI 需要把 stale=True
    # 的状态向运营者显式透出（比如在"最近 release"模块旁挂个 stale 标签），否则
    # DB 抖动时运营者会误把 JSON 副本当成真源。
    release_history_status: dict[str, Any] = {
        "source": "unknown",
        "stale": False,
    }
    try:
        from aats.data_platform.production_workflow.release_registry import (
            load_release_history,
        )

        release_history = load_release_history(root)
        release_history_status = {
            "source": str(release_history.get("source") or "unknown"),
            "stale": bool(release_history.get("stale")),
        }
        stale_reason = release_history.get("stale_reason")
        if stale_reason:
            release_history_status["stale_reason"] = str(stale_reason)
        released_success_recommendation_ids = {
            str(item.get("recommendation_id") or "").strip()
            for item in (release_history.get("releases") or [])
            if isinstance(item, dict)
            and item.get("apply_result") == "success"
            and str(item.get("recommendation_id") or "").strip()
        }
    except Exception as exc:
        logger.warning("control-summary: release history query failed: %s", exc)
    pending_recommendations = [
        rec
        for rec in recommendations
        if rec.get("status") in _PENDING_RECOMMENDATION_STATUSES
        and not (
            rec.get("recommendation_type") == "parameter_upgrade"
            and rec.get("status") == "approved"
            and (
                rec.get("recommendation_id") in applied_recommendation_ids
                or rec.get("recommendation_id") in released_success_recommendation_ids
            )
        )
    ]

    latest_research_conclusions: list[dict[str, Any]] = []
    round_upgrade_candidates: list[dict[str, Any]] = []
    try:
        decision_round = query_latest_decision_round(root)
        if decision_round.get("available"):
            # decision_round 的 round_id 是"本 round 的所有 phase2 结论"的共同
            # 追溯锚。evidence_bundle / phase2 卡片需要显示这个 round_id 才能形
            # 成完整的证据链；如果每条结论本身也带了 source_round_id（例如从
            # 跨 round 汇总合成的视图），优先用条目级的细粒度值。
            decision_round_id = decision_round.get("round_id")
            conclusions = [
                item
                for item in (decision_round.get("family_timeframe_decisions") or [])
                if isinstance(item, dict)
            ]
            latest_research_conclusions = [
                {
                    "combo_key": item.get("combo_key"),
                    "family": item.get("family"),
                    "timeframe": item.get("timeframe"),
                    "decision": item.get("decision"),
                    "confidence": item.get("confidence"),
                    "reasons": item.get("reasons", []),
                    "signal_summary": item.get("signal_summary", {}),
                    # 把 round_id 随每条结论下发，下游 evidence digest 读到的
                    # phase2 就能直接拿到 round 追溯锚，不用再回头 join。
                    "source_round_id": item.get("source_round_id") or decision_round_id,
                    "round_id": item.get("round_id") or decision_round_id,
                }
                for item in conclusions
            ]
            round_upgrade_candidates = [
                {
                    "combo_key": item.get("combo_key")
                    or _combo_key(item.get("family"), item.get("timeframe")),
                    "family": item.get("family"),
                    "timeframe": item.get("timeframe"),
                    "decision": item.get("decision"),
                    "parameter_set_id": item.get("parameter_set_id"),
                    "confidence": item.get("confidence"),
                    "reason": item.get("reason"),
                }
                for item in (decision_round.get("parameter_upgrade_candidates") or [])
                if isinstance(item, dict)
            ]
    except Exception as exc:
        logger.warning("control-summary: decision round query failed: %s", exc)

    latest_decision_state: dict[str, Any] = {
        "available": False,
        "generated_at": None,
        "status_distribution": {},
        "decisions": [],
    }
    try:
        decisions_data = query_latest_decisions(root)
        if decisions_data.get("available"):
            latest_decision_state = {
                "available": True,
                "generated_at": decisions_data.get("generated_at"),
                "status_distribution": decisions_data.get("status_distribution", {}),
                "decisions": [
                    {
                        "combo_key": item.get("combo_key"),
                        "family": item.get("family"),
                        "timeframe": item.get("timeframe"),
                        "current_status": item.get("current_status"),
                        "active_parameter_set_id": item.get("active_parameter_set_id"),
                        "last_recommendation_id": item.get("last_recommendation_id"),
                        "last_updated_at": item.get("last_updated_at"),
                    }
                    for item in (decisions_data.get("decisions") or [])
                    if isinstance(item, dict)
                ],
            }
    except Exception as exc:
        logger.warning("control-summary: decision registry query failed: %s", exc)

    parameter_registry_data: dict[str, Any] = {
        "available": False,
        "parameter_sets": [],
        "status_distribution": {},
    }
    try:
        parameter_registry_data = query_parameter_registry(root)
    except Exception as exc:
        logger.warning("control-summary: parameter registry query failed: %s", exc)
    governance_state = _build_governance_state(
        active_params_data=active_params_data,
        parameter_registry_data=parameter_registry_data,
        latest_research_conclusions=latest_research_conclusions,
        round_upgrade_candidates=round_upgrade_candidates,
        latest_decision_state=latest_decision_state,
        recommendations=recommendations,
    )
    health = query_rdp_health(root)
    # _load_recent_gate_results 在 governance DB 不可达时会抛 RuntimeError。
    # 这里降级为空列表，并通过 gate_history_status 向 UI 透出不可用原因，
    # 避免整个 control-summary 端点因为 gate 历史挂了而退化成裸 500。
    gate_history_status: dict[str, Any] = {"source": "db", "available": True}
    try:
        recent_gate_results = _load_recent_gate_results(root)
    except Exception as exc:
        logger.warning(
            "control-summary: gate 历史不可用（governance DB 抖动或查询失败）: %s",
            exc,
        )
        recent_gate_results = []
        gate_history_status = {
            "source": "db",
            "available": False,
            "unavailable_reason": str(exc) or "governance_db_unavailable",
        }
    recent_releases = _load_recent_releases(root, limit=10)
    observation_source_releases = _load_recent_releases(root, limit=None)
    observation_queue = _build_observation_queue(
        root,
        releases=observation_source_releases,
        active_parameters=active_parameters,
    )
    approved_parameter_recommendations = [
        rec
        for rec in pending_recommendations
        if rec.get("recommendation_type") == "parameter_upgrade"
        and rec.get("status") == "approved"
    ]
    draft_parameter_recommendations = [
        rec
        for rec in pending_recommendations
        if rec.get("recommendation_type") == "parameter_upgrade"
        and rec.get("status") == "draft"
    ]
    latest_gate = recent_gate_results[0] if recent_gate_results else None
    latest_release = recent_releases[0] if recent_releases else None

    # v3 §1.10 UI: Hero 4-column breakdown — scope × draft count
    # 让 operator 能一眼区分 combo vs profile vs sleeve 的 backlog。
    pending_by_scope: dict[str, int] = {
        "combo": 0, "profile": 0, "sleeve": 0, "risk": 0,
    }
    for rec in pending_recommendations:
        if rec.get("status") != "draft":
            continue
        scope = str(rec.get("scope") or "combo")
        if scope in pending_by_scope:
            pending_by_scope[scope] += 1

    operations_summary = {
        "approved_release_candidate_count": len(approved_parameter_recommendations),
        "draft_recommendation_count": sum(
            1 for rec in pending_recommendations if rec.get("status") == "draft"
        ),
        "draft_parameter_recommendation_count": len(draft_parameter_recommendations),
        "draft_recommendation_count_by_scope": pending_by_scope,
        "observing_release_count": sum(
            1 for item in observation_queue
            if item.get("observation_status") == "observing"
        ),
        "rollback_recommended_count": sum(
            1 for item in observation_queue
            if item.get("observation_status") == "rollback_recommended"
        ),
        "latest_gate_status": latest_gate.get("gate_status") if latest_gate else None,
        "latest_release_apply_result": (
            latest_release.get("apply_result") if latest_release else None
        ),
        "health_status": health.get("overall_health"),
        "health_blocked": health.get("overall_health") == "blocked",
    }

    result = {
        "environment": environment,
        "health": health,
        "operations_summary": operations_summary,
        "tasks": tasks_by_workflow,
        "pending_recommendations": pending_recommendations,
        "active_parameters": active_parameters,
        "governance_state": governance_state,
        "latest_research_conclusions": latest_research_conclusions,
        "round_upgrade_candidates": round_upgrade_candidates,
        "latest_decision_state": latest_decision_state,
        "recent_gate_results": recent_gate_results,
        "gate_history_status": gate_history_status,
        "observation_queue": observation_queue,
        "release_history_status": release_history_status,
    }
    try:
        # 存 deepcopy，这样后续调用方即便拿到本次返回值做了原地修改，下一个
        # cache 读者也是从干净的拷贝开始。代价是多一次 deepcopy，收益是把
        # "cache 不可变" 写死成函数契约。
        request.state._rdp_control_summary_cache = copy.deepcopy(result)
    except Exception:
        # Starlette Request.state 是 State()，setattr 总能成功；极少数 mock
        # request 不支持 setattr，吞掉并回退到 no-cache 语义。
        pass
    if snapshot_cache_runtime is not None:
        _put_snapshot_summary_cache(root, snapshot_cache_runtime, result)
    return result


_WORKBENCH_RECOMMENDATION_LABELS = {
    "parameter_upgrade": "参数候选待审批",
    "keep_active": "建议保持当前",
    "lower_priority": "建议降低优先级",
    "pause": "建议暂停该组合",
    "require_review": "建议人工复核",
}

_WORKFLOW_ACTION_LABELS = {
    "data_maintenance": "刷新数据",
    "research_cycle": "运行完整 RDP",
}

_HEALTH_ALERT_TITLE_LABELS = {
    "queue_state": "任务队列状态",
    "current_alerts": "当前告警文件",
    "readonly_access": "生产数据库连接",
    "active_parameter_sets": "Active 参数状态",
    "rdp_task_queue_backlog_or_failures": "任务队列积压",
}


def _split_reason_text(value: str | None) -> list[str]:
    if not value:
        return []
    text = str(value).replace("[", " ").replace("]", " ")
    for delimiter in ("\r", "\n", "；", ";", "。", "|"):
        text = text.replace(delimiter, "\n")
    result: list[str] = []
    for part in text.splitlines():
        candidate = part.strip(" -,:，。；;")
        if candidate:
            result.append(candidate)
    return result


def _dedupe_texts(values: list[str], *, limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
        if limit is not None and len(result) >= limit:
            break
    return result


def _humanize_reason_entry(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    normalized = re.sub(r"\s+", " ", text)
    compact = normalized.replace("[", " ").replace("]", " ").strip()

    patterns: list[tuple[re.Pattern[str], str | None]] = [
        (
            re.compile(r".*有\s*(\d+)\s*个实验产生开仓信号.*", re.IGNORECASE),
            "Step2 中共有 {0} 个实验出现开仓信号。",
        ),
        (
            re.compile(r".*最大\s*opening_count\s*=\s*(\d+).*", re.IGNORECASE),
            "本轮最高开仓次数为 {0}。",
        ),
        (
            re.compile(r".*平均\s*positive_edge_ratio\s*=\s*([0-9.]+)\s*>=\s*([0-9.]+).*", re.IGNORECASE),
            "正向收益占比为 {0}，已达到阈值 {1}。",
        ),
        (
            re.compile(r".*Phase 3 .*status\s*=\s*succeeded.*", re.IGNORECASE),
            "Phase3 归因任务已完成。",
        ),
        (
            re.compile(r".*failure\s*占比过高\s*:\s*(\d+)\s*/\s*(\d+).*", re.IGNORECASE),
            "Phase3 失败占比过高（{0}/{1}）。",
        ),
        (
            re.compile(r".*cost_adjusted_edge\s*=\s*([-0-9.]+)\s*bps\s*>=\s*([-0-9.]+).*", re.IGNORECASE),
            "Phase4 成本后边际为 {0} bps。",
        ),
        (
            re.compile(r".*full_fill_ratio\s*=\s*([0-9.]+)%\s*>=\s*([0-9.]+)%.*", re.IGNORECASE),
            "Phase4 完整成交率为 {0}%。",
        ),
        (
            re.compile(r".*治理层\s+degraded.*", re.IGNORECASE),
            "治理层当前处于降级状态。",
        ),
        (
            re.compile(r".*参数状态\s*:\s*candidate.*", re.IGNORECASE),
            "参数仍是候选状态，尚未正式生效。",
        ),
    ]
    for pattern, template in patterns:
        match = pattern.match(compact)
        if not match:
            continue
        if template is None:
            return None
        return template.format(*match.groups())

    direct_map = {
        "多维度正面且无负面": "研究和执行面整体偏正，暂时没有需要调整的信号。",
        "治理要求继续运行，但当前未加载 active 参数": "系统建议继续观察，但目前还没有已生效的实盘参数。",
        "治理目标参数与当前实盘 active 参数不一致": "治理目标参数与当前实盘参数不一致。",
        "参数建议已审批但尚未应用到实盘": "参数建议已审批，但还没有应用到实盘。",
        "已完成审批，准备创建 release": "这组参数已经批准，下一步可以运行 Gate 或创建发布。",
        "等待审批": "这组参数已经生成，等待人工确认。",
        "多维度正面且无负面。": "研究和执行面整体偏正，暂时没有需要调整的信号。",
        "治理要求继续运行，但当前未加载 active 参数。": "系统建议继续观察，但目前还没有已生效的实盘参数。",
    }
    if compact in direct_map:
        return direct_map[compact]

    replacements = {
        "opening_count": "开仓次数",
        "positive_edge_ratio": "正向收益占比",
        "failure_ratio": "失败占比",
        "full_fill_ratio": "完整成交率",
        "cost_adjusted_edge_proxy_bps": "成本后边际",
        "cost_adjusted_edge": "成本后边际",
        "mean_cost_bps": "平均成本",
        "candidate": "候选",
        "succeeded": "已完成",
        "degraded": "降级",
        "status": "状态",
        "active": "已生效",
    }
    for raw, friendly in replacements.items():
        compact = re.sub(rf"\b{re.escape(raw)}\b", friendly, compact, flags=re.IGNORECASE)

    compact = re.sub(r"\bphase\d+_[a-z_]+\b", "", compact, flags=re.IGNORECASE).strip(" -:;，。")
    if not compact:
        return None
    if compact.endswith("。"):
        return compact
    return f"{compact}。"


def _build_reason_summary(rec: dict[str, Any], combo_state: dict[str, Any]) -> list[str]:
    values = (
        _split_reason_text(rec.get("reason"))
        + [str(item) for item in (combo_state.get("latest_round_reasons") or [])]
        + [str(item) for item in (combo_state.get("inconsistencies") or [])]
    )
    return _dedupe_texts(
        [humanized for value in values if (humanized := _humanize_reason_entry(value))],
        limit=2,
    )


def _approval_effect_summary_v2(recommendation_type: str) -> str:
    if recommendation_type == "parameter_upgrade":
        return "批准后进入待发布，下一步是运行 Gate 或创建发布。"
    if recommendation_type == "keep_active":
        return "批准后只记录“保持当前”，不会进入发布。"
    if recommendation_type == "lower_priority":
        return "批准后只记录“降低优先级”，不会进入发布。"
    if recommendation_type == "pause":
        return "批准后会把该组合标记为暂停，不会进入发布。"
    return "批准后只会记录这轮治理结论，不会进入发布。"


def _decision_summary_v2(
    recommendation_type: str,
    *,
    has_integrity_block: bool,
    combo_state: dict[str, Any],
) -> str:
    if has_integrity_block:
        return "这轮证据还不完整，先补数据和研究结果，再决定是否审批。"
    if recommendation_type == "parameter_upgrade":
        return "这轮产出了一组新参数，确认后就会进入待发布。"
    if recommendation_type == "keep_active":
        if combo_state.get("runtime_source") != "active_parameters":
            return "这轮先保持不动。现在还没有实盘参数，这次只记录治理结论。"
        return "这轮先保持不动，不创建新发布。"
    if recommendation_type == "lower_priority":
        return "这轮建议降低这个组合的优先级，不创建新发布。"
    if recommendation_type == "pause":
        return "这轮建议暂停这个组合，不创建新发布。"
    return "这轮结论还不够稳定，需要人工复核后再定。"


def _approval_action_label_v2(recommendation_type: str) -> str:
    if recommendation_type == "parameter_upgrade":
        return "批准参数候选"
    if recommendation_type == "keep_active":
        return "同意保持当前"
    if recommendation_type == "lower_priority":
        return "同意降优先级"
    if recommendation_type == "pause":
        return "同意暂停"
    return "转人工复核"


def _reject_action_label_v2(recommendation_type: str) -> str:
    if recommendation_type == "parameter_upgrade":
        return "退回参数候选"
    if recommendation_type == "keep_active":
        return "退回此结论"
    if recommendation_type == "lower_priority":
        return "退回此结论"
    if recommendation_type == "pause":
        return "退回此结论"
    return "退回复核结论"


_WORKBENCH_HEALTH_ALERT_TITLE_LABELS = {
    "queue_state": "任务队列积压",
    "current_alerts": "最新告警结果",
    "current_alerts_missing": "最新告警结果",
    "readonly_access": "生产数据库连接",
    "active_parameter_sets": "实盘参数",
    "no_active_parameter_sets": "实盘参数",
    "rdp_task_queue_backlog_or_failures": "任务队列积压",
    "live_db_unhealthy": "生产数据库连接",
    "workflow_runs_stale_or_missing": "流程结果过旧",
    "workflow_runs_incomplete": "流程结果不完整",
    "rdp_daemon_unhealthy": "后台服务状态",
    "rdp_daemon_degraded": "后台服务状态",
    "rdp_daemon_status_missing": "后台服务状态",
    "governance_db_unreachable": "治理数据库",
}


def _workflow_disabled_reason(workflow: str, state: str) -> str:
    action = _WORKFLOW_ACTION_LABELS.get(workflow, workflow)
    if state == "running":
        return f"{action}正在执行，完成后才能再次点击。"
    if state == "pending":
        return f"{action}已经在排队，请等待上一条任务完成。"
    return "当前暂时不能执行这个操作。"


def _summarize_queue_state(detail: str | None) -> str:
    text = str(detail or "").strip()
    if not text:
        return "任务队列里还有未处理的任务。"
    match = re.search(
        r"pending\s*=\s*(\d+)\s*,\s*running\s*=\s*(\d+)\s*,\s*failed\s*=\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return f"任务队列仍有积压或失败：{text}"
    pending, running, failed = (int(item) for item in match.groups())
    parts: list[str] = []
    if pending:
        parts.append(f"待执行 {pending} 条")
    if running:
        parts.append(f"执行中 {running} 条")
    if failed:
        parts.append(f"失败 {failed} 条")
    if not parts:
        return "任务队列目前没有异常。"
    return f"任务队列里还有未处理记录：{'，'.join(parts)}。"


def _humanize_operational_alert(title: str | None, message: str | None, code: str | None) -> tuple[str, str] | None:
    normalized_title = str(title or "").strip()
    normalized_code = str(code or "").strip()
    normalized_message = str(message or "").strip()
    title_key = normalized_title or normalized_code
    friendly_title = _WORKBENCH_HEALTH_ALERT_TITLE_LABELS.get(
        title_key,
        normalized_title or "系统告警",
    )

    if title_key in {"current_alerts", "current_alerts_missing", "rdp_task_queue_backlog_or_failures"}:
        return None
    if title_key == "queue_state":
        return friendly_title, _summarize_queue_state(normalized_message)
    if title_key == "readonly_access":
        return friendly_title, "当前没有可写的生产数据库连接，发布链只能停留在只读状态。"
    if title_key in {"active_parameter_sets", "no_active_parameter_sets"}:
        return friendly_title, "当前还没有已生效的实盘参数。先完成治理结论，再决定是否发布。"
    if title_key in {"rdp_daemon_unhealthy", "rdp_daemon_degraded", "rdp_daemon_status_missing"}:
        return friendly_title, "后台服务状态异常，新的 RDP 任务可能不会继续推进。"
    if title_key == "live_db_unhealthy":
        return friendly_title, "生产数据库连接不可用，发布链当前不能继续。"
    if title_key == "workflow_runs_stale_or_missing":
        return friendly_title, "最近一轮流程结果过旧或缺失，请先补跑完整流程。"
    if title_key == "workflow_runs_incomplete":
        return friendly_title, "最近一轮流程结果还不完整，请先补齐再审批。"
    if title_key == "governance_db_unreachable":
        return friendly_title, "治理数据库当前不可用，治理和发布链都会受影响。"
    return friendly_title, normalized_message or "当前存在需要处理的系统告警。"


def _build_manual_workflow_actions(
    project_root: Path,
    tasks: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    from aats.data_platform.operations.workflow_dispatcher import (
        describe_manual_trigger_availability,
    )

    actions: list[dict[str, Any]] = []
    for workflow in ("data_maintenance", "research_cycle"):
        lane = (tasks or {}).get(workflow) or {}
        availability = describe_manual_trigger_availability(project_root, workflow)
        disabled_reason = availability.get("disabled_reason")
        enabled = bool(availability.get("enabled"))
        if lane.get("running_task"):
            enabled = False
            disabled_reason = _workflow_disabled_reason(workflow, "running")
        elif lane.get("pending_task"):
            enabled = False
            disabled_reason = _workflow_disabled_reason(workflow, "pending")
        actions.append(_make_ui_action(
            key=f"trigger_{workflow}",
            label=_WORKFLOW_ACTION_LABELS[workflow],
            ui_action="rdp-trigger-workflow",
            value=workflow,
            enabled=enabled,
            disabled_reason=disabled_reason,
        ))
    primary = actions[0] if actions else None
    secondary = actions[1:] if len(actions) > 1 else []
    return primary, secondary


def _build_release_candidates_payload(summary: dict[str, Any]) -> dict[str, Any]:
    governance_state = summary.get("governance_state") or {}
    combo_states = {
        item.get("combo_key"): item
        for item in (governance_state.get("combo_states") or [])
        if isinstance(item, dict) and item.get("combo_key")
    }
    latest_gate_by_recommendation = {
        str(item.get("recommendation_id") or ""): item
        for item in (summary.get("recent_gate_results") or [])
        if isinstance(item, dict) and str(item.get("recommendation_id") or "").strip()
    }
    approved_candidates = [
        item
        for item in (summary.get("pending_recommendations") or [])
        if isinstance(item, dict)
        and item.get("recommendation_type") == "parameter_upgrade"
        and item.get("status") == "approved"
    ]
    by_combo: dict[str, dict[str, Any]] = {}
    for rec in sorted(
        approved_candidates,
        key=lambda item: _iso_sort_key(item.get("created_at")),
        reverse=True,
    ):
        combo_key = str(rec.get("combo_key") or _combo_key(rec.get("family"), rec.get("timeframe")))
        if combo_key and combo_key not in by_combo:
            by_combo[combo_key] = rec

    items: list[dict[str, Any]] = []
    for combo_key, rec in by_combo.items():
        combo_state = combo_states.get(combo_key) or {}
        gate_result = latest_gate_by_recommendation.get(str(rec.get("recommendation_id") or ""))
        gate_status = str((gate_result or {}).get("gate_status") or "not_run")
        gate_note = None
        if gate_result:
            gate_note = (
                ((gate_result.get("blocking_reasons") or [None])[0])
                or ("最近一次 Gate 有警告，请先复核。" if (gate_result.get("warnings") or []) else None)
                or "最近一次 Gate 已执行。"
            )
        item = {
            "combo_key": combo_key,
            "family": rec.get("family") or combo_state.get("family"),
            "timeframe": rec.get("timeframe") or combo_state.get("timeframe"),
            "recommendation_id": rec.get("recommendation_id"),
            "confidence": rec.get("confidence") or "unknown",
            "headline": "已批准，待发布",
            "decision_summary": "这组参数已经批准，下一步可以运行 Gate 或直接创建发布。",
            "gate_status": gate_status,
            "gate_note": _humanize_reason_entry(str(gate_note)) if gate_note else None,
            "reason_summary": _build_reason_summary(rec, combo_state),
            "created_at": rec.get("created_at"),
            "actions": [
                _make_ui_action(
                    key="run_gate",
                    label="运行 Gate",
                    ui_action="rdp-run-gate",
                    value=str(rec.get("recommendation_id") or ""),
                ),
                _make_ui_action(
                    key="create_release",
                    label="创建发布",
                    ui_action="rdp-create-release",
                    value=str(rec.get("recommendation_id") or ""),
                ),
            ],
        }
        items.append(item)

    return {
        "total": len(items),
        "items": items,
    }


def _make_ui_action(
    *,
    key: str,
    label: str,
    ui_action: str,
    value: str,
    enabled: bool = True,
    disabled_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ui_action": ui_action,
        "value": value,
        "enabled": bool(enabled),
        "disabled_reason": disabled_reason,
    }


def _build_task_lane_summary(tasks: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    running_candidates: list[dict[str, Any]] = []
    pending_candidates: list[dict[str, Any]] = []
    for workflow, payload in (tasks or {}).items():
        if not isinstance(payload, dict):
            continue
        running = payload.get("running_task")
        if isinstance(running, dict):
            running_candidates.append({
                "workflow": workflow,
                "status": "running",
                "started_at": running.get("started_at"),
                "requested_at": running.get("requested_at"),
                "task_id": running.get("task_id"),
            })
        pending = payload.get("pending_task")
        if isinstance(pending, dict):
            pending_candidates.append({
                "workflow": workflow,
                "status": "pending",
                "requested_at": pending.get("requested_at"),
                "task_id": pending.get("task_id"),
            })

    running_candidates.sort(key=lambda item: _iso_sort_key(item.get("started_at")))
    pending_candidates.sort(key=lambda item: _iso_sort_key(item.get("requested_at")))
    current_execution = running_candidates[0] if running_candidates else {
        "workflow": None,
        "status": "idle",
        "task_id": None,
        "started_at": None,
        "requested_at": None,
    }
    next_queue = pending_candidates[0] if pending_candidates else {
        "workflow": None,
        "status": "none",
        "task_id": None,
        "requested_at": None,
    }
    return current_execution, next_queue


def _load_workbench_phase_payloads(root: Path) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for phase, query_fn in (
        ("phase3", query_latest_attribution),
        ("phase4", query_latest_execution_realism),
    ):
        try:
            payload = query_fn(root)
        except Exception:
            logger.exception("workbench: %s payload query failed", phase)
            payload = {"available": False, "incomplete_reason": "query_failed"}
        if not isinstance(payload, dict):
            payload = {"available": False, "incomplete_reason": "query_failed"}
        payloads[phase] = payload
    return payloads


def _build_workbench_alerts_payload(
    root: Path,
    summary: dict[str, Any],
    *,
    phase_payloads: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    integrity_alerts: list[dict[str, Any]] = []
    operational_alerts: list[dict[str, Any]] = []

    try:
        from aats.data_platform.governance.snapshot_db import (
            ROUND_PHASE_STEP2,
            is_snapshot_incomplete,
            load_latest_research_round_snapshot,
        )

        step2_snapshot = load_latest_research_round_snapshot(
            phase=ROUND_PHASE_STEP2,
            project_root=root,
        )
        if is_snapshot_incomplete(step2_snapshot):
            integrity_alerts.append({
                "code": "step2_manifest_missing",
                "severity": "danger",
                "scope": "round",
                "phase": "phase2",
                "title": "Step2 研究快照不完整",
                "message": "最新 Step2 目录缺少 round_manifest，当前轮次不能据此做正式审批。",
                "blocks_approval": True,
            })
    except Exception as exc:
        logger.warning("workbench alerts: failed to inspect step2 snapshot: %s", exc)

    # phase3 / phase4 payload 查询本身也要 fail-safe——真 DB/文件抖动不应让整个
    # /auth/dashboard/bundle 500，否则前端回到 "RDP 数据暂未就绪" 的 callout，
    # 等于又撞上 B2 的回归场景。任一 query 抛异常就降级为 "unavailable"，让下面
    # 的 gate 当作缺 round 处理（阻塞审批，显示告警）。
    phase_payloads = phase_payloads or _load_workbench_phase_payloads(root)
    safe_phase_payloads = [
        ("phase3", phase_payloads.get("phase3", {}), "最新归因结果不完整"),
        ("phase4", phase_payloads.get("phase4", {}), "最新执行评估不完整"),
    ]

    for phase, payload, title in safe_phase_payloads:
        if payload.get("available"):
            continue
        incomplete_reason = str(payload.get("incomplete_reason") or "").strip()
        # H5 修复：available=False 但没给 incomplete_reason 的情况下，历史代码
        # 直接 `continue` 跳过告警，审批门禁就看不到这一 phase 缺席 —— 等于静默
        # 放行。现在统一视为 "missing_round"：告警一定会写，blocks_approval=True。
        if not incomplete_reason:
            incomplete_reason = "missing_round"
        # M8 修复：manifest_missing_on_disk 的语义是清单本身从磁盘消失，属于
        # 审批阻塞级别（和 step2_manifest_missing 同级），severity 应为 danger。
        # 此前标成 warning 让 UI 看起来像可以推进，与 blocks_approval=True 矛盾。
        integrity_alerts.append({
            "code": incomplete_reason,
            "severity": "danger",
            "scope": "phase",
            "phase": phase,
            "title": title,
            "message": f"{phase.upper()} 当前不可用于正式结论：{incomplete_reason}",
            "blocks_approval": True,
        })

    health = summary.get("health") or {}
    for check in health.get("checks") or []:
        status = str(check.get("status") or "").lower()
        if status not in {"warn", "blocked"}:
            continue
        humanized = _humanize_operational_alert(
            str(check.get("name") or ""),
            str(check.get("detail") or ""),
            str(check.get("name") or ""),
        )
        if humanized is None:
            continue
        title, message = humanized
        operational_alerts.append({
            "code": f"{check.get('category')}:{check.get('name')}",
            "severity": "danger" if status == "blocked" else "warning",
            "title": title,
            "message": message,
        })

    for reason in health.get("blocking_reasons") or []:
        humanized = _humanize_operational_alert(None, str(reason), str(reason))
        if humanized is None:
            continue
        title, message = humanized
        operational_alerts.append({
            "code": str(reason),
            "severity": "danger",
            "title": title,
            "message": message,
        })

    for reason in health.get("warnings") or []:
        humanized = _humanize_operational_alert(None, str(reason), str(reason))
        if humanized is None:
            continue
        title, message = humanized
        operational_alerts.append({
            "code": str(reason),
            "severity": "warning",
            "title": title,
            "message": message,
        })

    deduped_operational_alerts: list[dict[str, Any]] = []
    alert_index_by_title: dict[str, int] = {}
    for alert in operational_alerts:
        title_key = str(alert.get("title") or "").strip()
        existing_index = alert_index_by_title.get(title_key)
        if existing_index is not None:
            existing = deduped_operational_alerts[existing_index]
            if alert.get("severity") == "danger" and existing.get("severity") != "danger":
                deduped_operational_alerts[existing_index] = alert
            continue
        alert_index_by_title[title_key] = len(deduped_operational_alerts)
        deduped_operational_alerts.append(alert)

    return {
        "integrity_alerts": integrity_alerts,
        "operational_alerts": deduped_operational_alerts,
    }


def _select_workbench_pending_recommendations(
    summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    pending_recommendations = [
        item for item in (summary.get("pending_recommendations") or [])
        if isinstance(item, dict) and item.get("status") == "draft"
    ]

    # Research can emit parameter_upgrade and keep_active for the same combo in
    # one round. Keep the same priority rule as the full items payload so the
    # overview count matches the detail view without building detail evidence.
    by_combo: dict[str, dict[str, Any]] = {}
    for rec in sorted(
        pending_recommendations,
        key=lambda item: (
            1 if item.get("recommendation_type") == "parameter_upgrade" else 0,
            _iso_sort_key(item.get("created_at")),
        ),
        reverse=True,
    ):
        combo_key = str(
            rec.get("combo_key")
            or _combo_key(rec.get("family"), rec.get("timeframe"))
        )
        if combo_key and combo_key not in by_combo:
            by_combo[combo_key] = rec
    return by_combo


def _count_workbench_integrity_blocked_items(
    pending_items_by_combo: dict[str, dict[str, Any]],
    alerts_payload: dict[str, Any],
) -> int:
    blocking_integrity = [
        alert for alert in (alerts_payload.get("integrity_alerts") or [])
        if bool(alert.get("blocks_approval"))
    ]
    if not blocking_integrity:
        return 0

    blocked_count = 0
    for combo_key in pending_items_by_combo:
        if any(
            not str(alert.get("combo_key") or "").strip()
            or str(alert.get("combo_key") or "").strip() == combo_key
            for alert in blocking_integrity
        ):
            blocked_count += 1
    return blocked_count


def _find_combo_summary(payload: dict[str, Any] | None, combo_key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for item in payload.get("combos") or []:
        if isinstance(item, dict) and item.get("combo_key") == combo_key:
            summary = item.get("summary")
            return summary if isinstance(summary, dict) else {}
    return {}


def _compact_metrics(raw_metrics: dict[str, Any], *keys: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key in keys:
        value = raw_metrics.get(key)
        if value in (None, "", []):
            continue
        metrics[key] = value
    return metrics


def _build_combo_evidence_digest(
    *,
    root: Path,
    summary: dict[str, Any],
    combo_key: str,
    combo_state: dict[str, Any],
    item_alerts: list[dict[str, Any]],
    phase3_payload: dict[str, Any],
    phase4_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    latest_research_conclusions = {
        item.get("combo_key"): item
        for item in (summary.get("latest_research_conclusions") or [])
        if isinstance(item, dict) and item.get("combo_key")
    }
    phase2 = latest_research_conclusions.get(combo_key) or {}
    phase2_signal = phase2.get("signal_summary") or {}
    phase2_blocked = any(alert.get("phase") == "phase2" for alert in item_alerts)

    phase3_combo = _find_combo_summary(phase3_payload, combo_key)
    phase4_combo = _find_combo_summary(phase4_payload, combo_key)
    phase3_incomplete = str(phase3_payload.get("incomplete_reason") or "").strip()
    phase4_incomplete = str(phase4_payload.get("incomplete_reason") or "").strip()
    readiness_blockers = _dedupe_texts(
        [str(item) for item in (combo_state.get("inconsistencies") or [])],
        limit=3,
    )

    # M7 修复：phase2 的 round_id 原本硬编码 None，evidence 追溯链断一环。
    # phase2 来自 latest_research_conclusions，每条结论自带 source_round_id；
    # 优先用它，退而用 phase2.round_id（部分来源字段不一致的兼容路径）。
    phase2_round_id = phase2.get("source_round_id") or phase2.get("round_id")
    return [
        {
            "phase": "phase2",
            "status": "blocked" if phase2_blocked else ("available" if phase2 else "missing"),
            "headline": (
                f"研究结论：{phase2.get('decision')}"
                if phase2.get("decision")
                else "当前没有可用的 Step2 研究结论"
            ),
            "metrics": _compact_metrics(
                phase2_signal,
                "experiments_with_openings",
                "max_opening_count",
                "mean_positive_edge_ratio",
            ),
            "round_id": phase2_round_id,
            "incomplete_reason": "manifest_missing_on_disk" if phase2_blocked else None,
        },
        {
            "phase": "phase3",
            "status": "incomplete" if phase3_incomplete else ("available" if phase3_combo else "missing"),
            "headline": (
                "归因结论可用"
                if phase3_combo
                else ("最新归因结果不完整" if phase3_incomplete else "当前没有可用的归因结论")
            ),
            "metrics": _compact_metrics(
                phase3_combo,
                "status",
                "failure_ratio",
                "failure_count",
                "total_count",
            ),
            "round_id": phase3_payload.get("round_id"),
            "incomplete_reason": phase3_incomplete or None,
        },
        {
            "phase": "phase4",
            "status": "incomplete" if phase4_incomplete else ("available" if phase4_combo else "missing"),
            "headline": (
                "执行评估可用"
                if phase4_combo
                else ("最新执行评估不完整" if phase4_incomplete else "当前没有可用的执行评估")
            ),
            "metrics": _compact_metrics(
                phase4_combo,
                "full_fill_ratio",
                "cost_adjusted_edge_proxy_bps",
                "mean_cost_bps",
            ),
            "round_id": phase4_payload.get("round_id"),
            "incomplete_reason": phase4_incomplete or None,
        },
        {
            "phase": "readiness",
            "status": "blocked" if readiness_blockers else "available",
            "headline": (
                "当前组合存在治理或一致性风险"
                if readiness_blockers
                else "当前组合没有新增的 readiness 阻断"
            ),
            "metrics": _compact_metrics(
                {
                    "decision_status": combo_state.get("decision_status"),
                    "runtime_source": combo_state.get("runtime_source"),
                },
                "decision_status",
                "runtime_source",
            ),
            "round_id": None,
            "incomplete_reason": readiness_blockers[0] if readiness_blockers else None,
        },
    ]


def _build_item_detail_payload(item: dict[str, Any]) -> dict[str, Any]:
    recommendation_type = str(item.get("recommendation_type") or "require_review")
    return {
        "current_recommendation_reason": item.get("decision_summary"),
        "risk_summary": _dedupe_texts(
            list(item.get("reason_summary") or []) + list(item.get("blocking_flags") or []),
            limit=4,
        ),
        "next_state_if_approved": _approval_effect_summary_v2(recommendation_type),
        "next_state_if_rejected": "保留当前运行参数，并把这条建议标记为拒绝。",
        "source_rounds": item.get("source_rounds") or {},
        "integrity_status": item.get("integrity_status"),
        "integrity_alerts": item.get("integrity_alerts") or [],
    }


def _build_workbench_items_payload(
    root: Path,
    summary: dict[str, Any],
    alerts_payload: dict[str, Any],
    *,
    phase_payloads: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    governance_state = summary.get("governance_state") or {}
    combo_states = {
        item.get("combo_key"): item
        for item in (governance_state.get("combo_states") or [])
        if isinstance(item, dict) and item.get("combo_key")
    }
    by_combo = _select_workbench_pending_recommendations(summary)

    blocking_integrity = [
        alert for alert in (alerts_payload.get("integrity_alerts") or [])
        if bool(alert.get("blocks_approval"))
    ]
    # H6 修复：query_latest_attribution / query_latest_execution_realism 读 DB
    # 或文件，一旦抖动原来会把 /auth/dashboard/bundle 打成 500。这里降级为
    # unavailable payload，并叠加一条 blocking alert，让下游门禁继续阻止审批。
    phase_payloads = phase_payloads or _load_workbench_phase_payloads(root)
    phase3_payload = phase_payloads.get("phase3", {})
    phase4_payload = phase_payloads.get("phase4", {})

    items: list[dict[str, Any]] = []
    for combo_key, rec in by_combo.items():
        combo_state = combo_states.get(combo_key) or {}
        item_alerts = [
            alert for alert in blocking_integrity
            if not alert.get("combo_key") or alert.get("combo_key") == combo_key
        ]
        integrity_status = "blocked" if item_alerts else "complete"
        reason_summary = _build_reason_summary(rec, combo_state)
        missing_evidence = _dedupe_texts(
            [str(alert.get("title") or "") for alert in item_alerts],
            limit=3,
        )
        recommendation_type = str(rec.get("recommendation_type") or "require_review")
        headline = _WORKBENCH_RECOMMENDATION_LABELS.get(
            recommendation_type,
            "当前组合需要处理",
        )
        decision_summary = _decision_summary_v2(
            recommendation_type,
            has_integrity_block=bool(item_alerts),
            combo_state=combo_state,
        )
        disabled_reason = (
            "当前轮次存在不完整证据，需先补齐研究/归因/执行结论。"
            if item_alerts
            else None
        )
        evidence_digest = _build_combo_evidence_digest(
            root=root,
            summary=summary,
            combo_key=combo_key,
            combo_state=combo_state,
            item_alerts=item_alerts,
            phase3_payload=phase3_payload,
            phase4_payload=phase4_payload,
        )
        actions = [
            _make_ui_action(
                key="approve",
                label=_approval_action_label_v2(recommendation_type),
                ui_action="rdp-approve-only",
                value=str(rec.get("recommendation_id") or ""),
                enabled=not item_alerts,
                disabled_reason=disabled_reason,
            ),
            _make_ui_action(
                key="reject",
                label=_reject_action_label_v2(recommendation_type),
                ui_action="rdp-reject-recommendation",
                value=str(rec.get("recommendation_id") or ""),
                enabled=True,
            ),
        ]
        items.append({
            "combo_key": combo_key,
            "family": rec.get("family") or combo_state.get("family"),
            "timeframe": rec.get("timeframe") or combo_state.get("timeframe"),
            "recommendation_id": rec.get("recommendation_id"),
            "recommendation_type": recommendation_type,
            "confidence": rec.get("confidence") or "unknown",
            "status": rec.get("status") or "draft",
            "headline": headline,
            "decision_summary": decision_summary,
            "approval_effect_summary": _approval_effect_summary_v2(recommendation_type),
            "reason_summary": reason_summary,
            "missing_evidence": missing_evidence,
            "blocking_flags": [
                _humanize_reason_entry(str(item)) or str(item)
                for item in (combo_state.get("inconsistencies") or [])
            ],
            "integrity_status": integrity_status,
            "integrity_alerts": item_alerts,
            "approval_enabled": not item_alerts,
            "approval_blocked_reason": disabled_reason,
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("created_at"),
            "source_rounds": {
                "phase2_round_id": rec.get("source_round_id"),
                "phase3_round_id": phase3_payload.get("round_id"),
                "phase4_round_id": phase4_payload.get("round_id"),
            },
            "detail_summary": None,
            "evidence_digest": evidence_digest,
            # L4 修复：combo_key 可能包含 `/` 或 `:` 等字符（历史上大多是
            # `family_timeframe` 形式，但 combo_key 的生成器并不约束合法字符集），
            # 直接拼进 URL 会破坏路径解析或被下游误路由。用 urlencode 的 `safe=""`
            # 参数保证把所有非字母数字字符都转义。
            "detail_url": f"/rdp/workbench/items/{_url_quote(str(combo_key), safe='')}",
            "evidence_url": f"/rdp/workbench/evidence/{_url_quote(str(combo_key), safe='')}",
            "actions": actions,
        })

    for item in items:
        item["detail_summary"] = _build_item_detail_payload(item)

    return {
        "total": len(items),
        "items": items,
    }


def _build_tuning_overview_payload(root: Path) -> dict[str, Any]:
    try:
        from aats.data_platform.operations.strategy_tuning_registry import (
            load_strategy_tuning_overrides,
            load_strategy_tuning_registry,
        )

        registry = load_strategy_tuning_registry(root)
        overrides = load_strategy_tuning_overrides(root)
    except Exception as exc:
        logger.warning("tuning overview: failed to load registry: %s", exc)
        registry = {"proposals": []}
        overrides = {"combo_overrides": {}}

    # 与 proposals / alerts 两处保持一致：当前 Step2 快照不完整时不能让 overview
    # 读起来像"可以放心点批准"。headline 显式告警，approvable_count 拉到 0。
    step2_incomplete_reason: str | None = None
    try:
        from aats.data_platform.governance.snapshot_db import (
            ROUND_PHASE_STEP2,
            is_snapshot_incomplete,
            load_latest_research_round_snapshot,
        )

        step2_snapshot = load_latest_research_round_snapshot(
            phase=ROUND_PHASE_STEP2,
            project_root=root,
        )
        if is_snapshot_incomplete(step2_snapshot):
            step2_incomplete_reason = "manifest_missing_on_disk"
    except Exception as exc:
        logger.warning("tuning overview: failed to inspect step2 snapshot: %s", exc)

    proposals = [
        item for item in (registry.get("proposals") or [])
        if isinstance(item, dict)
    ]
    pending_review = [item for item in proposals if item.get("status") == "pending_review"]
    approved = [item for item in proposals if item.get("status") == "approved"]
    active_overrides = overrides.get("combo_overrides") or {}

    if step2_incomplete_reason:
        headline = (
            f"当前 Step2 研究快照不完整，{len(pending_review)} 条调优提案暂不能批准。"
            if pending_review
            else "当前 Step2 研究快照不完整，暂不能处理调优提案。"
        )
    elif pending_review:
        headline = f"当前有 {len(pending_review)} 条自动调优提案待审核。"
    elif approved:
        headline = f"已有 {len(approved)} 条调优提案获批，正在影响后续 research 默认值。"
    else:
        headline = "当前没有待审核调优提案。"

    return {
        "pending_review_count": len(pending_review),
        "approvable_count": 0 if step2_incomplete_reason else len(pending_review),
        "approved_count": len(approved),
        "active_override_count": len(active_overrides),
        "headline": headline,
        "step2_incomplete_reason": step2_incomplete_reason,
    }


def _build_tuning_proposals_payload(root: Path) -> dict[str, Any]:
    try:
        from aats.data_platform.operations.strategy_tuning_registry import (
            load_strategy_tuning_registry,
        )

        registry = load_strategy_tuning_registry(root)
    except Exception as exc:
        logger.warning("tuning proposals: failed to load registry: %s", exc)
        registry = {"proposals": []}

    # Step2 当前快照不完整时，不能让运营者按现有 proposal 继续走审批：哪怕提案
    # 自身是历史完整数据产出的，当前数据链不健康也意味着"立即应用"的影响不可控。
    # 与 _build_workbench_alerts_payload 共用同一判定，保持 UI 两处信号一致：
    # alerts 板块会亮红告警，这里则把 proposal 的 actions 禁用、integrity_status 改
    # 为 blocked，同时保留 proposal 可见，让运营者清楚知道"有历史调优提案在排队，
    # 但当前数据不完整不能点批准"。
    step2_incomplete_reason: str | None = None
    step2_incomplete_alert: dict[str, Any] | None = None
    try:
        from aats.data_platform.governance.snapshot_db import (
            ROUND_PHASE_STEP2,
            is_snapshot_incomplete,
            load_latest_research_round_snapshot,
        )

        step2_snapshot = load_latest_research_round_snapshot(
            phase=ROUND_PHASE_STEP2,
            project_root=root,
        )
        if is_snapshot_incomplete(step2_snapshot):
            step2_incomplete_reason = "manifest_missing_on_disk"
            step2_incomplete_alert = {
                "code": "step2_manifest_missing",
                "severity": "danger",
                "scope": "round",
                "phase": "phase2",
                "title": "Step2 研究快照不完整",
                "message": "最新 Step2 目录缺少 round_manifest，不能据此批准调优提案。",
                "blocks_approval": True,
            }
    except Exception as exc:
        logger.warning("tuning proposals: failed to inspect step2 snapshot: %s", exc)

    proposals = sorted(
        [
            item for item in (registry.get("proposals") or [])
            if isinstance(item, dict) and item.get("status") == "pending_review"
        ],
        key=lambda item: _iso_sort_key(item.get("created_at")),
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for item in proposals[:8]:
        approval_enabled = step2_incomplete_reason is None
        disabled_reason = (
            "当前 Step2 研究快照不完整，暂不能批准调优提案。"
            if not approval_enabled
            else None
        )
        integrity_alerts = [step2_incomplete_alert] if step2_incomplete_alert else []
        items.append({
            "proposal_id": item.get("proposal_id"),
            "combo_key": item.get("combo_key"),
            "family": item.get("family"),
            "timeframe": item.get("timeframe"),
            "status": item.get("status"),
            "headline": f"建议调整 {item.get('parameter')}",
            "proposed_changes": [
                {
                    "key": item.get("parameter"),
                    "from": item.get("current_value"),
                    "to": item.get("proposed_value"),
                },
            ],
            "reason_summary": _dedupe_texts(
                _split_reason_text(item.get("rationale")),
                limit=3,
            ),
            "impact_scope": ["research", "replay", "scan", "step3"],
            "integrity_status": "complete" if approval_enabled else "blocked",
            "integrity_alerts": integrity_alerts,
            "approval_enabled": approval_enabled,
            "approval_blocked_reason": disabled_reason,
            "created_at": item.get("created_at"),
            "actions": [
                _make_ui_action(
                    key="approve_tuning",
                    label="批准调优",
                    ui_action="rdp-approve-tuning-proposal",
                    value=str(item.get("proposal_id") or ""),
                    enabled=approval_enabled,
                    disabled_reason=disabled_reason,
                ),
                _make_ui_action(
                    key="reject_tuning",
                    label="拒绝调优",
                    ui_action="rdp-reject-tuning-proposal",
                    value=str(item.get("proposal_id") or ""),
                ),
            ],
        })

    return {
        "total": len(proposals),
        "items": items,
        "step2_incomplete_reason": step2_incomplete_reason,
    }


def _build_rdp_workbench_overview_payload(
    root: Path,
    summary: dict[str, Any],
    *,
    alerts_payload: dict[str, Any] | None = None,
    tuning_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alerts_payload = alerts_payload or _build_workbench_alerts_payload(root, summary)
    pending_items_by_combo = _select_workbench_pending_recommendations(summary)
    pending_item_count = len(pending_items_by_combo)
    integrity_blocked_item_count = _count_workbench_integrity_blocked_items(
        pending_items_by_combo,
        alerts_payload,
    )
    release_candidates_payload = _build_release_candidates_payload(summary)
    tuning_payload = tuning_payload or _build_tuning_overview_payload(root)
    current_execution, next_queue = _build_task_lane_summary(summary.get("tasks") or {})
    primary_action, secondary_actions = _build_manual_workflow_actions(root, summary.get("tasks") or {})
    observation_queue = summary.get("observation_queue") or []
    operations_summary = summary.get("operations_summary") or {}

    overall_status = "idle"
    if any(item.get("observation_status") == "rollback_recommended" for item in observation_queue):
        overall_status = "rollback_required"
    elif pending_item_count:
        overall_status = "needs_approval"
    elif operations_summary.get("approved_release_candidate_count"):
        overall_status = "ready_to_release"
    elif operations_summary.get("observing_release_count"):
        overall_status = "observing"
    elif alerts_payload.get("integrity_alerts"):
        overall_status = "needs_more_research"

    headline = "当前没有新的待处理项。"
    subheadline = "需要时可以刷新数据，或重跑完整 RDP。"
    if overall_status == "rollback_required":
        headline = "当前有发布进入回滚建议，先处理回滚。"
        subheadline = "回滚完成后，再继续研究和审批。"
    elif overall_status == "needs_approval":
        headline = f"当前有 {pending_item_count} 个组合待处理。"
        subheadline = "先看结论，再决定是否批准。"
    elif overall_status == "ready_to_release":
        headline = f"已有 {release_candidates_payload['total']} 个参数候选待发布。"
        subheadline = "先运行 Gate，再决定是否创建发布。"
    elif overall_status == "observing":
        headline = "当前有发布仍在观察窗口中。"
        subheadline = "先确认观察结果，再推进下一轮。"
    elif overall_status == "needs_more_research":
        headline = "当前轮次证据不完整，暂时不能审批。"
        subheadline = "先补研究、归因或执行评估，再继续治理。"

    return {
        "round_id": None,
        "overall_status": overall_status,
        "headline": headline,
        "subheadline": subheadline,
        "primary_action": primary_action,
        "secondary_actions": secondary_actions,
        "blockers": alerts_payload.get("integrity_alerts") or [],
        "summary_counts": {
            "pending_items": pending_item_count,
            "ready_release_items": release_candidates_payload["total"],
            "integrity_blocked_items": integrity_blocked_item_count,
            "observing_releases": int(operations_summary.get("observing_release_count") or 0),
            "tuning_pending": int(tuning_payload.get("pending_review_count") or 0),
        },
        "current_execution": current_execution,
        "next_queue": next_queue,
        "health": {
            "daemon": summary.get("health", {}).get("overall_health"),
            "governance_db": "healthy" if not any(
                check.get("category") == "governance_db" and check.get("status") == "blocked"
                for check in (summary.get("health", {}).get("checks") or [])
            ) else "blocked",
            "latest_gate": operations_summary.get("latest_gate_status") or "not_run",
        },
    }


def build_rdp_workbench_overview(request: Request) -> dict[str, Any]:
    root = _project_root(request)
    summary = build_rdp_control_summary(request)
    return _build_rdp_workbench_overview_payload(root, summary)


def build_rdp_workbench_bundle(
    request: Request,
    *,
    control_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从同一份控制摘要派生 V3 工作台的研究、治理与调优读模型。"""
    root = _project_root(request)
    summary = control_summary if isinstance(control_summary, dict) else build_rdp_control_summary(request)
    phase_payloads = _load_workbench_phase_payloads(root)
    alerts = _build_workbench_alerts_payload(
        root,
        summary,
        phase_payloads=phase_payloads,
    )
    tuning_overview = _build_tuning_overview_payload(root)
    workbench = _build_workbench_items_payload(
        root,
        summary,
        alerts,
        phase_payloads=phase_payloads,
    )
    workbench["release_candidates"] = _build_release_candidates_payload(summary)
    return {
        "overview": _build_rdp_workbench_overview_payload(
            root,
            summary,
            alerts_payload=alerts,
            tuning_payload=tuning_overview,
        ),
        "workbench": workbench,
        "alerts": alerts,
        "tuning_overview": tuning_overview,
        "tuning_proposals": _build_tuning_proposals_payload(root),
    }


def build_rdp_workbench_items(request: Request) -> dict[str, Any]:
    root = _project_root(request)
    summary = build_rdp_control_summary(request)
    alerts_payload = _build_workbench_alerts_payload(root, summary)
    payload = _build_workbench_items_payload(root, summary, alerts_payload)
    payload["release_candidates"] = _build_release_candidates_payload(summary)
    return payload


def build_rdp_workbench_alerts(request: Request) -> dict[str, Any]:
    root = _project_root(request)
    summary = build_rdp_control_summary(request)
    return _build_workbench_alerts_payload(root, summary)


def build_rdp_tuning_overview(request: Request) -> dict[str, Any]:
    root = _project_root(request)
    return _build_tuning_overview_payload(root)


def build_rdp_tuning_proposals(request: Request) -> dict[str, Any]:
    root = _project_root(request)
    return _build_tuning_proposals_payload(root)


def build_rdp_workbench_item_detail(request: Request, combo_key: str) -> dict[str, Any]:
    root = _project_root(request)
    summary = build_rdp_control_summary(request)
    alerts_payload = _build_workbench_alerts_payload(root, summary)
    payload = _build_workbench_items_payload(root, summary, alerts_payload)
    item = next(
        (entry for entry in (payload.get("items") or []) if entry.get("combo_key") == combo_key),
        None,
    )
    return {
        "available": bool(item),
        "combo_key": combo_key,
        "item": item,
        "detail_summary": item.get("detail_summary") if item else {},
        "evidence_digest": item.get("evidence_digest") if item else [],
        "source_rounds": item.get("source_rounds") if item else {},
    }


def build_rdp_workbench_item_evidence(request: Request, combo_key: str) -> dict[str, Any]:
    detail = build_rdp_workbench_item_detail(request, combo_key)
    item = detail.get("item") or {}
    evidence_by_phase = {
        str(entry.get("phase")): entry
        for entry in (item.get("evidence_digest") or [])
        if isinstance(entry, dict) and entry.get("phase")
    }
    return {
        "available": bool(item),
        "combo_key": combo_key,
        "integrity_status": item.get("integrity_status", "missing"),
        "integrity_alerts": item.get("integrity_alerts") or [],
        "evidence_digest": item.get("evidence_digest") or [],
        "phase2": evidence_by_phase.get("phase2"),
        "phase3": evidence_by_phase.get("phase3"),
        "phase4": evidence_by_phase.get("phase4"),
        "readiness": evidence_by_phase.get("readiness"),
        "source_rounds": item.get("source_rounds") or {},
        "detail_summary": item.get("detail_summary") or {},
    }
