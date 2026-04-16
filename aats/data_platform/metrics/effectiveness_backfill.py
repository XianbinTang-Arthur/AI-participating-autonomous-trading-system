from __future__ import annotations

from pathlib import Path
from typing import Any

from aats.data_platform.metrics.release_effectiveness import (
    evaluate_release_effectiveness,
    find_effectiveness,
)
from aats.data_platform.production_workflow.release_registry import load_release_history


def collect_rolled_back_release_ids(root: Path) -> list[str]:
    history = load_release_history(root)
    releases = history.get("releases", []) if isinstance(history, dict) else []
    ordered: list[str] = []
    seen: set[str] = set()
    for release in releases:
        if str(release.get("observation_status") or "").strip().lower() != "rolled_back":
            continue
        release_id = str(release.get("release_id") or "").strip()
        if not release_id or release_id in seen:
            continue
        seen.add(release_id)
        ordered.append(release_id)
    return ordered


def backfill_release_effectiveness(
    root: Path,
    *,
    release_ids: list[str] | None = None,
    save_result: bool = True,
) -> dict[str, Any]:
    target_ids = list(release_ids or collect_rolled_back_release_ids(root))
    results: list[dict[str, Any]] = []
    changed_count = 0
    error_count = 0

    for release_id in target_ids:
        previous = find_effectiveness(root, release_id)
        evaluation = evaluate_release_effectiveness(root, release_id, save_result=save_result)
        if evaluation.get("error"):
            error_count += 1
            results.append({
                "release_id": release_id,
                "ok": False,
                "error": evaluation.get("error"),
            })
            continue
        if (previous or {}).get("conclusion") != evaluation.get("conclusion"):
            changed_count += 1
        results.append({
            "release_id": release_id,
            "ok": True,
            "previous_conclusion": (previous or {}).get("conclusion"),
            "new_conclusion": evaluation.get("conclusion"),
        })

    return {
        "ok": error_count == 0,
        "target_count": len(target_ids),
        "processed_count": len(results),
        "changed_count": changed_count,
        "error_count": error_count,
        "results": results,
    }
