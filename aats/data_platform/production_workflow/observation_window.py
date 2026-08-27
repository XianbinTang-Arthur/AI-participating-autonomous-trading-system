"""Observation Window management."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
    try_governance_db,
)
from aats.data_platform.governance._exceptions import DBUnavailableError
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_PHASE3,
    ROUND_PHASE_PHASE4,
    SNAPSHOT_QUALITY_MONITOR,
    load_governance_snapshot,
    load_latest_research_round_snapshot,
)
from aats.data_platform.production_workflow.evidence_metrics import finite_metric
from aats.data_platform.production_workflow.post_apply_evidence import (
    POST_APPLY_EVIDENCE_CONTRACT_VERSION,
    collect_source_provenance,
    make_source_provenance,
)

log = logging.getLogger(__name__)

_OBSERVATIONS_DIR = "artifacts/production_workflow/observations"


def _combo_key(family: str, timeframe: str) -> str:
    return f"{family}_{timeframe.lower()}"


def _with_source(
    result: dict[str, Any],
    *,
    source_kind: str,
    source_id: str,
    source_timestamp: str | datetime,
    source_payload: dict[str, Any],
    source_phase: str | None = None,
    source_family: str | None = None,
    source_timeframe: str | None = None,
) -> dict[str, Any]:
    try:
        result["source_provenance"] = make_source_provenance(
            source_kind=source_kind,
            source_id=source_id,
            source_timestamp=source_timestamp,
            source_payload=source_payload,
            source_phase=source_phase,
            source_family=source_family,
            source_timeframe=source_timeframe,
        )
    except (TypeError, ValueError):
        return {
            "name": result.get("name", "unknown"),
            "status": "unknown",
            "detail": "canonical source provenance could not be constructed",
            "severity": "none",
        }
    return result


def _load_combo_round_summary(
    project_root: Path,
    *,
    phase: str,
    combo_key: str,
    summary_key: str,
    not_before: datetime,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    snapshot = load_latest_research_round_snapshot(
        phase=phase,
        project_root=project_root,
        require_managed_db_truth=True,
    )
    if not isinstance(snapshot, dict):
        return None, None, None
    try:
        from aats.data_platform.governance._time_util import (
            parse_iso_datetime_utc,
        )

        finished_at = parse_iso_datetime_utc(
            snapshot.get("finished_at"),
            context=f"observation_window.{phase}.finished_at",
        )
    except (TypeError, ValueError):
        return None, None, None
    if (
        snapshot.get("status") != "succeeded"
        or finished_at is None
        or finished_at < not_before
        or finished_at > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        return None, None, None
    snapshot_summary = snapshot.get("summary")
    if not isinstance(snapshot_summary, dict):
        return snapshot, None, None
    combos = snapshot_summary.get("combos")
    if not isinstance(combos, dict):
        return snapshot, None, None
    combo = combos.get(combo_key)
    if not isinstance(combo, dict):
        return snapshot, None, None
    summary = combo.get(summary_key)
    if not isinstance(summary, dict):
        return snapshot, combo, None
    return snapshot, combo, summary


def load_observation_result(project_root: Path, release_id: str) -> dict[str, Any] | None:
    managed_truth = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_get_observation_result,
            )

            with Session(engine) as session:
                result = db_get_observation_result(session, release_id)
            if result is not None:
                return result
        except Exception as exc:
            if managed_truth:
                raise DBUnavailableError(
                    "managed observation read failed; stale file fallback denied"
                ) from exc
            log.warning("failed to load observation from DB (%s)", type(exc).__name__)
        finally:
            if engine is not None:
                engine.dispose()
        if managed_truth:
            return None
    elif managed_truth:
        raise DBUnavailableError(
            "managed observation unavailable; stale file fallback denied"
        )

    summary_path = project_root / _OBSERVATIONS_DIR / release_id / "observation_summary.json"
    if not summary_path.exists():
        return None
    try:
        with summary_path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _check_quality_monitor_regression(
    project_root: Path,
    *,
    not_before: datetime,
) -> dict[str, Any]:
    qm = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_QUALITY_MONITOR,
        require_managed_db_truth=True,
    )
    if not isinstance(qm, dict):
        return {"name": "quality_monitor", "status": "unknown", "detail": "missing quality monitor snapshot"}

    try:
        from aats.data_platform.governance._time_util import (
            parse_iso_datetime_utc,
        )

        generated_at = parse_iso_datetime_utc(
            qm.get("generated_at"),
            context="observation_window.quality_monitor.generated_at",
        )
    except (TypeError, ValueError):
        generated_at = None
    if (
        generated_at is None
        or generated_at < not_before
        or generated_at > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        return {
            "name": "quality_monitor",
            "status": "unknown",
            "detail": "quality monitor evidence is missing, stale, or invalid",
            "severity": "none",
        }

    summary = qm.get("summary")
    if not isinstance(summary, dict):
        return {
            "name": "quality_monitor",
            "status": "unknown",
            "detail": "quality monitor summary is malformed",
            "severity": "none",
        }
    health = summary.get("health")
    critical = summary.get("critical_failures")
    if (
        health not in {"healthy", "degraded", "unhealthy"}
        or type(critical) is not int
        or critical < 0
    ):
        return {
            "name": "quality_monitor",
            "status": "unknown",
            "detail": "quality monitor health/count contract is invalid",
            "severity": "none",
        }

    if health == "unhealthy" or critical > 0:
        return _with_source({
            "name": "quality_monitor",
            "status": "regression",
            "detail": f"health={health}, critical={critical}",
            "severity": "high",
        }, source_kind="governance_snapshot",
            source_id=f"{SNAPSHOT_QUALITY_MONITOR}:{generated_at.isoformat()}",
            source_timestamp=generated_at,
            source_payload={
                "snapshot_type": SNAPSHOT_QUALITY_MONITOR,
                "generated_at": generated_at.isoformat(),
                "health": health,
                "critical_failures": critical,
            })
    if health == "degraded":
        return _with_source({
            "name": "quality_monitor",
            "status": "warn",
            "detail": "health=degraded",
            "severity": "medium",
        }, source_kind="governance_snapshot",
            source_id=f"{SNAPSHOT_QUALITY_MONITOR}:{generated_at.isoformat()}",
            source_timestamp=generated_at,
            source_payload={
                "snapshot_type": SNAPSHOT_QUALITY_MONITOR,
                "generated_at": generated_at.isoformat(),
                "health": health,
                "critical_failures": critical,
            })
    return _with_source({
        "name": "quality_monitor",
        "status": "ok",
        "detail": f"health={health}",
        "severity": "none",
    }, source_kind="governance_snapshot",
        source_id=f"{SNAPSHOT_QUALITY_MONITOR}:{generated_at.isoformat()}",
        source_timestamp=generated_at,
        source_payload={
            "snapshot_type": SNAPSHOT_QUALITY_MONITOR,
            "generated_at": generated_at.isoformat(),
            "health": health,
            "critical_failures": critical,
        })


def _check_decision_regression(
    project_root: Path,
    family: str,
    timeframe: str,
    *,
    not_before: datetime,
) -> dict[str, Any]:
    try:
        from aats.data_platform.decision_system.recommendation_registry import (
            load_active_decision_registry,
        )

        reg = load_active_decision_registry(
            project_root / "artifacts/decision_system/active_decision_registry.json",
        )
    except Exception:
        return {"name": "decision_status", "status": "unknown", "detail": "unable to read decision registry"}

    combo_key = _combo_key(family, timeframe)
    for decision in reg.get("decisions", []):
        if decision.get("combo_key") == combo_key or (
            decision.get("family") == family
            and str(decision.get("timeframe", "")).lower() == timeframe.lower()
        ):
            try:
                from aats.data_platform.governance._time_util import (
                    parse_iso_datetime_utc,
                )

                last_updated_at = parse_iso_datetime_utc(
                    decision.get("last_updated_at"),
                    context="observation_window.active_decision.last_updated_at",
                )
            except (TypeError, ValueError):
                last_updated_at = None
            if (
                last_updated_at is None
                or last_updated_at < not_before
                or last_updated_at > datetime.now(timezone.utc) + timedelta(minutes=5)
            ):
                return {
                    "name": "decision_status",
                    "status": "unknown",
                    "detail": "active decision evidence is missing, stale, or invalid",
                    "severity": "none",
                }
            status = decision.get("current_status", "unknown")
            source_kwargs = {
                "source_kind": "active_decision",
                "source_id": str(
                    decision.get("last_recommendation_id") or combo_key
                ),
                "source_timestamp": last_updated_at,
                "source_family": family,
                "source_timeframe": timeframe,
                "source_payload": {
                    "family": family,
                    "timeframe": timeframe.lower(),
                    "combo_key": combo_key,
                    "current_status": status,
                    "active_parameter_set_id": decision.get(
                        "active_parameter_set_id"
                    ),
                    "last_recommendation_id": decision.get(
                        "last_recommendation_id"
                    ),
                    "last_updated_at": last_updated_at.isoformat(),
                },
            }
            if status == "pause":
                return _with_source({
                    "name": "decision_status",
                    "status": "regression",
                    "detail": f"{combo_key} status=pause",
                    "severity": "high",
                }, **source_kwargs)
            if status == "require_review":
                return _with_source({
                    "name": "decision_status",
                    "status": "warn",
                    "detail": f"{combo_key} status=require_review",
                    "severity": "medium",
                }, **source_kwargs)
            if status not in {"keep_active", "lower_priority"}:
                return {
                    "name": "decision_status",
                    "status": "unknown",
                    "detail": f"{combo_key} status={status}",
                    "severity": "none",
                }
            return _with_source({
                "name": "decision_status",
                "status": "ok",
                "detail": f"{combo_key} status={status}",
                "severity": "none",
            }, **source_kwargs)

    return {
        "name": "decision_status",
        "status": "unknown",
        "detail": "no decision record",
        "severity": "none",
    }


def _check_attribution_regression(
    project_root: Path,
    family: str,
    timeframe: str,
    *,
    not_before: datetime,
) -> dict[str, Any]:
    combo_key = _combo_key(family, timeframe)
    snapshot, combo, summary = _load_combo_round_summary(
        project_root,
        phase=ROUND_PHASE_PHASE3,
        combo_key=combo_key,
        summary_key="attribution_summary",
        not_before=not_before,
    )
    if snapshot is not None and summary is not None:
        strategy_failure_pct = finite_metric(
            summary.get("strategy_failure_pct"), minimum=0, maximum=100
        )
        risk_failure_pct = finite_metric(
            summary.get("risk_failure_pct"), minimum=0, maximum=100
        )
        execution_failure_pct = finite_metric(
            summary.get("execution_failure_pct"), minimum=0, maximum=100
        )
        if None in {
            strategy_failure_pct,
            risk_failure_pct,
            execution_failure_pct,
        }:
            return {
                "name": "attribution",
                "status": "unknown",
                "detail": (
                    f"{combo_key} attribution percentages must be finite "
                    "numbers in [0, 100]"
                ),
                "severity": "none",
            }
        assert strategy_failure_pct is not None
        assert risk_failure_pct is not None
        assert execution_failure_pct is not None
        total_failure = strategy_failure_pct + risk_failure_pct + execution_failure_pct
        if total_failure > 80:
            return _with_source({
                "name": "attribution",
                "status": "regression",
                "detail": (
                    f"{combo_key} latest attribution total_failure={total_failure:.0f}% "
                    f"(strategy={strategy_failure_pct:.0f}%, risk={risk_failure_pct:.0f}%, "
                    f"execution={execution_failure_pct:.0f}%)"
                ),
                "severity": "high",
            }, source_kind="research_round",
                source_id=str(snapshot.get("round_id")),
                source_timestamp=str(snapshot.get("finished_at")),
                source_phase=ROUND_PHASE_PHASE3,
                source_family=family,
                source_timeframe=timeframe,
                source_payload={
                    "round_id": snapshot.get("round_id"),
                    "phase": ROUND_PHASE_PHASE3,
                    "finished_at": snapshot.get("finished_at"),
                    "combo_key": combo_key,
                    "strategy_failure_pct": strategy_failure_pct,
                    "risk_failure_pct": risk_failure_pct,
                    "execution_failure_pct": execution_failure_pct,
                })
        return _with_source({
            "name": "attribution",
            "status": "ok",
            "detail": f"{combo_key} latest round={snapshot.get('round_id')}",
            "severity": "none",
        }, source_kind="research_round",
            source_id=str(snapshot.get("round_id")),
            source_timestamp=str(snapshot.get("finished_at")),
            source_phase=ROUND_PHASE_PHASE3,
            source_family=family,
            source_timeframe=timeframe,
            source_payload={
                "round_id": snapshot.get("round_id"),
                "phase": ROUND_PHASE_PHASE3,
                "finished_at": snapshot.get("finished_at"),
                "combo_key": combo_key,
                "strategy_failure_pct": strategy_failure_pct,
                "risk_failure_pct": risk_failure_pct,
                "execution_failure_pct": execution_failure_pct,
            })

    if snapshot is not None:
        combo_status = combo.get("status") if isinstance(combo, dict) else None
        detail = f"{combo_key} missing attribution summary in latest round {snapshot.get('round_id')}"
        if combo_status:
            detail = (
                f"{combo_key} latest attribution summary unavailable in round "
                f"{snapshot.get('round_id')} (status={combo_status})"
            )
        return {
            "name": "attribution",
            "status": "unknown",
            "detail": detail,
            "severity": "none",
        }

    return {
        "name": "attribution",
        "status": "unknown",
        "detail": "missing attribution round snapshot",
        "severity": "none",
    }


def _check_execution_regression(
    project_root: Path,
    family: str,
    timeframe: str,
    *,
    not_before: datetime,
) -> dict[str, Any]:
    combo_key = _combo_key(family, timeframe)
    snapshot, combo, summary = _load_combo_round_summary(
        project_root,
        phase=ROUND_PHASE_PHASE4,
        combo_key=combo_key,
        summary_key="cost_summary",
        not_before=not_before,
    )
    if snapshot is not None and summary is not None:
        nested_cost = summary.get("total_execution_cost")
        nested_cost_mean = (
            nested_cost.get("mean") if isinstance(nested_cost, dict) else None
        )
        full_fill = finite_metric(
            summary.get("full_fill_ratio"), minimum=0, maximum=1
        )
        cost_bps = finite_metric(
            summary.get("mean_total_execution_cost_bps", nested_cost_mean)
        )
        if "positive_adjusted_edge_ratio" in summary:
            positive_edge = finite_metric(
                summary.get("positive_adjusted_edge_ratio"),
                minimum=0,
                maximum=1,
            )
        else:
            positive_edge = finite_metric(
                summary.get("positive_edge_ratio"),
                minimum=0,
                maximum=1,
            )
        if full_fill is None or cost_bps is None or positive_edge is None:
            return {
                "name": "execution_realism",
                "status": "unknown",
                "detail": (
                    f"{combo_key} execution metrics must be finite numbers; "
                    "fill and positive-edge ratios must be in [0, 1]"
                ),
                "severity": "none",
            }
        reasons = []
        if full_fill < 0.5:
            reasons.append(f"full_fill_ratio={full_fill:.2f} (<0.5)")
        if cost_bps > 10:
            reasons.append(f"mean_execution_cost={cost_bps:.1f}bps (>10)")
        if positive_edge < 0.3:
            reasons.append(f"positive_edge_ratio={positive_edge:.2f} (<0.3)")
        if reasons:
            return _with_source({
                "name": "execution_realism",
                "status": "regression",
                "detail": "; ".join(reasons),
                "severity": "high" if len(reasons) >= 2 else "medium",
            }, source_kind="research_round",
                source_id=str(snapshot.get("round_id")),
                source_timestamp=str(snapshot.get("finished_at")),
                source_phase=ROUND_PHASE_PHASE4,
                source_family=family,
                source_timeframe=timeframe,
                source_payload={
                    "round_id": snapshot.get("round_id"),
                    "phase": ROUND_PHASE_PHASE4,
                    "finished_at": snapshot.get("finished_at"),
                    "combo_key": combo_key,
                    "full_fill_ratio": full_fill,
                    "mean_execution_cost_bps": cost_bps,
                    "positive_edge_ratio": positive_edge,
                })
        return _with_source({
            "name": "execution_realism",
            "status": "ok",
            "detail": f"{combo_key} fill={full_fill:.2f}, cost={cost_bps:.1f}bps, edge={positive_edge:.2f}",
            "severity": "none",
        }, source_kind="research_round",
            source_id=str(snapshot.get("round_id")),
            source_timestamp=str(snapshot.get("finished_at")),
            source_phase=ROUND_PHASE_PHASE4,
            source_family=family,
            source_timeframe=timeframe,
            source_payload={
                "round_id": snapshot.get("round_id"),
                "phase": ROUND_PHASE_PHASE4,
                "finished_at": snapshot.get("finished_at"),
                "combo_key": combo_key,
                "full_fill_ratio": full_fill,
                "mean_execution_cost_bps": cost_bps,
                "positive_edge_ratio": positive_edge,
            })

    if snapshot is not None:
        combo_status = combo.get("status") if isinstance(combo, dict) else None
        detail = f"{combo_key} missing execution summary in latest round {snapshot.get('round_id')}"
        if combo_status:
            detail = (
                f"{combo_key} latest execution summary unavailable in round "
                f"{snapshot.get('round_id')} (status={combo_status})"
            )
        return {
            "name": "execution_realism",
            "status": "unknown",
            "detail": detail,
            "severity": "none",
        }

    return {
        "name": "execution_realism",
        "status": "unknown",
        "detail": "missing execution round snapshot",
        "severity": "none",
    }


def run_observation(
    project_root: Path,
    *,
    release_id: str,
    family: str | None,
    timeframe: str | None,
    window_hours: int | None = None,
    save_result: bool = True,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
        validate_post_apply_release_identity,
    )

    history = load_release_history(project_root)
    release, identity_error = validate_post_apply_release_identity(
        history,
        release_id=release_id,
        requested_family=family,
        requested_timeframe=timeframe,
    )
    if identity_error is not None:
        return identity_error
    assert release is not None
    family = str(release["family"])
    timeframe = str(release["timeframe"])
    canonical_window_hours = release.get("observation_window_hours")
    if (
        type(canonical_window_hours) is not int
        or canonical_window_hours <= 0
    ):
        return {
            "ok": False,
            "reason": "release_observation_window_invalid",
            "message": "release 缺少有效的 canonical observation window",
            "release_id": release_id,
        }
    if window_hours is None:
        window_hours = canonical_window_hours
    elif type(window_hours) is not int or window_hours != canonical_window_hours:
        return {
            "ok": False,
            "reason": "observation_window_mismatch",
            "message": (
                "请求 observation window 与 release canonical window 不一致；"
                "禁止缩短或改写已批准观察期"
            ),
            "release_id": release_id,
            "requested_observation_window_hours": window_hours,
            "canonical_observation_window_hours": canonical_window_hours,
        }

    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        created_at = parse_iso_datetime_utc(
            release.get("created_at"),
            context="observation_window.release.created_at",
        )
        applied_at = parse_iso_datetime_utc(
            release.get("applied_at"),
            context="observation_window.release.applied_at",
        )
    except (ValueError, TypeError):
        created_at = None
        applied_at = None
    if (
        created_at is None
        or applied_at is None
        or applied_at < created_at
        or applied_at > now + timedelta(minutes=5)
    ):
        return {
            "ok": False,
            "reason": "release_apply_timestamp_invalid",
            "message": (
                "release 缺少可信 applied_at，或 apply 时间早于创建/异常超前；"
                "禁止伪造观察期起点"
            ),
            "release_id": release_id,
        }
    elapsed_hours = max(0.0, (now - applied_at).total_seconds() / 3600)
    window_active = elapsed_hours < window_hours
    started_at = applied_at.isoformat()

    checklist = [
        _check_quality_monitor_regression(project_root, not_before=applied_at),
        _check_decision_regression(
            project_root, family, timeframe, not_before=applied_at
        ),
        _check_attribution_regression(
            project_root, family, timeframe, not_before=applied_at
        ),
        _check_execution_regression(
            project_root, family, timeframe, not_before=applied_at
        ),
    ]

    regressions = [check for check in checklist if check.get("status") == "regression"]
    warns = [check for check in checklist if check.get("status") == "warn"]
    unknowns = [check for check in checklist if check.get("status") == "unknown"]

    if regressions:
        observation_status = "rollback_recommended"
        recommendation = "rollback_recommended"
    elif unknowns:
        # 自然时间到期不等于证据完整。缺失/失败/早于实际 apply 的观测只能
        # 保持 observing + insufficient_evidence，绝不能升级为 completed/keep。
        observation_status = "observing"
        recommendation = "insufficient_evidence"
    elif warns:
        if window_active:
            observation_status = "observing"
            recommendation = "review"
        else:
            observation_status = "completed"
            recommendation = "review"
    else:
        if window_active:
            observation_status = "observing"
            recommendation = "keep"
        else:
            observation_status = "completed"
            recommendation = "keep"

    result = {
        "release_id": release_id,
        "family": family,
        "timeframe": timeframe,
        "combo_key": _combo_key(family, timeframe),
        "evaluated_at": now.isoformat(),
        "started_at": started_at,
        "observation_window_hours": window_hours,
        "window_active": window_active,
        "status": observation_status,
        "recommendation": recommendation,
        "checklist": checklist,
        "regression_count": len(regressions),
        "warning_count": len(warns),
        "unknown_count": len(unknowns),
        "evidence_complete": not unknowns,
        "evidence_contract_version": POST_APPLY_EVIDENCE_CONTRACT_VERSION,
        "source_provenance": collect_source_provenance(checklist),
    }

    if save_result:
        release_to_persist = dict(release)
        release_to_persist["observation_status"] = observation_status
        result = _save_observation(
            project_root,
            release_id,
            result,
            release=release_to_persist,
        )

    return result


def _save_observation(
    project_root: Path,
    release_id: str,
    result: dict[str, Any],
    *,
    release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    managed_truth = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    db_committed = False
    persisted_result = dict(result)
    if ok:
        try:
            from aats.data_platform.governance.active_params_db import (
                db_try_acquire_parameter_apply_lock,
            )
            from aats.data_platform.governance.operational_state_db import (
                db_get_observation_result,
                db_upsert_parameter_release,
                db_upsert_observation_result,
            )

            with Session(engine) as session, session.begin():
                if not db_try_acquire_parameter_apply_lock(
                    session,
                    family=str(result.get("family") or ""),
                    timeframe=str(result.get("timeframe") or ""),
                ):
                    raise RuntimeError(
                        "parameter combo mutation is in progress; "
                        "observation persistence rejected"
                    )
                db_upsert_observation_result(session, result)
                canonical_result = db_get_observation_result(session, release_id)
                if not isinstance(canonical_result, dict):
                    raise RuntimeError(
                        "canonical observation was not readable after upsert"
                    )
                persisted_result = canonical_result
                if release is not None:
                    canonical_release = db_upsert_parameter_release(
                        session,
                        {
                            **release,
                            "observation_status": canonical_result.get("status"),
                        },
                    )
                    if canonical_release.get("observation_status") != (
                        canonical_result.get("status")
                    ):
                        raise RuntimeError(
                            "observation/release status transition did not converge"
                        )
            db_committed = True
        except Exception as exc:
            if managed_truth:
                log.error(
                    "managed observation persistence failed (%s)",
                    type(exc).__name__,
                )
                raise DBUnavailableError(
                    "managed observation persistence failed"
                ) from exc
            log.warning(
                "observation DB sync failed; using offline file mode (%s)",
                type(exc).__name__,
            )
        finally:
            if engine is not None:
                engine.dispose()
    elif managed_truth:
        raise DBUnavailableError(
            "managed observation persistence unavailable"
        )

    if db_committed and release is not None:
        from aats.data_platform.production_workflow.release_registry import (
            mirror_release_history_from_db_best_effort,
        )

        mirror_release_history_from_db_best_effort(project_root)
    elif release is not None:
        # Explicit offline development mode retains the file registry contract.
        from aats.data_platform.production_workflow.release_registry import (
            save_release_record,
        )

        save_release_record(
            {
                **release,
                "observation_status": persisted_result.get("status"),
            },
            project_root,
        )

    obs_dir = project_root / _OBSERVATIONS_DIR / release_id
    try:
        obs_dir.mkdir(parents=True, exist_ok=True)

        summary_path = obs_dir / "observation_summary.json"
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump(persisted_result, f, ensure_ascii=False, indent=2, default=str)

        report_path = obs_dir / "observation_report.md"
        _write_observation_report(persisted_result, report_path)
    except Exception as exc:
        if not managed_truth:
            raise
        log.error(
            "observation_mirror_degraded: canonical DB evidence committed but "
            "local JSON/Markdown mirror/render failed: %s",
            type(exc).__name__,
        )

    log.info("Observation saved: %s", obs_dir)
    return persisted_result


def _write_observation_report(result: dict[str, Any], path: Path) -> None:
    lines = [
        "# Post-Apply Observation Report",
        "",
        f"- Release ID: `{result['release_id']}`",
        f"- Combo: {result['combo_key']}",
        f"- Evaluated: {result['evaluated_at']}",
        f"- Window Active: {'Yes' if result['window_active'] else 'No'}",
        f"- **Status: {result['status'].upper()}**",
        f"- **Recommendation: {result['recommendation'].upper()}**",
        "",
        "## Checklist",
        "",
    ]

    for check in result["checklist"]:
        status_icon = {
            "ok": "OK",
            "warn": "WARN",
            "regression": "REGRESSION",
            "unknown": "?",
        }.get(check.get("status", "?"), "?")
        lines.append(f"- [{status_icon}] **{check['name']}**: {check.get('detail', '')}")

    lines.append("")
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
