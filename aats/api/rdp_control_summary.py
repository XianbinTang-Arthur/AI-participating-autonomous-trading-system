from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request

from aats.data_platform.operations.environment_guard import (
    get_current_environment,
    get_observation_window_hours,
    get_policy,
    production_parameter_apply_enabled,
)
from aats.services.operator.rdp_queries import query_rdp_health
from aats.services.operator.rdp_queries import (
    query_active_parameter_sets,
    query_latest_decision_round,
    query_latest_decisions,
    query_latest_recommendations,
    query_parameter_registry,
)

logger = logging.getLogger(__name__)

_governance_engine_cache: dict[str, Any] = {}
_PENDING_RECOMMENDATION_STATUSES = {"draft", "approved"}


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


def _governance_db_url() -> str | None:
    url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if url:
        return url
    try:
        from aats.data_platform.config import get_settings as get_rdp_settings

        return get_rdp_settings().database_url
    except Exception:
        return None


def _get_governance_engine(url: str) -> Any:
    from sqlalchemy import create_engine

    cached = _governance_engine_cache.get(url)
    if cached is not None:
        return cached
    engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1)
    _governance_engine_cache[url] = engine
    return engine


@contextlib.contextmanager
def _governance_session() -> Iterator[Any]:
    url = _governance_db_url()
    if not url:
        raise RuntimeError(
            "No governance DB URL available "
            "(AATS_ACTIVE_PARAMETER_DB_URL / RDP_DATABASE_URL)",
        )

    from sqlalchemy.orm import sessionmaker

    engine = _get_governance_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _combo_key(family: str | None, timeframe: str | None) -> str:
    if not family or not timeframe:
        return ""
    return f"{family}_{str(timeframe).lower()}"


def _iso_sort_key(value: str | None) -> str:
    return value or ""


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None


def _load_recent_gate_results(project_root: Path, *, limit: int = 8) -> list[dict[str, Any]]:
    gates_root = project_root / "artifacts" / "production_workflow" / "gates"
    if not gates_root.exists():
        return []

    results: list[dict[str, Any]] = []
    for path in gates_root.glob("*/pre_apply_gate_result.json"):
        payload = _safe_load_json(path)
        if not isinstance(payload, dict):
            continue
        results.append({
            "gate_run_id": payload.get("gate_run_id"),
            "recommendation_id": payload.get("recommendation_id"),
            "created_at": payload.get("created_at"),
            "gate_status": payload.get("gate_status"),
            "allow_apply": bool(payload.get("allow_apply")),
            "blocking_reasons": payload.get("blocking_reasons") or [],
            "warnings": payload.get("warnings") or [],
            "checks": payload.get("checks") or [],
        })

    results.sort(key=lambda item: _iso_sort_key(item.get("created_at")), reverse=True)
    return results[:limit]


def _load_recent_releases(project_root: Path, *, limit: int = 10) -> list[dict[str, Any]]:
    from aats.data_platform.production_workflow.release_registry import load_release_history

    history = load_release_history(project_root)
    releases = [
        item for item in (history.get("releases") or [])
        if isinstance(item, dict)
    ]
    releases.sort(key=lambda item: _iso_sort_key(item.get("created_at")), reverse=True)
    return releases[:limit]


