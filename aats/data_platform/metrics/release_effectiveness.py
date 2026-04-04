"""Release Effectiveness Evaluation 模块.

工作包 C: 给每次 parameter release 一个 effectiveness 评价。

评价维度:
  1. 行为层 — attribution 是否改善
  2. 执行层 — execution realism 是否恶化
  3. 运营层 — 是否触发 rollback, observation 是否完成
  4. 治理层 — evidence freshness, unresolved alerts

结论分类:
  effective / mixed / ineffective / rollback_triggered / insufficient_evidence
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


def _effectiveness_registry_path(root: Path) -> Path:
    return root / "artifacts" / "metrics" / "release_effectiveness_registry.json"


def load_effectiveness_registry(root: Path) -> dict:
    fp = _effectiveness_registry_path(root)
    if not fp.exists():
        return {"evaluations": [], "generated_at": None}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_effectiveness_registry(root: Path, data: dict) -> None:
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(_effectiveness_registry_path(root), data)


# ── 维度评估函数 ──────────────────────────────────────────────

def _evaluate_behavior(root: Path, release: dict) -> dict:
    """行为层: 检查 observation 中的 attribution 和 decision status."""
    release_id = release.get("release_id", "")
    obs = _load_json(
        root / "artifacts" / "production_workflow" / "observations"
        / release_id / "observation_summary.json"
    )
    if not obs:
        return {
            "dimension": "behavior",
            "score": "unknown",
            "detail": "no observation data",
        }

    checklist = {c["name"]: c for c in obs.get("checklist", [])}

    attr_check = checklist.get("attribution", {})
    decision_check = checklist.get("decision_status", {})

    issues = []
    if attr_check.get("status") == "regression":
        issues.append("attribution regression detected")
    if decision_check.get("status") == "regression":
        issues.append("decision status regression")

    if issues:
        return {"dimension": "behavior", "score": "negative", "detail": "; ".join(issues)}
    if attr_check.get("status") == "unknown" and decision_check.get("status") == "unknown":
        return {"dimension": "behavior", "score": "unknown", "detail": "insufficient data"}
    return {"dimension": "behavior", "score": "positive", "detail": "no regression detected"}


def _evaluate_execution(root: Path, release: dict) -> dict:
    """执行层: 检查 execution realism 是否恶化."""
    release_id = release.get("release_id", "")
    obs = _load_json(
        root / "artifacts" / "production_workflow" / "observations"
        / release_id / "observation_summary.json"
    )
    if not obs:
        return {
            "dimension": "execution",
            "score": "unknown",
            "detail": "no observation data",
        }

    checklist = {c["name"]: c for c in obs.get("checklist", [])}
    exec_check = checklist.get("execution_realism", {})

    if exec_check.get("status") == "regression":
        return {
            "dimension": "execution",
            "score": "negative",
            "detail": exec_check.get("detail", "execution regression"),
        }
    if exec_check.get("status") == "unknown":
        return {"dimension": "execution", "score": "unknown", "detail": "no execution data"}
    return {"dimension": "execution", "score": "positive", "detail": "execution stable or improved"}


def _evaluate_operations(root: Path, release: dict) -> dict:
    """运营层: 检查 rollback 和 observation 完成."""
    release_id = release.get("release_id", "")

    # rollback recommendation
    rb = _load_json(
        root / "artifacts" / "production_workflow" / "rollback_recommendations"
        / release_id / "rollback_recommendation.json"
    )
    rollback_recommended = rb.get("rollback_recommended", False) if rb else False

    if rollback_recommended:
        return {
            "dimension": "operations",
            "score": "negative",
            "detail": f"rollback recommended (severity={rb.get('severity', '?')})",
        }

    obs_status = release.get("observation_status", "unknown")
    if obs_status == "completed":
        return {"dimension": "operations", "score": "positive", "detail": "observation completed, no rollback"}
    if obs_status == "observing":
        return {"dimension": "operations", "score": "unknown", "detail": "still observing"}
    return {"dimension": "operations", "score": "unknown", "detail": f"observation_status={obs_status}"}


def _evaluate_governance(root: Path, release: dict) -> dict:
    """治理层: evidence freshness + unresolved alerts."""
    # gate status
    gate_status = release.get("gate_status", "unknown")

    # 当前 alerts
    alerts_data = _load_json(
        root / "artifacts" / "operations" / "alerts" / "current_alerts.json"
    )
    alert_count = 0
    if alerts_data:
        alert_count = sum(
            1 for a in alerts_data.get("alerts", [])
            if not a.get("acknowledged") and a.get("severity") == "critical"
        )

    issues = []
    if gate_status == "block":
        issues.append("gate was blocked")
    if alert_count > 0:
        issues.append(f"{alert_count} unresolved critical alert(s)")

    if issues:
        return {"dimension": "governance", "score": "negative", "detail": "; ".join(issues)}
    if gate_status == "warn":
        return {"dimension": "governance", "score": "mixed", "detail": "gate passed with warnings"}
    return {"dimension": "governance", "score": "positive", "detail": "governance healthy"}


# ── 综合评估 ────────���─────────────────────────────────────────

def evaluate_release_effectiveness(
    root: Path,
    release_id: str,
) -> dict:
    """评估一次 release 的 effectiveness.

    Returns:
        evaluation dict with dimensions, conclusion, detail
    """
    now = datetime.now(timezone.utc)

    # 找 release
    rel_data = _load_json(
        root / "artifacts" / "production_workflow" / "parameter_release_history.json"
    )
    release = None
    for r in (rel_data.get("releases", []) if rel_data else []):
        if r.get("release_id") == release_id:
            release = r
            break

    if release is None:
        return {"error": f"release {release_id} not found"}

    # 评估各维度
    dimensions = [
        _evaluate_behavior(root, release),
        _evaluate_execution(root, release),
        _evaluate_operations(root, release),
        _evaluate_governance(root, release),
    ]

    # 综合结论
    conclusion = _derive_effectiveness(dimensions, release)

    # 加载 baseline comparison (如果有)
    comparison = _load_json(
        root / "artifacts" / "metrics" / "release_comparisons"
        / release_id / "baseline_comparison.json"
    )
    comparison_conclusion = comparison.get("conclusion") if comparison else None

    evaluation = {
        "evaluation_id": f"eff_{now.strftime('%Y%m%d_%H%M%S')}",
        "release_id": release_id,
        "family": release.get("family"),
        "timeframe": release.get("timeframe"),
        "evaluated_at": now.isoformat(),
        "dimensions": dimensions,
        "baseline_comparison_conclusion": comparison_conclusion,
        "conclusion": conclusion,
        "detail": _effectiveness_detail(dimensions, conclusion),
    }

    # 保存到 registry
    registry = load_effectiveness_registry(root)
    # 去重: 替换同 release_id 的旧评估
    registry["evaluations"] = [
        e for e in registry["evaluations"]
        if e.get("release_id") != release_id
    ]
    registry["evaluations"].append(evaluation)
    save_effectiveness_registry(root, registry)

    return evaluation


def _derive_effectiveness(dimensions: list[dict], release: dict) -> str:
    """从各维度分数推导 effectiveness."""
    # 如果有 rollback
    ops_dim = next((d for d in dimensions if d["dimension"] == "operations"), None)
    if ops_dim and ops_dim["score"] == "negative" and "rollback" in ops_dim.get("detail", ""):
        return "rollback_triggered"

    scores = [d["score"] for d in dimensions]

    # 全部 unknown = insufficient_evidence
    if all(s == "unknown" for s in scores):
        return "insufficient_evidence"

    negatives = sum(1 for s in scores if s == "negative")
    positives = sum(1 for s in scores if s == "positive")
    unknowns = sum(1 for s in scores if s == "unknown")

    if negatives >= 2:
        return "ineffective"
    if negatives == 0 and positives >= 2:
        return "effective"
    if unknowns >= 3:
        return "insufficient_evidence"
    return "mixed"


def _effectiveness_detail(dimensions: list[dict], conclusion: str) -> str:
    """生成 effectiveness 描��."""
    parts = [f"conclusion={conclusion}"]
    for d in dimensions:
        parts.append(f"{d['dimension']}={d['score']}")
    return "; ".join(parts)


def find_effectiveness(root: Path, release_id: str) -> dict | None:
    """查找指定 release 的 effectiveness 评估."""
    registry = load_effectiveness_registry(root)
    for e in registry.get("evaluations", []):
        if e.get("release_id") == release_id:
            return e
    return None
