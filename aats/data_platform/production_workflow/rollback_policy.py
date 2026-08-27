"""Rollback recommendation policy."""

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

_ROLLBACK_DIR = "artifacts/production_workflow/rollback_recommendations"


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
            "trigger": result.get("trigger", "unknown"),
            "fired": False,
            "evidence_status": "insufficient",
            "detail": "canonical source provenance could not be constructed",
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
            context=f"rollback_policy.{phase}.finished_at",
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


def load_rollback_recommendation(project_root: Path, release_id: str) -> dict[str, Any] | None:
    managed_truth = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_get_rollback_recommendation,
            )

            with Session(engine) as session:
                result = db_get_rollback_recommendation(session, release_id)
            if result is not None:
                return result
        except Exception as exc:
            if managed_truth:
                raise DBUnavailableError(
                    "managed rollback recommendation read failed; stale file fallback denied"
                ) from exc
            log.warning(
                "failed to load rollback recommendation from DB (%s)",
                type(exc).__name__,
            )
        finally:
            if engine is not None:
                engine.dispose()
        if managed_truth:
            return None
    elif managed_truth:
        raise DBUnavailableError(
            "managed rollback recommendation unavailable; stale file fallback denied"
        )

    json_path = project_root / _ROLLBACK_DIR / release_id / "rollback_recommendation.json"
    if not json_path.exists():
        return None
    try:
        with json_path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _evaluate_attribution_regression(
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
                "trigger": "attribution_regression",
                "fired": False,
                "evidence_status": "insufficient",
                "detail": (
                    f"{combo_key} attribution percentages must be finite "
                    "numbers in [0, 100]"
                ),
            }
        assert strategy_failure_pct is not None
        assert risk_failure_pct is not None
        assert execution_failure_pct is not None
        total_failure = strategy_failure_pct + risk_failure_pct + execution_failure_pct
        source_kwargs = {
            "source_kind": "research_round",
            "source_id": str(snapshot.get("round_id")),
            "source_timestamp": str(snapshot.get("finished_at")),
            "source_phase": ROUND_PHASE_PHASE3,
            "source_family": family,
            "source_timeframe": timeframe,
            "source_payload": {
                "round_id": snapshot.get("round_id"),
                "phase": ROUND_PHASE_PHASE3,
                "finished_at": snapshot.get("finished_at"),
                "combo_key": combo_key,
                "strategy_failure_pct": strategy_failure_pct,
                "risk_failure_pct": risk_failure_pct,
                "execution_failure_pct": execution_failure_pct,
            },
        }
        if total_failure > 80:
            return _with_source({
                "trigger": "attribution_regression",
                "fired": True,
                "severity": "high",
                "evidence_status": "valid",
                "detail": (
                    f"{combo_key} total_failure={total_failure:.0f}% "
                    f"(strategy={strategy_failure_pct:.0f}%, risk={risk_failure_pct:.0f}%, "
                    f"execution={execution_failure_pct:.0f}%)"
                ),
            }, **source_kwargs)
        return _with_source({
            "trigger": "attribution_regression",
            "fired": False,
            "evidence_status": "valid",
            "detail": f"{combo_key} total_failure={total_failure:.0f}% (within expected range)",
        }, **source_kwargs)

    if snapshot is not None:
        combo_status = combo.get("status") if isinstance(combo, dict) else None
        detail = f"{combo_key} missing attribution summary in latest round {snapshot.get('round_id')}"
        if combo_status:
            detail = (
                f"{combo_key} latest attribution summary unavailable in round "
                f"{snapshot.get('round_id')} (status={combo_status})"
            )
        return {
            "trigger": "attribution_regression",
            "fired": False,
            "evidence_status": "insufficient",
            "detail": detail,
        }

    return {
        "trigger": "attribution_regression",
        "fired": False,
        "evidence_status": "insufficient",
        "detail": "missing attribution round snapshot",
    }


