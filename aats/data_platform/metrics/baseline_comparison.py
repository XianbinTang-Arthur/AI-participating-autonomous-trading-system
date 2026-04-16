"""Baseline / Version Comparison 模块.

工作包 B: 让每次 release 都能与历史 baseline 对比。
支持多种 baseline 来源:
  - 上一版 active parameter set
  - 最近 frozen parameter set
  - 同 family/timeframe 的最近稳定 release
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


# ── Baseline 查找 ─────────────────────────────────────────────

def find_baseline_for_release(
    root: Path, release: dict
) -> dict | None:
    """为一个 release 找到 baseline.

    按优先级:
    1. release 中的 previous_parameter_set_id 对应的 release
    2. 同 family/timeframe 的上一个完成的 release
    3. 同 family/timeframe 的最近 frozen parameter set
    """
    family = release.get("family")
    timeframe = release.get("timeframe")
    prev_ps_id = release.get("previous_parameter_set_id")

    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )

    rel_data = load_release_history(root)
    releases = rel_data.get("releases", []) if rel_data else []

    # 策略 1: 找 previous_parameter_set_id 对应的 release
    if prev_ps_id:
        for r in reversed(releases):
            if (
                r.get("parameter_set_id") == prev_ps_id
                and r.get("release_id") != release.get("release_id")
            ):
                return {
                    "baseline_type": "previous_release",
                    "baseline_release": r,
                    "baseline_parameter_set_id": prev_ps_id,
                }

    # 策略 2: 同 family/timeframe 的上一个 release
    for r in reversed(releases):
        if (
            r.get("family") == family
            and r.get("timeframe") == timeframe
            and r.get("release_id") != release.get("release_id")
            and r.get("apply_result") == "success"
        ):
            return {
                "baseline_type": "previous_same_combo",
                "baseline_release": r,
                "baseline_parameter_set_id": r.get("parameter_set_id"),
            }

    # 策略 3: frozen parameter set
    from aats.data_platform.governance.parameter_registry import load_registry

    ps_data = load_registry(root / "artifacts" / "governance" / "current_parameter_registry.json")
    if ps_data:
        for ps in ps_data.get("parameter_sets", []):
            if (
                ps.get("family") == family
                and ps.get("timeframe") == timeframe
                and ps.get("status") == "frozen"
                and ps.get("parameter_set_id") != release.get("parameter_set_id")
            ):
                return {
                    "baseline_type": "frozen_parameter_set",
                    "baseline_release": None,
                    "baseline_parameter_set_id": ps.get("parameter_set_id"),
                }

    return None


# ── Observation 对比 ──���───────────────────────────────────────

def _load_observation(root: Path, release_id: str) -> dict | None:
    from aats.data_platform.production_workflow.observation_window import (
        load_observation_result,
    )
    return load_observation_result(root, release_id)


def _load_rollback_rec(root: Path, release_id: str) -> dict | None:
    from aats.data_platform.production_workflow.rollback_policy import (
        load_rollback_recommendation,
    )
    return load_rollback_recommendation(root, release_id)


def _find_parameter_set(root: Path, ps_id: str) -> dict | None:
    from aats.data_platform.governance.parameter_registry import load_registry

    ps_data = load_registry(root / "artifacts" / "governance" / "current_parameter_registry.json")
    if not ps_data:
        return None
    for ps in ps_data.get("parameter_sets", []):
        if ps.get("parameter_set_id") == ps_id:
            return ps
    return None


def _compare_parameter_values(
    current_ps: dict | None, baseline_ps: dict | None
) -> list[dict]:
    """比较两组参数值."""
    if not current_ps or not baseline_ps:
        return []
    cur_vals = current_ps.get("values", {})
    base_vals = baseline_ps.get("values", {})
    diffs = []
    all_keys = set(cur_vals.keys()) | set(base_vals.keys())
    for k in sorted(all_keys):
        cv = cur_vals.get(k)
        bv = base_vals.get(k)
        if cv != bv:
            diffs.append({
                "parameter": k,
                "current": cv,
                "baseline": bv,
                "delta": round(cv - bv, 6)
                if isinstance(cv, (int, float)) and isinstance(bv, (int, float))
                else None,
            })
    return diffs


def _compare_observations(
    current_obs: dict | None, baseline_obs: dict | None
) -> dict:
    """比较两次 observation."""
    if not current_obs and not baseline_obs:
        return {"available": False, "detail": "no observations"}
    if not baseline_obs:
        return {
            "available": True,
            "detail": "baseline has no observation",
            "current_status": current_obs.get("status") if current_obs else None,
            "current_recommendation": current_obs.get("recommendation") if current_obs else None,
        }
    if not current_obs:
        return {
            "available": True,
            "detail": "current has no observation yet",
            "baseline_status": baseline_obs.get("status"),
            "baseline_recommendation": baseline_obs.get("recommendation"),
        }

    cur_warnings = current_obs.get("warning_count", 0)
    base_warnings = baseline_obs.get("warning_count", 0)
    cur_regressions = current_obs.get("regression_count", 0)
    base_regressions = baseline_obs.get("regression_count", 0)

    return {
        "available": True,
        "current_status": current_obs.get("status"),
        "baseline_status": baseline_obs.get("status"),
        "current_recommendation": current_obs.get("recommendation"),
        "baseline_recommendation": baseline_obs.get("recommendation"),
        "warning_delta": cur_warnings - base_warnings,
        "regression_delta": cur_regressions - base_regressions,
        "observation_improved": (
            cur_warnings <= base_warnings and cur_regressions <= base_regressions
        ),
    }


# ── 主比较函数 ────────────────────────────────────────────────

def compare_release_to_baseline(
    root: Path,
    release_id: str,
) -> dict:
    """将指定 release 与 baseline 对比.

    Returns:
        完整比较报告 dict
    """
    now = datetime.now(timezone.utc)

    # 找到 release
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

    # 找 baseline
    baseline_info = find_baseline_for_release(root, release)

    comparison = {
        "comparison_id": f"cmp_{now.strftime('%Y%m%d_%H%M%S')}",
        "generated_at": now.isoformat(),
        "release_id": release_id,
        "family": release.get("family"),
        "timeframe": release.get("timeframe"),
        "current_parameter_set_id": release.get("parameter_set_id"),
        "baseline_found": baseline_info is not None,
    }

    if baseline_info is None:
        comparison["baseline_type"] = None
        comparison["conclusion"] = "no_baseline"
        comparison["detail"] = "no baseline found for comparison"
        _save_comparison(root, release_id, comparison)
        return comparison

    comparison["baseline_type"] = baseline_info["baseline_type"]
    comparison["baseline_parameter_set_id"] = baseline_info[
        "baseline_parameter_set_id"
    ]

    baseline_release = baseline_info.get("baseline_release")
    if baseline_release:
        comparison["baseline_release_id"] = baseline_release.get("release_id")

    # 1. 参数值对比
    cur_ps = _find_parameter_set(root, release.get("parameter_set_id", ""))
    base_ps = _find_parameter_set(
        root, baseline_info["baseline_parameter_set_id"] or ""
    )
    comparison["parameter_diffs"] = _compare_parameter_values(cur_ps, base_ps)

    # 2. Observation 对比
    cur_obs = _load_observation(root, release_id)
    base_obs = None
    if baseline_release:
        base_obs = _load_observation(
            root, baseline_release.get("release_id", "")
        )
    comparison["observation_comparison"] = _compare_observations(
        cur_obs, base_obs
    )

    # 3. Rollback recommendation 对比
    cur_rb = _load_rollback_rec(root, release_id)
    comparison["rollback_info"] = {
        "current_rollback_recommended": (
            cur_rb.get("rollback_recommended", False) if cur_rb else False
        ),
        "current_severity": cur_rb.get("severity") if cur_rb else None,
    }

    # 4. 综合结论
    comparison["conclusion"] = _derive_conclusion(comparison)
    comparison["detail"] = _derive_detail(comparison)

    _save_comparison(root, release_id, comparison)
    return comparison


def _derive_conclusion(comparison: dict) -> str:
    """推导对比结论: improvement / regression / neutral / insufficient_evidence."""
    if comparison.get("rollback_info", {}).get("current_rollback_recommended"):
        return "regression"

    obs_cmp = comparison.get("observation_comparison", {})
    if not obs_cmp.get("available"):
        return "insufficient_evidence"

    if obs_cmp.get("observation_improved"):
        return "improvement"

    reg_delta = obs_cmp.get("regression_delta", 0)
    if reg_delta > 0:
        return "regression"

    warn_delta = obs_cmp.get("warning_delta", 0)
    if warn_delta > 0:
        return "regression"

    cur_rec = obs_cmp.get("current_recommendation")
    if cur_rec == "rollback_recommended":
        return "regression"
    if cur_rec == "keep":
        return "improvement"

    return "neutral"


def _derive_detail(comparison: dict) -> str:
    """生成可读的对比说明."""
    conclusion = comparison.get("conclusion", "unknown")
    parts = [f"conclusion={conclusion}"]

    diffs = comparison.get("parameter_diffs", [])
    if diffs:
        parts.append(f"{len(diffs)} parameter(s) changed")

    obs = comparison.get("observation_comparison", {})
    if obs.get("available"):
        parts.append(
            f"obs: {obs.get('current_status', '?')} vs {obs.get('baseline_status', '?')}"
        )

    return "; ".join(parts)


def _save_comparison(root: Path, release_id: str, data: dict) -> Path:
    """保存比较结果."""
    out_dir = root / "artifacts" / "metrics" / "release_comparisons" / release_id
    json_path = out_dir / "baseline_comparison.json"
    _atomic_write_json(json_path, data)

    # 同时生成 markdown 报告
    md_path = out_dir / "baseline_comparison_report.md"
    md = _generate_comparison_md(data)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")

    return json_path


def _generate_comparison_md(data: dict) -> str:
    """生成 markdown 比较报告."""
    lines = [
        "# Baseline Comparison Report",
        "",
        f"**Release:** {data.get('release_id')}",
        f"**Family:** {data.get('family')} | **Timeframe:** {data.get('timeframe')}",
        f"**Generated:** {data.get('generated_at')}",
        f"**Baseline Type:** {data.get('baseline_type', 'N/A')}",
        f"**Conclusion:** `{data.get('conclusion', 'unknown')}`",
        "",
    ]

    # 参数差异
    diffs = data.get("parameter_diffs", [])
    if diffs:
        lines.append("## Parameter Changes")
        lines.append("")
        lines.append("| Parameter | Current | Baseline | Delta |")
        lines.append("|-----------|---------|----------|-------|")
        for d in diffs:
            lines.append(
                f"| {d['parameter']} | {d['current']} | {d['baseline']} | {d.get('delta', '-')} |"
            )
        lines.append("")

    # Observation 对比
    obs = data.get("observation_comparison", {})
    if obs.get("available"):
        lines.append("## Observation Comparison")
        lines.append("")
        lines.append(f"- Current status: `{obs.get('current_status', '?')}`")
        lines.append(f"- Baseline status: `{obs.get('baseline_status', '?')}`")
        lines.append(
            f"- Current recommendation: `{obs.get('current_recommendation', '?')}`"
        )
        lines.append(f"- Warning delta: {obs.get('warning_delta', 0)}")
        lines.append(f"- Regression delta: {obs.get('regression_delta', 0)}")
        lines.append(
            f"- Improved: {'Yes' if obs.get('observation_improved') else 'No'}"
        )
        lines.append("")

    # Rollback info
    rb = data.get("rollback_info", {})
    if rb.get("current_rollback_recommended"):
        lines.append("## Rollback")
        lines.append("")
        lines.append(
            f"Rollback recommended with severity: `{rb.get('current_severity')}`"
        )
        lines.append("")

    return "\n".join(lines)
