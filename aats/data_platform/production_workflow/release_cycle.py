"""Approved-only release cycle for RDP parameter governance."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aats.data_platform.decision_system.evidence_bundle import make_combo_key
from aats.data_platform.decision_system.recommendation_registry import (
    load_recommendation_registry,
)
from aats.data_platform.operations.environment_guard import get_current_environment
from aats.data_platform.production_workflow.release_registry import (
    create_parameter_release,
    load_release_history,
)

log = logging.getLogger(__name__)

_RELEASE_CYCLE_ROOT = Path("artifacts/production_workflow/release_cycles")
_AUTO_RELEASE_TYPES = frozenset({"parameter_upgrade"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_cycle_id() -> str:
    return f"relcy_{_utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _approval_sort_key(recommendation: dict[str, Any]) -> tuple[str, str]:
    approved_at = str(recommendation.get("approved_at") or "")
    created_at = str(recommendation.get("created_at") or "")
    return approved_at or created_at, created_at


def _release_cycle_dir(project_root: Path, cycle_id: str) -> Path:
    return project_root / _RELEASE_CYCLE_ROOT / cycle_id


def _build_markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "# Release Cycle Report",
        "",
        f"- Cycle ID: `{result.get('cycle_id')}`",
        f"- Environment: `{result.get('environment')}`",
        f"- Started At: `{result.get('started_at')}`",
        f"- Finished At: `{result.get('finished_at')}`",
        f"- Dry Run: `{result.get('dry_run')}`",
        f"- Reviewed Recommendations: `{result.get('reviewed_count', 0)}`",
        f"- Eligible Recommendations: `{result.get('eligible_count', 0)}`",
        f"- Selected Recommendations: `{result.get('selected_count', 0)}`",
        f"- Created Releases: `{result.get('created_release_count', 0)}`",
        f"- Gate Blocked: `{result.get('blocked_count', 0)}`",
        f"- Failures: `{result.get('failed_count', 0)}`",
        "",
        "## Results",
        "",
        "| Combo | Recommendation | Outcome | Detail |",
        "|---|---|---|---|",
    ]
    for item in result.get("results", []):
        lines.append(
            "| {combo} | `{rec}` | `{outcome}` | {detail} |".format(
                combo=item.get("combo_key") or "-",
                rec=item.get("recommendation_id") or "-",
                outcome=item.get("outcome") or "-",
                detail=item.get("detail") or "-",
            ),
        )
    return "\n".join(lines) + "\n"


def _save_cycle_result(project_root: Path, result: dict[str, Any]) -> dict[str, str]:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    cycle_dir = _release_cycle_dir(project_root, result["cycle_id"])
    cycle_dir.mkdir(parents=True, exist_ok=True)

    summary_path = cycle_dir / "release_cycle_summary.json"
    atomic_json_write(result, summary_path)

    report_path = cycle_dir / "release_cycle_report.md"
    report_path.write_text(_build_markdown_report(result), encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "report_path": str(report_path),
    }


def _select_release_candidates(
    registry: dict[str, Any],
    release_history: dict[str, Any],
) -> dict[str, Any]:
    existing_release_by_rec: dict[str, dict[str, Any]] = {}
    for release in release_history.get("releases", []):
        recommendation_id = release.get("recommendation_id")
        if recommendation_id:
            existing_release_by_rec[recommendation_id] = release

    reviewed_count = 0
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for recommendation in registry.get("recommendations", []):
        reviewed_count += 1
        recommendation_id = recommendation.get("recommendation_id")
        combo_key = make_combo_key(
            recommendation.get("family"),
            recommendation.get("timeframe"),
        )
        if recommendation.get("status") != "approved":
            continue
        if recommendation.get("recommendation_type") not in _AUTO_RELEASE_TYPES:
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": "不是自动发布类型",
                },
            )
            continue
        if not recommendation.get("target_parameter_set_id"):
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": "缺少 target_parameter_set_id",
                },
            )
            continue
        if recommendation_id in existing_release_by_rec:
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": "已存在 release 记录",
                    "release_id": existing_release_by_rec[recommendation_id].get("release_id"),
                },
            )
            continue
        eligible.append(recommendation)

    eligible.sort(key=_approval_sort_key, reverse=True)

    selected: list[dict[str, Any]] = []
    seen_combos: set[str] = set()
    for recommendation in eligible:
        combo_key = make_combo_key(
            recommendation.get("family"),
            recommendation.get("timeframe"),
        )
        if not combo_key:
            skipped.append(
                {
                    "recommendation_id": recommendation.get("recommendation_id"),
                    "combo_key": None,
                    "outcome": "skipped",
                    "detail": "family/timeframe 不完整",
                },
            )
            continue
        if combo_key in seen_combos:
            skipped.append(
                {
                    "recommendation_id": recommendation.get("recommendation_id"),
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": "同一组合已有更新的 approved recommendation",
                },
            )
            continue
        seen_combos.add(combo_key)
        selected.append(recommendation)

    return {
        "reviewed_count": reviewed_count,
        "eligible": selected,
        "skipped": skipped,
    }


def run_release_cycle(
    project_root: Path,
    *,
    actor: str = "release_cycle",
    dry_run: bool = False,
    save_results: bool = True,
) -> dict[str, Any]:
    started_at = _utcnow()
    cycle_id = _make_cycle_id()
    environment = get_current_environment()

    registry_path = project_root / "artifacts/decision_system/recommendation_registry.json"
    registry = load_recommendation_registry(registry_path)
    release_history = load_release_history(project_root)
    selection = _select_release_candidates(registry, release_history)

    results: list[dict[str, Any]] = list(selection["skipped"])
    created_release_count = 0
    blocked_count = 0
    failed_count = 0

    for recommendation in selection["eligible"]:
        recommendation_id = str(recommendation.get("recommendation_id"))
        combo_key = make_combo_key(
            recommendation.get("family"),
            recommendation.get("timeframe"),
        )

        if dry_run:
            results.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "dry_run",
                    "detail": "仅评估，不创建 release",
                },
            )
            continue

        try:
            release_result = create_parameter_release(
                project_root,
                recommendation_id=recommendation_id,
                actor=actor,
                run_gate=True,
                run_apply=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            failed_count += 1
            log.exception("release cycle failed for %s", recommendation_id)
            results.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "failed",
                    "detail": str(exc),
                },
            )
            continue

        release = release_result.get("release") or {}
        apply_result = release.get("apply_result")
        outcome = "release_created"
        detail = release_result.get("message") or "release created"
        if apply_result == "success":
            created_release_count += 1
        elif apply_result == "blocked_by_gate":
            blocked_count += 1
            outcome = "blocked_by_gate"
            detail = release_result.get("message") or "gate blocked apply"
        elif release_result.get("ok") is False:
            failed_count += 1
            outcome = "failed"

        results.append(
            {
                "recommendation_id": recommendation_id,
                "combo_key": combo_key,
                "outcome": outcome,
                "detail": detail,
                "release_id": release.get("release_id"),
                "apply_result": apply_result,
                "gate_status": release.get("gate_status"),
            },
        )

    finished_at = _utcnow()
    result: dict[str, Any] = {
        "ok": failed_count == 0,
        "cycle_id": cycle_id,
        "environment": environment,
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "reviewed_count": selection["reviewed_count"],
        "eligible_count": len(selection["eligible"]),
        "selected_count": len(selection["eligible"]),
        "created_release_count": created_release_count,
        "blocked_count": blocked_count,
        "failed_count": failed_count,
        "skipped_count": len(selection["skipped"]),
        "results": results,
    }

    if save_results:
        result["artifacts"] = _save_cycle_result(project_root, result)

    return result