def _evaluate_execution_regression(
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
                "trigger": "execution_regression",
                "fired": False,
                "evidence_status": "insufficient",
                "detail": (
                    f"{combo_key} execution metrics must be finite numbers; "
                    "fill and positive-edge ratios must be in [0, 1]"
                ),
            }
        reasons = []
        if full_fill < 0.5:
            reasons.append(f"full_fill_ratio={full_fill:.2f} (<0.5)")
        if cost_bps > 10:
            reasons.append(f"mean_execution_cost={cost_bps:.1f}bps (>10)")
        if positive_edge < 0.3:
            reasons.append(f"positive_edge_ratio={positive_edge:.2f} (<0.3)")
        source_kwargs = {
            "source_kind": "research_round",
            "source_id": str(snapshot.get("round_id")),
            "source_timestamp": str(snapshot.get("finished_at")),
            "source_phase": ROUND_PHASE_PHASE4,
            "source_family": family,
            "source_timeframe": timeframe,
            "source_payload": {
                "round_id": snapshot.get("round_id"),
                "phase": ROUND_PHASE_PHASE4,
                "finished_at": snapshot.get("finished_at"),
                "combo_key": combo_key,
                "full_fill_ratio": full_fill,
                "mean_execution_cost_bps": cost_bps,
                "positive_edge_ratio": positive_edge,
            },
        }
        if reasons:
            return _with_source({
                "trigger": "execution_regression",
                "fired": True,
                "severity": "high" if len(reasons) >= 2 else "medium",
                "evidence_status": "valid",
                "detail": "; ".join(reasons),
            }, **source_kwargs)
        return _with_source({
            "trigger": "execution_regression",
            "fired": False,
            "evidence_status": "valid",
            "detail": f"{combo_key} fill={full_fill:.2f}, cost={cost_bps:.1f}bps, edge={positive_edge:.2f}",
        }, **source_kwargs)

    if snapshot is not None:
        combo_status = combo.get("status") if isinstance(combo, dict) else None
        detail = f"{combo_key} missing execution summary in latest round {snapshot.get('round_id')}"
        if combo_status:
            detail = (
                f"{combo_key} latest execution summary unavailable in round "
                f"{snapshot.get('round_id')} (status={combo_status})"
            )
        return {
            "trigger": "execution_regression",
            "fired": False,
            "evidence_status": "insufficient",
            "detail": detail,
        }

    return {
        "trigger": "execution_regression",
        "fired": False,
        "evidence_status": "insufficient",
        "detail": "missing execution round snapshot",
    }


