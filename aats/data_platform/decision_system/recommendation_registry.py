"""Phase 6-E: Recommendation / Decision Registry 管理.

让 recommendation 成为受治理对象：
  - recommendation_registry.json: 所有历史建议
  - active_decision_registry.json: 当前 family/tf 运营状态
  - evidence_bundle_index.json: evidence bundle 引用索引
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

# ── Recommendation 状态 ──────────────────────────────────────────────

RECOMMENDATION_STATUSES = ("draft", "approved", "rejected", "superseded")

RECOMMENDATION_TYPES = (
    "parameter_upgrade",
    "keep_active",
    "lower_priority",
    "pause",
    "require_review",
)


# ── ID 生成 ──────────────────────────────────────────────────────────


def _make_recommendation_id() -> str:
    return f"rec_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


# ── Recommendation Registry ──────────────────────────────────────────


def load_recommendation_registry(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": None, "recommendations": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_recommendation_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False, default=str)
    log.info("保存 recommendation registry -> %s (%d items)",
             path, len(registry.get("recommendations", [])))


def create_recommendation(
    *,
    family: str,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str,
    recommendation_type: str,
    target_parameter_set_id: str | None = None,
    confidence: str,
    reason: str,
    evidence_bundle_ref: str | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    return {
        "recommendation_id": _make_recommendation_id(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "recommendation_type": recommendation_type,
        "target_parameter_set_id": target_parameter_set_id,
        "confidence": confidence,
        "reason": reason,
        "evidence_bundle_ref": evidence_bundle_ref,
        "status": status,
    }


def add_recommendation(
    registry: dict[str, Any], rec: dict[str, Any],
) -> None:
    registry.setdefault("recommendations", []).append(rec)


# ── Active Decision Registry ────────────────────────────────────────


def load_active_decision_registry(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": None, "decisions": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_active_decision_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False, default=str)
    log.info("保存 active decision registry -> %s (%d items)",
             path, len(registry.get("decisions", [])))


def upsert_active_decision(
    registry: dict[str, Any],
    *,
    family: str,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str,
    current_status: str,
    active_parameter_set_id: str | None = None,
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """更新或插入 family/timeframe 的 active decision."""
    decisions = registry.setdefault("decisions", [])
    combo_key = f"{family}_{timeframe.lower()}"

    # 查找已有
    existing = None
    for d in decisions:
        if d.get("family") == family and d.get("timeframe") == timeframe:
            existing = d
            break

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        existing["current_status"] = current_status
        existing["active_parameter_set_id"] = active_parameter_set_id
        existing["last_recommendation_id"] = last_recommendation_id
        existing["last_updated_at"] = now
        if notes:
            existing["notes"] = notes
    else:
        decisions.append({
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe,
            "combo_key": combo_key,
            "current_status": current_status,
            "active_parameter_set_id": active_parameter_set_id,
            "last_recommendation_id": last_recommendation_id,
            "last_updated_at": now,
            "notes": notes,
        })


# ── Evidence Bundle Index ────────────────────────────────────────────


def load_evidence_bundle_index(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": None, "bundles": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_evidence_bundle_index(
    index: dict[str, Any], path: pathlib.Path,
) -> None:
    index["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False, default=str)


def register_evidence_bundle(
    index: dict[str, Any],
    *,
    round_id: str,
    evidence_summary_path: str,
    phases_with_data: list[str],
    completeness_ratio: float,
) -> None:
    index.setdefault("bundles", []).append({
        "round_id": round_id,
        "evidence_summary_path": evidence_summary_path,
        "phases_with_data": phases_with_data,
        "completeness_ratio": completeness_ratio,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# ── 从 decision round 结果批量更新 ──────────────────────────────────


def update_registries_from_round(
    *,
    round_id: str,
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
    evidence_bundle: dict[str, Any],
    rec_registry_path: pathlib.Path,
    decision_registry_path: pathlib.Path,
    bundle_index_path: pathlib.Path,
    evidence_summary_path: str,
) -> dict[str, int]:
    """从 decision round 结果批量更新三个 registry.

    Returns
    -------
    dict  统计: recommendations_added, decisions_updated, bundles_registered
    """
    stats = {"recommendations_added": 0, "decisions_updated": 0, "bundles_registered": 0}

    # 1. Recommendation registry
    rec_reg = load_recommendation_registry(rec_registry_path)

    for uc in upgrade_candidates:
        # 只为有明确 decision 的参数创建 recommendation
        if uc.get("decision") in ("promote_candidate", "reject"):
            rec_type = "parameter_upgrade" if uc["decision"] == "promote_candidate" else "pause"
            rec = create_recommendation(
                family=uc["family"],
                timeframe=uc["timeframe"],
                recommendation_type=rec_type,
                target_parameter_set_id=uc.get("parameter_set_id"),
                confidence=uc.get("confidence", "low"),
                reason=uc.get("reason", ""),
                evidence_bundle_ref=round_id,
            )
            add_recommendation(rec_reg, rec)
            stats["recommendations_added"] += 1

    for ftd in ft_decisions:
        rec_type = ftd.get("decision", "require_review")
        if rec_type in RECOMMENDATION_TYPES:
            rec = create_recommendation(
                family=ftd["family"],
                timeframe=ftd["timeframe"],
                recommendation_type=rec_type,
                confidence=ftd.get("confidence", "low"),
                reason="; ".join(ftd.get("reasons", [])),
                evidence_bundle_ref=round_id,
            )
            add_recommendation(rec_reg, rec)
            stats["recommendations_added"] += 1

    save_recommendation_registry(rec_reg, rec_registry_path)

    # 2. Active decision registry
    dec_reg = load_active_decision_registry(decision_registry_path)

    # 参数升级建议 → 关联 parameter_set_id
    promoted_by_ft: dict[str, str] = {}
    for uc in upgrade_candidates:
        if uc.get("decision") == "promote_candidate":
            ft_key = f"{uc['family']}_{uc['timeframe'].lower()}"
            promoted_by_ft[ft_key] = uc.get("parameter_set_id", "")

    last_rec_ids: dict[str, str] = {}
    for rec in rec_reg.get("recommendations", []):
        ft_key = f"{rec['family']}_{rec['timeframe'].lower()}"
        last_rec_ids[ft_key] = rec["recommendation_id"]

    for ftd in ft_decisions:
        ft_key = ftd.get("combo_key", "")
        upsert_active_decision(
            dec_reg,
            family=ftd["family"],
            timeframe=ftd["timeframe"],
            current_status=ftd["decision"],
            active_parameter_set_id=promoted_by_ft.get(ft_key),
            last_recommendation_id=last_rec_ids.get(ft_key),
            notes=f"Decision round {round_id}",
        )
        stats["decisions_updated"] += 1

    save_active_decision_registry(dec_reg, decision_registry_path)

    # 3. Evidence bundle index
    bi = load_evidence_bundle_index(bundle_index_path)
    completeness = evidence_bundle.get("evidence_completeness", {})
    register_evidence_bundle(
        bi,
        round_id=round_id,
        evidence_summary_path=evidence_summary_path,
        phases_with_data=completeness.get("phases_with_data", []),
        completeness_ratio=completeness.get("completeness_ratio", 0),
    )
    save_evidence_bundle_index(bi, bundle_index_path)
    stats["bundles_registered"] += 1

    return stats
