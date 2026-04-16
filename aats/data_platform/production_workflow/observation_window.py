"""Observation Window management."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import try_governance_db
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_PHASE3,
    ROUND_PHASE_PHASE4,
    SNAPSHOT_QUALITY_MONITOR,
    load_governance_snapshot,
    load_latest_research_round_snapshot,
)

log = logging.getLogger(__name__)

_OBSERVATIONS_DIR = "artifacts/production_workflow/observations"


def _combo_key(family: str, timeframe: str) -> str:
    return f"{family}_{timeframe.lower()}"


def _load_combo_round_summary(
    project_root: Path,
    *,
    phase: str,
    combo_key: str,
    summary_key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    snapshot = load_latest_research_round_snapshot(
        phase=phase,
        project_root=project_root,
    )
    if not isinstance(snapshot, dict):
        return None, None, None
    combos = (snapshot.get("summary") or {}).get("combos") or {}
    combo = combos.get(combo_key)
    if not isinstance(combo, dict):
        return snapshot, None, None
    summary = combo.get(summary_key)
    if not isinstance(summary, dict):
        return snapshot, combo, None
    return snapshot, combo, summary


def load_observation_result(project_root: Path, release_id: str) -> dict[str, Any] | None:
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_get_observation_result,
            )

            with Session(engine) as session:
                result = db_get_observation_result(session, release_id)
            if result:
                return result
        except Exception as exc:
            log.warning("failed to load observation from DB: %s", exc)
        finally:
            if engine is not None:
                engine.dispose()

    summary_path = project_root / _OBSERVATIONS_DIR / release_id / "observation_summary.json"
    if not summary_path.exists():
        return None
    try:
        with summary_path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _check_quality_monitor_regression(project_root: Path) -> dict[str, Any]:
    qm = load_governance_snapshot(project_root, snapshot_type=SNAPSHOT_QUALITY_MONITOR)
    if not isinstance(qm, dict):
        return {"name": "quality_monitor", "status": "unknown", "detail": "missing quality monitor snapshot"}

    summary = qm.get("summary", {})
    health = summary.get("health", "unknown")
    critical = summary.get("critical_failures", 0)

    if health == "unhealthy" or critical > 0:
        return {
            "name": "quality_monitor",
            "status": "regression",
            "detail": f"health={health}, critical={critical}",
            "severity": "high",
        }
    if health == "degraded":
        return {
            "name": "quality_monitor",
            "status": "warn",
            "detail": "health=degraded",
            "severity": "medium",
        }
    return {
        "name": "quality_monitor",
        "status": "ok",
        "detail": f"health={health}",
        "severity": "none",
    }


def _check_decision_regression(
    project_root: Path, family: str, timeframe: str,
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
            status = decision.get("current_status", "unknown")
            if status == "pause":
                return {
                    "name": "decision_status",
                    "status": "regression",
                    "detail": f"{combo_key} status=pause",
                    "severity": "high",
                }
            if status == "require_review":
                return {
                    "name": "decision_status",
                    "status": "warn",
                    "detail": f"{combo_key} status=require_review",
                    "severity": "medium",
                }
            return {
                "name": "decision_status",
                "status": "ok",
                "detail": f"{combo_key} status={status}",
                "severity": "none",
            }

    return {"name": "decision_status", "status": "ok", "detail": "no decision record"}


def _check_attribution_regression(
    project_root: Path, family: str, timeframe: str,
) -> dict[str, Any]:
    combo_key = _combo_key(family, timeframe)
    snapshot, combo, summary = _load_combo_round_summary(
        project_root,
        phase=ROUND_PHASE_PHASE3,
        combo_key=combo_key,
        summary_key="attribution_summary",
    )
    if snapshot is not None and summary is not None:
        strategy_failure_pct = summary.get("strategy_failure_pct", 0)
        risk_failure_pct = summary.get("risk_failure_pct", 0)
        execution_failure_pct = summary.get("execution_failure_pct", 0)
        total_failure = strategy_failure_pct + risk_failure_pct + execution_failure_pct
        if total_failure > 80:
            return {
                "name": "attribution",
                "status": "regression",
                "detail": (
                    f"{combo_key} latest attribution total_failure={total_failure:.0f}% "
                    f"(strategy={strategy_failure_pct:.0f}%, risk={risk_failure_pct:.0f}%, "
                    f"execution={execution_failure_pct:.0f}%)"
                ),
                "severity": "high",
            }
        return {
            "name": "attribution",
            "status": "ok",
            "detail": f"{combo_key} latest round={snapshot.get('round_id')}",
            "severity": "none",
        }

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
            "status": "warn",
            "detail": detail,
            "severity": "medium",
        }

    return {
        "name": "attribution",
        "status": "unknown",
        "detail": "missing attribution round snapshot",
        "severity": "none",
    }


def _check_execution_regression(
    project_root: Path, family: str, timeframe: str,
) -> dict[str, Any]:
    combo_key = _combo_key(family, timeframe)
    snapshot, combo, summary = _load_combo_round_summary(
        project_root,
        phase=ROUND_PHASE_PHASE4,
        combo_key=combo_key,
        summary_key="cost_summary",
    )
    if snapshot is not None and summary is not None:
        full_fill = summary.get("full_fill_ratio", 1.0)
        cost_bps = summary.get(
            "mean_total_execution_cost_bps",
            summary.get("total_execution_cost", {}).get("mean", 0),
        )
        positive_edge = summary.get(
            "positive_adjusted_edge_ratio",
            summary.get("positive_edge_ratio", 1.0),
        )
        reasons = []
        if full_fill < 0.5:
            reasons.append(f"full_fill_ratio={full_fill:.2f} (<0.5)")
        if cost_bps > 10:
            reasons.append(f"mean_execution_cost={cost_bps:.1f}bps (>10)")
        if positive_edge < 0.3:
            reasons.append(f"positive_edge_ratio={positive_edge:.2f} (<0.3)")
        if reasons:
            return {
                "name": "execution_realism",
                "status": "regression",
                "detail": "; ".join(reasons),
                "severity": "high" if len(reasons) >= 2 else "medium",
            }
        return {
            "name": "execution_realism",
            "status": "ok",
            "detail": f"{combo_key} fill={full_fill:.2f}, cost={cost_bps:.1f}bps, edge={positive_edge:.2f}",
            "severity": "none",
        }

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
            "status": "warn",
            "detail": detail,
            "severity": "medium",
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
    family: str,
    timeframe: str,
    window_hours: int = 24,
    save_result: bool = True,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    from aats.data_platform.production_workflow.release_registry import (
        find_release,
        load_release_history,
    )

    history = load_release_history(project_root)
    release = find_release(history, release_id)

    window_active = True
    started_at = now.isoformat()
    if release:
        created_str = release.get("created_at")
        if created_str:
            try:
                created_at = datetime.fromisoformat(created_str)
                elapsed_hours = (now - created_at).total_seconds() / 3600
                started_at = created_str
                if elapsed_hours >= window_hours:
                    window_active = False
            except (ValueError, TypeError):
                pass

    checklist = [
        _check_quality_monitor_regression(project_root),
        _check_decision_regression(project_root, family, timeframe),
        _check_attribution_regression(project_root, family, timeframe),
        _check_execution_regression(project_root, family, timeframe),
    ]

    regressions = [check for check in checklist if check.get("status") == "regression"]
    warns = [check for check in checklist if check.get("status") == "warn"]

    if regressions:
        observation_status = "rollback_recommended"
        recommendation = "rollback_recommended"
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
    }

    if save_result:
        _save_observation(project_root, release_id, result)

    if release and save_result:
        from aats.data_platform.production_workflow.release_registry import (
            save_release_history,
            update_release_status,
        )

        update_release_status(
            history,
            release_id,
            observation_status=observation_status,
        )
        save_release_history(history, project_root)

    return result


def _save_observation(
    project_root: Path,
    release_id: str,
    result: dict[str, Any],
) -> Path:
    obs_dir = project_root / _OBSERVATIONS_DIR / release_id
    obs_dir.mkdir(parents=True, exist_ok=True)

    summary_path = obs_dir / "observation_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_upsert_observation_result,
            )

            with Session(engine) as session, session.begin():
                db_upsert_observation_result(session, result)
        except Exception as exc:
            log.warning("observation DB sync failed: %s", exc)
        finally:
            if engine is not None:
                engine.dispose()

    report_path = obs_dir / "observation_report.md"
    _write_observation_report(result, report_path)

    log.info("Observation saved: %s", obs_dir)
    return obs_dir


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
