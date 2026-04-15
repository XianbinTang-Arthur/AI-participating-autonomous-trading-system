"""Release observation cycle orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_release_observation_cycle(
    project_root: Path,
    *,
    save_results: bool = True,
) -> dict[str, Any]:
    """处理所有仍在观察中的 release."""
    from aats.data_platform.metrics.release_effectiveness import (
        enforce_pending_rollbacks,
        evaluate_release_effectiveness,
    )
    from aats.data_platform.production_workflow.observation_window import (
        run_observation,
    )
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )
    from aats.data_platform.production_workflow.rollback_policy import (
        evaluate_rollback_recommendation,
    )

    history = load_release_history(project_root)
    releases = history.get("releases", [])
    observing_releases = [
        release
        for release in releases
        if release.get("apply_result") == "success"
        and release.get("observation_status") in {"pending", "observing", "rollback_recommended"}
    ]

    summary: dict[str, Any] = {
        "ok": True,
        "processed_count": 0,
        "rollback_recommended_count": 0,
        "auto_rollback_count": 0,
        "results": [],
    }

    for release in observing_releases:
        family = release.get("family")
        timeframe = release.get("timeframe")
        release_id = release.get("release_id")
        if not family or not timeframe or not release_id:
            summary["ok"] = False
            summary["results"].append(
                {
                    "release_id": release_id,
                    "ok": False,
                    "error": "release missing family/timeframe/release_id",
                }
            )
            continue

        observation_result = run_observation(
            project_root,
            release_id=release_id,
            family=family,
            timeframe=timeframe,
            window_hours=int(release.get("observation_window_hours", 24) or 24),
            save_result=save_results,
        )
        rollback_result = evaluate_rollback_recommendation(
            project_root,
            release_id=release_id,
            family=family,
            timeframe=timeframe,
            save_result=save_results,
        )
        effectiveness_result = evaluate_release_effectiveness(
            project_root,
            release_id,
            save_result=save_results,
        )

        if rollback_result.get("rollback_recommended"):
            summary["rollback_recommended_count"] += 1

        summary["processed_count"] += 1
        summary["results"].append(
            {
                "release_id": release_id,
                "family": family,
                "timeframe": timeframe,
                "observation": observation_result,
                "rollback_recommendation": rollback_result,
                "effectiveness": effectiveness_result,
                "ok": "error" not in effectiveness_result,
            }
        )

    auto_rollbacks = enforce_pending_rollbacks(project_root) if save_results else []
    summary["auto_rollbacks"] = auto_rollbacks
    summary["auto_rollback_count"] = sum(1 for item in auto_rollbacks if item.get("ok"))
    if any(not item.get("ok", False) for item in auto_rollbacks):
        summary["ok"] = False

    return summary
