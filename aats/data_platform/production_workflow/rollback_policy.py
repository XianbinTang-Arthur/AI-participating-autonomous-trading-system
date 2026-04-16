"""Rollback recommendation policy."""

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

_ROLLBACK_DIR = "artifacts/production_workflow/rollback_recommendations"


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


def load_rollback_recommendation(project_root: Path, release_id: str) -> dict[str, Any] | None:
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_get_rollback_recommendation,
            )

            with Session(engine) as session:
                result = db_get_rollback_recommendation(session, release_id)
            if result:
                return result
        except Exception as exc:
            log.warning("failed to load rollback recommendation from DB: %s", exc)
        finally:
            if engine is not None:
                engine.dispose()

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
                "trigger": "attribution_regression",
                "fired": True,
                "severity": "high",
                "detail": (
                    f"{combo_key} total_failure={total_failure:.0f}% "
                    f"(strategy={strategy_failure_pct:.0f}%, risk={risk_failure_pct:.0f}%, "
                    f"execution={execution_failure_pct:.0f}%)"
                ),
            }
        return {
            "trigger": "attribution_regression",
            "fired": False,
            "detail": f"{combo_key} total_failure={total_failure:.0f}% (within expected range)",
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
            "trigger": "attribution_regression",
            "fired": False,
            "detail": detail,
        }

    return {
        "trigger": "attribution_regression",
        "fired": False,
        "detail": "missing attribution round snapshot",
    }


def _evaluate_execution_regression(
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
                "trigger": "execution_regression",
                "fired": True,
                "severity": "high" if len(reasons) >= 2 else "medium",
                "detail": "; ".join(reasons),
            }
        return {
            "trigger": "execution_regression",
            "fired": False,
            "detail": f"{combo_key} fill={full_fill:.2f}, cost={cost_bps:.1f}bps, edge={positive_edge:.2f}",
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
            "trigger": "execution_regression",
            "fired": False,
            "detail": detail,
        }

    return {
        "trigger": "execution_regression",
        "fired": False,
        "detail": "missing execution round snapshot",
    }


def _evaluate_governance_regression(project_root: Path) -> dict[str, Any]:
    qm = load_governance_snapshot(project_root, snapshot_type=SNAPSHOT_QUALITY_MONITOR)
    if not isinstance(qm, dict):
        return {"trigger": "governance_regression", "fired": False, "detail": "missing quality monitor"}

    summary = qm.get("summary", {})
    health = summary.get("health", "unknown")
    critical = summary.get("critical_failures", 0)

    if health == "unhealthy" or critical > 0:
        return {
            "trigger": "governance_regression",
            "fired": True,
            "severity": "high",
            "detail": f"health={health}, critical_failures={critical}",
        }
    if health == "degraded":
        return {
            "trigger": "governance_regression",
            "fired": True,
            "severity": "medium",
            "detail": "health=degraded",
        }

    return {
        "trigger": "governance_regression",
        "fired": False,
        "detail": f"health={health}",
    }


def evaluate_rollback_recommendation(
    project_root: Path,
    *,
    release_id: str,
    family: str,
    timeframe: str,
    save_result: bool = True,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    triggers = [
        _evaluate_attribution_regression(project_root, family, timeframe),
        _evaluate_execution_regression(project_root, family, timeframe),
        _evaluate_governance_regression(project_root),
    ]

    fired = [trigger for trigger in triggers if trigger.get("fired")]
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
    else:
        severity = "none"
        rollback_recommended = False

    suggested_target = None
    if rollback_recommended:
        from aats.data_platform.production_workflow.release_registry import (
            find_release,
            load_release_history,
        )

        history = load_release_history(project_root)
        release = find_release(history, release_id)
        if release:
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
    }

    if save_result:
        _save_rollback_recommendation(project_root, release_id, result)

    return result


def _save_rollback_recommendation(
    project_root: Path,
    release_id: str,
    result: dict[str, Any],
) -> Path:
    rb_dir = project_root / _ROLLBACK_DIR / release_id
    rb_dir.mkdir(parents=True, exist_ok=True)

    json_path = rb_dir / "rollback_recommendation.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_upsert_rollback_recommendation,
            )

            with Session(engine) as session, session.begin():
                db_upsert_rollback_recommendation(session, result)
        except Exception as exc:
            log.warning("rollback recommendation DB sync failed: %s", exc)
        finally:
            if engine is not None:
                engine.dispose()

    report_path = rb_dir / "rollback_recommendation_report.md"
    _write_rollback_report(result, report_path)

    log.info("Rollback recommendation saved: %s", rb_dir)
    return rb_dir


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