def _evaluate_governance_regression(
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
        return {
            "trigger": "governance_regression",
            "fired": False,
            "evidence_status": "insufficient",
            "detail": "missing quality monitor",
        }

    try:
        from aats.data_platform.governance._time_util import (
            parse_iso_datetime_utc,
        )

        generated_at = parse_iso_datetime_utc(
            qm.get("generated_at"),
            context="rollback_policy.quality_monitor.generated_at",
        )
    except (TypeError, ValueError):
        generated_at = None
    if (
        generated_at is None
        or generated_at < not_before
        or generated_at > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        return {
            "trigger": "governance_regression",
            "fired": False,
            "evidence_status": "insufficient",
            "detail": "quality monitor evidence is missing, stale, or invalid",
        }

    summary = qm.get("summary")
    if not isinstance(summary, dict):
        return {
            "trigger": "governance_regression",
            "fired": False,
            "evidence_status": "insufficient",
            "detail": "quality monitor summary is malformed",
        }
    health = summary.get("health")
    critical = summary.get("critical_failures")
    if (
        health not in {"healthy", "degraded", "unhealthy"}
        or type(critical) is not int
        or critical < 0
    ):
        return {
            "trigger": "governance_regression",
            "fired": False,
            "evidence_status": "insufficient",
            "detail": "quality monitor health/count contract is invalid",
        }

    source_kwargs = {
        "source_kind": "governance_snapshot",
        "source_id": f"{SNAPSHOT_QUALITY_MONITOR}:{generated_at.isoformat()}",
        "source_timestamp": generated_at,
        "source_payload": {
            "snapshot_type": SNAPSHOT_QUALITY_MONITOR,
            "generated_at": generated_at.isoformat(),
            "health": health,
            "critical_failures": critical,
        },
    }

    if health == "unhealthy" or critical > 0:
        return _with_source({
            "trigger": "governance_regression",
            "fired": True,
            "severity": "high",
            "evidence_status": "valid",
            "detail": f"health={health}, critical_failures={critical}",
        }, **source_kwargs)
    if health == "degraded":
        return _with_source({
            "trigger": "governance_regression",
            "fired": True,
            "severity": "medium",
            "evidence_status": "valid",
            "detail": "health=degraded",
        }, **source_kwargs)

    return _with_source({
        "trigger": "governance_regression",
        "fired": False,
        "evidence_status": "valid",
        "detail": f"health={health}",
    }, **source_kwargs)


def evaluate_rollback_recommendation(
    project_root: Path,
    *,
    release_id: str,
    family: str | None,
    timeframe: str | None,
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

    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        created_at = parse_iso_datetime_utc(
            release.get("created_at"),
            context="rollback_policy.release.created_at",
        )
        applied_at = parse_iso_datetime_utc(
            release.get("applied_at"),
            context="rollback_policy.release.applied_at",
        )
    except (TypeError, ValueError):
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
            "release_id": release_id,
            "rollback_recommended": None,
            "message": "缺少可信 applied_at，禁止基于错误时间窗口生成回滚建议",
        }

    triggers = [
        _evaluate_attribution_regression(
            project_root, family, timeframe, not_before=applied_at
        ),
        _evaluate_execution_regression(
            project_root, family, timeframe, not_before=applied_at
        ),
        _evaluate_governance_regression(project_root, not_before=applied_at),
    ]

    insufficient = [
        trigger
        for trigger in triggers
        if trigger.get("evidence_status") != "valid"
    ]
    fired = [
        trigger
        for trigger in triggers
        if trigger.get("evidence_status") == "valid" and trigger.get("fired")
    ]
    reasons = [trigger["detail"] for trigger in fired]

    high_count = sum(1 for trigger in fired if trigger.get("severity") == "high")
    medium_count = sum(1 for trigger in fired if trigger.get("severity") == "medium")

    if high_count > 0:
        severity = "high"
        rollback_recommended = True
    elif medium_count >= 2:
        severity = "high"
        rollback_recommended = True
    elif medium_count == 1:
        severity = "medium"
        rollback_recommended = True
    elif insufficient:
        # Missing evidence may not manufacture a reassuring false.  It also
        # must not erase a valid fired trigger: the true branch above wins and
        # is persisted as a sticky risk obligation.
        return {
            "ok": False,
            "reason": "rollback_evidence_insufficient",
            "release_id": release_id,
            "family": family,
            "timeframe": timeframe,
            "combo_key": _combo_key(family, timeframe),
            "evaluated_at": now.isoformat(),
            "rollback_recommended": None,
            "severity": "none",
            "triggers": triggers,
            "insufficient_evidence_count": len(insufficient),
            "message": (
                "post-apply 证据缺失、过旧或无效；保持观察并禁止物化 true/false "
                "rollback 结论"
            ),
        }
    else:
        severity = "none"
        rollback_recommended = False

    suggested_target = None
    if rollback_recommended:
        suggested_target = release.get("previous_parameter_set_id")

    result = {
        "release_id": release_id,
        "family": family,
        "timeframe": timeframe,
        "combo_key": _combo_key(family, timeframe),
        "evaluated_at": now.isoformat(),
        "rollback_recommended": rollback_recommended,
        "severity": severity,
        "reasons": reasons,
        "suggested_target_parameter_set_id": suggested_target,
        "triggers": triggers,
        "fired_trigger_count": len(fired),
        "evidence_contract_version": POST_APPLY_EVIDENCE_CONTRACT_VERSION,
        "source_provenance": collect_source_provenance(triggers),
    }

    if save_result:
        result = _save_rollback_recommendation(project_root, release_id, result)

    return result


def _save_rollback_recommendation(
    project_root: Path,
    release_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    managed_truth = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    persisted_result = dict(result)
    if ok:
        try:
            from aats.data_platform.governance.active_params_db import (
                db_try_acquire_parameter_apply_lock,
            )
            from aats.data_platform.governance.operational_state_db import (
                db_get_rollback_recommendation,
                db_upsert_rollback_recommendation,
            )

            with Session(engine) as session, session.begin():
                if not db_try_acquire_parameter_apply_lock(
                    session,
                    family=str(result.get("family") or ""),
                    timeframe=str(result.get("timeframe") or ""),
                ):
                    raise RuntimeError(
                        "parameter combo mutation is in progress; "
                        "rollback recommendation persistence rejected"
                    )
                db_upsert_rollback_recommendation(session, result)
                canonical_result = db_get_rollback_recommendation(
                    session, release_id
                )
                if not isinstance(canonical_result, dict):
                    raise RuntimeError(
                        "canonical rollback recommendation was not readable "
                        "after upsert"
                    )
                persisted_result = canonical_result
        except Exception as exc:
            if managed_truth:
                log.error(
                    "managed rollback recommendation persistence failed (%s)",
                    type(exc).__name__,
                )
                raise DBUnavailableError(
                    "managed rollback recommendation persistence failed"
                ) from exc
            log.warning(
                "rollback recommendation DB sync failed; using offline file mode (%s)",
                type(exc).__name__,
            )
        finally:
            if engine is not None:
                engine.dispose()
    elif managed_truth:
        raise DBUnavailableError(
            "managed rollback recommendation persistence unavailable"
        )

    rb_dir = project_root / _ROLLBACK_DIR / release_id
    try:
        rb_dir.mkdir(parents=True, exist_ok=True)

        json_path = rb_dir / "rollback_recommendation.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(persisted_result, f, ensure_ascii=False, indent=2, default=str)

        report_path = rb_dir / "rollback_recommendation_report.md"
        _write_rollback_report(persisted_result, report_path)
    except Exception as exc:
        if not managed_truth:
            raise
        log.error(
            "rollback_recommendation_mirror_degraded: canonical DB evidence "
            "committed but local JSON/Markdown mirror/render failed: %s",
            type(exc).__name__,
        )

    log.info("Rollback recommendation saved: %s", rb_dir)
    return persisted_result


def _write_rollback_report(result: dict[str, Any], path: Path) -> None:
    recommended = "YES" if result["rollback_recommended"] else "NO"
    lines = [
        "# Rollback Recommendation Report",
        "",
        f"- Release ID: `{result['release_id']}`",
        f"- Combo: {result['combo_key']}",
        f"- Evaluated: {result['evaluated_at']}",
        f"- **Rollback Recommended: {recommended}**",
        f"- Severity: {result['severity']}",
        "",
    ]

    if result["suggested_target_parameter_set_id"]:
        lines.append(f"- Suggested Target: `{result['suggested_target_parameter_set_id']}`")
        lines.append("")

    lines.append("## Triggers")
    lines.append("")
    for trigger in result["triggers"]:
        icon = "FIRED" if trigger.get("fired") else "OK"
        lines.append(f"- [{icon}] **{trigger['trigger']}**: {trigger.get('detail', '')}")
    lines.append("")

    if result["reasons"]:
        lines.append("## Reasons")
        lines.append("")
        for reason in result["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