def _build_observation_queue(
    project_root: Path,
    *,
    releases: list[dict[str, Any]],
    active_parameters: dict[str, Any],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []

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

    for release in releases:
        release_id = str(release.get("release_id") or "").strip()
        if not release_id:
            continue
        observation_status = str(release.get("observation_status") or "unknown")
        if observation_status not in {"observing", "rollback_recommended", "completed", "not_started"}:
            continue

        observation_path = (
            project_root
            / "artifacts"
            / "production_workflow"
            / "observations"
            / release_id
            / "observation_summary.json"
        )
        observation = _safe_load_json(observation_path)
        if not isinstance(observation, dict):
            observation = {}

        combo_key = str(release.get("combo_key") or "").strip()
        active_entry = active_parameters.get(combo_key) if combo_key else None
        current_active_parameter_set_id = None
        if isinstance(active_entry, dict):
            current_active_parameter_set_id = active_entry.get("parameter_set_id")

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
            "is_current_active_release": (
                bool(current_active_parameter_set_id)
                and current_active_parameter_set_id == release.get("parameter_set_id")
            ),
        })

    queue.sort(
        key=lambda item: (
            {"rollback_recommended": 0, "observing": 1, "completed": 2, "not_started": 3}.get(
                str(item.get("observation_status") or ""), 9,
            ),
            -(
                _parse_iso_datetime(item.get("created_at")).timestamp()
                if _parse_iso_datetime(item.get("created_at")) is not None
                else 0
            ),
        ),
        reverse=False,
    )
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
        "production_apply_enabled": production_parameter_apply_enabled(env),
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
    root = _project_root(request)
    environment = _environment_summary()

    tasks_by_workflow: dict[str, Any] = {}
    tasks_error: str | None = None
    try:
        from aats.data_platform.governance.rdp_task_db import db_get_recent_tasks

        with _governance_session() as session:
            recent = db_get_recent_tasks(session, limit=20)
        for task in recent:
            workflow = task["workflow"]
            if workflow not in tasks_by_workflow:
                tasks_by_workflow[workflow] = task
    except Exception as exc:
        logger.warning("control-summary: task query failed: %s", exc)
        tasks_error = str(exc)

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
    pending_recommendations = [
        rec
        for rec in recommendations
        if rec.get("status") in _PENDING_RECOMMENDATION_STATUSES
    ]

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

    latest_round_summary: dict[str, Any] = {"available": False}
    latest_research_conclusions: list[dict[str, Any]] = []
    round_upgrade_candidates: list[dict[str, Any]] = []
    try:
        decision_round = query_latest_decision_round(root)
        if decision_round.get("available"):
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
            readiness = decision_round.get("promotion_readiness_assessment") or {}
            latest_round_summary = {
                "available": True,
                "round_id": decision_round.get("round_id"),
                "candidate_count": len(round_upgrade_candidates),
                "conclusion_count": len(latest_research_conclusions),
                "has_conclusion_report": bool(
                    decision_round.get("has_conclusion_report"),
                ),
                "readiness_status": readiness.get("overall_status"),
            }
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
    runtime_parameter_source = {
        "mode": governance_state.get("parameter_source_mode", "profile_defaults"),
        "active_count": len(active_parameters),
        "governance_managed": governance_state.get("governance_managed", False),
        "paused_combos": governance_state.get("paused_combos", []),
        "combo_count": len(governance_state.get("combo_states", [])),
    }
    health = query_rdp_health(root)
    recent_gate_results = _load_recent_gate_results(root)
    recent_releases = _load_recent_releases(root)
    observation_queue = _build_observation_queue(
        root,
        releases=recent_releases,
        active_parameters=active_parameters,
    )
    recommendations_sorted = sorted(
        [item for item in recommendations if isinstance(item, dict)],
        key=lambda item: _iso_sort_key(item.get("created_at")),
        reverse=True,
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
    operations_summary = {
        "approved_release_candidate_count": len(approved_parameter_recommendations),
        "draft_recommendation_count": sum(
            1 for rec in pending_recommendations if rec.get("status") == "draft"
        ),
        "draft_parameter_recommendation_count": len(draft_parameter_recommendations),
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

    return {
        "environment": environment,
        "health": health,
        "operations_summary": operations_summary,
        "tasks": tasks_by_workflow,
        "tasks_error": tasks_error,
        "pending_recommendations": pending_recommendations,
        "recommendation_history": recommendations_sorted[:48],
        "active_parameters": active_parameters,
        "runtime_parameter_source": runtime_parameter_source,
        "latest_round_summary": latest_round_summary,
        "latest_research_conclusions": latest_research_conclusions,
        "latest_decision_state": latest_decision_state,
        "governance_state": governance_state,
        "recent_gate_results": recent_gate_results,
        "recent_releases": recent_releases,
        "observation_queue": observation_queue,
    }
