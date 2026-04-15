"""Phase 6-E: Recommendation / Decision Registry 管理.

让 recommendation 成为受治理对象：
  - recommendation_registry.json: 所有历史建议
  - active_decision_registry.json: 当前 family/tf 运营状态
  - evidence_bundle_index.json: evidence bundle 引用索引

数据存储策略（DB-first + 文件 fallback）:
  - 写入: 同时写 DB + 文件（DB 失败不阻塞文件写入）
  - 读取: DB 优先 → 文件 fallback
  - DB 开关: 环境变量 AATS_ACTIVE_PARAMETER_DB_URL
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._db_util import try_governance_db

log = logging.getLogger(__name__)


# ── DB 辅助 ──────────────────────────────────────────────────────────

def _db_sync_recommendation(rec: dict[str, Any]) -> None:
    """将单个 recommendation dict 同步到 DB（best-effort）."""
    engine, ok = try_governance_db()
    if not ok:
        return
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import db_upsert_recommendation

        with Session(engine) as session, session.begin():
            db_upsert_recommendation(
                session,
                recommendation_id=rec["recommendation_id"],
                family=rec["family"],
                timeframe=rec["timeframe"],
                recommendation_type=rec.get("recommendation_type", "require_review"),
                confidence=rec.get("confidence", "low"),
                reason=rec.get("reason", ""),
                symbol=rec.get("symbol", "BTC-USDT-SWAP"),
                target_parameter_set_id=rec.get("target_parameter_set_id"),
                evidence_bundle_ref=rec.get("evidence_bundle_ref"),
                status=rec.get("status", "draft"),
                approved_by=rec.get("approved_by"),
                approved_at=rec.get("approved_at"),
                review_notes=rec.get("review_notes"),
                rejected_by=rec.get("rejected_by"),
                rejected_at=rec.get("rejected_at"),
                superseded_by=rec.get("superseded_by"),
                superseded_at=rec.get("superseded_at"),
                superseded_by_recommendation_id=rec.get("superseded_by_recommendation_id"),
                created_at=rec.get("created_at"),
            )
    except Exception as exc:
        log.warning("recommendation_registry: DB 写入失败 (%s)", exc)
    finally:
        if engine is not None:
            engine.dispose()


def _db_update_rec_status(rec: dict[str, Any]) -> None:
    """将 recommendation 状态变更同步到 DB（best-effort）."""
    engine, ok = try_governance_db()
    if not ok:
        return
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import db_update_recommendation_status

        with Session(engine) as session, session.begin():
            db_update_recommendation_status(
                session,
                rec["recommendation_id"],
                status=rec["status"],
                approved_by=rec.get("approved_by"),
                approved_at=rec.get("approved_at"),
                review_notes=rec.get("review_notes"),
                rejected_by=rec.get("rejected_by"),
                rejected_at=rec.get("rejected_at"),
                superseded_by=rec.get("superseded_by"),
                superseded_at=rec.get("superseded_at"),
                superseded_by_recommendation_id=rec.get("superseded_by_recommendation_id"),
            )
    except Exception as exc:
        log.warning("recommendation_registry: DB 状态更新失败 (%s)", exc)
    finally:
        if engine is not None:
            engine.dispose()


def _db_sync_active_decision(
    family: str, timeframe: str, current_status: str,
    active_parameter_set_id: str | None = None,
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """将 active decision UPSERT 同步到 DB（best-effort）."""
    engine, ok = try_governance_db()
    if not ok:
        return
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import db_upsert_active_decision

        with Session(engine) as session, session.begin():
            db_upsert_active_decision(
                session,
                family=family, timeframe=timeframe,
                current_status=current_status,
                active_parameter_set_id=active_parameter_set_id,
                last_recommendation_id=last_recommendation_id,
                notes=notes,
            )
    except Exception as exc:
        log.warning("recommendation_registry: DB active_decision 写入失败 (%s)", exc)
    finally:
        if engine is not None:
            engine.dispose()

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


def load_recommendation_registry(path: pathlib.Path, *, skip_db: bool = False) -> dict[str, Any]:
    """加载 recommendation registry.

    优先级: DB → 文件 → 空 registry。skip_db=True 跳过 DB 直接读文件。
    """
    if not skip_db:
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.recommendations_db import db_load_recommendation_registry

                with Session(engine) as session:
                    registry = db_load_recommendation_registry(session)
                if registry.get("recommendations"):
                    log.info("从数据库加载 recommendation registry (%d recommendations)",
                             len(registry["recommendations"]))
                    return registry
                log.debug("recommendation_registry: DB 为空，fallback 到文件")
            except Exception as exc:
                log.warning("recommendation_registry: DB 读取失败 (%s)，fallback 到文件", exc)
            finally:
                if engine is not None:
                    engine.dispose()

    if not path.exists():
        return {"generated_at": None, "recommendations": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_recommendation_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    registry["version"] = registry.get("version", 0) + 1
    atomic_json_write(registry, path)
    log.info("保存 recommendation registry -> %s (v%d, %d items)",
             path, registry["version"], len(registry.get("recommendations", [])))


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
    """将新 recommendation 加入 registry.

    同一 ``(family, symbol, timeframe, recommendation_type)`` 下已有的
    **draft** 建议会被自动标记为 ``superseded``，避免审批队列无限膨胀。

    不同类型的 recommendation 需要并存。例如同一轮里既可能有
    ``parameter_upgrade``，也可能有 ``pause`` / ``require_review``，
    前者代表“本轮生成了候选参数”，后者代表“当前治理建议不要推进”。
    这两类信息不能互相覆盖，否则 operator 会误以为系统没有产出参数候选。

    已 approved / rejected 等终态记录不受影响。
    """
    new_family = rec.get("family")
    new_symbol = rec.get("symbol")
    new_tf = rec.get("timeframe")
    new_type = rec.get("recommendation_type")
    new_id = rec.get("recommendation_id")

    for existing in registry.get("recommendations", []):
        if (
            existing.get("status") == "draft"
            and existing.get("family") == new_family
            and existing.get("symbol") == new_symbol
            and existing.get("timeframe") == new_tf
            and existing.get("recommendation_type") == new_type
        ):
            existing["status"] = "superseded"
            existing["superseded_at"] = rec.get("created_at")
            existing["superseded_by"] = "system"
            existing["superseded_by_recommendation_id"] = new_id
            _db_update_rec_status(existing)

    registry.setdefault("recommendations", []).append(rec)
    _db_sync_recommendation(rec)


def find_recommendation(
    registry: dict[str, Any], recommendation_id: str,
) -> dict[str, Any] | None:
    """按 recommendation_id 查找."""
    for rec in registry.get("recommendations", []):
        if rec.get("recommendation_id") == recommendation_id:
            return rec
    return None


# ── Recommendation 状态流转 ──────────────────────────────────────────


def approve_recommendation(
    registry: dict[str, Any],
    recommendation_id: str,
    *,
    approved_by: str = "operator",
    notes: str | None = None,
) -> dict[str, Any] | None:
    """将 recommendation 从 draft → approved.

    Returns
    -------
    dict | None  被审批的 recommendation，找不到或状态非 draft 返回 None
    """
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        log.warning("approve: recommendation %s 不存在", recommendation_id)
        return None

    if rec["status"] != "draft":
        log.warning(
            "approve: recommendation %s 状态为 %s（非 draft），拒绝审批",
            recommendation_id, rec["status"],
        )
        return None

    rec["status"] = "approved"
    rec["approved_by"] = approved_by
    rec["approved_at"] = datetime.now(timezone.utc).isoformat()
    if notes:
        rec["review_notes"] = notes
    _db_update_rec_status(rec)
    return rec


def reject_recommendation(
    registry: dict[str, Any],
    recommendation_id: str,
    *,
    rejected_by: str = "operator",
    notes: str | None = None,
) -> dict[str, Any] | None:
    """将 recommendation 从 draft → rejected."""
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        log.warning("reject: recommendation %s 不存在", recommendation_id)
        return None

    if rec["status"] != "draft":
        log.warning(
            "reject: recommendation %s 状态为 %s（非 draft），拒绝驳回",
            recommendation_id, rec["status"],
        )
        return None

    rec["status"] = "rejected"
    rec["rejected_by"] = rejected_by
    rec["rejected_at"] = datetime.now(timezone.utc).isoformat()
    if notes:
        rec["review_notes"] = notes
    _db_update_rec_status(rec)
    return rec


def supersede_recommendation(
    registry: dict[str, Any],
    recommendation_id: str,
    *,
    superseded_by_id: str | None = None,
    actor: str = "system",
    notes: str | None = None,
) -> dict[str, Any] | None:
    """将 recommendation 标记为 superseded.

    当新 recommendation 替代旧 recommendation 时使用。
    """
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        log.warning("supersede: recommendation %s 不存在", recommendation_id)
        return None

    rec["status"] = "superseded"
    rec["superseded_at"] = datetime.now(timezone.utc).isoformat()
    rec["superseded_by"] = actor
    if superseded_by_id:
        rec["superseded_by_recommendation_id"] = superseded_by_id
    if notes:
        rec["review_notes"] = notes
    _db_update_rec_status(rec)
    return rec


# ── Active Decision Registry ────────────────────────────────────────


def load_active_decision_registry(path: pathlib.Path, *, skip_db: bool = False) -> dict[str, Any]:
    """加载 active decision registry.

    优先级: DB → 文件 → 空 registry。skip_db=True 跳过 DB 直接读文件。
    """
    if not skip_db:
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.recommendations_db import db_load_active_decisions

                with Session(engine) as session:
                    registry = db_load_active_decisions(session)
                if registry.get("decisions"):
                    log.info("从数据库加载 active decision registry (%d decisions)",
                             len(registry["decisions"]))
                    return registry
                log.debug("active_decision_registry: DB 为空，fallback 到文件")
            except Exception as exc:
                log.warning("active_decision_registry: DB 读取失败 (%s)，fallback 到文件", exc)
            finally:
                if engine is not None:
                    engine.dispose()

    if not path.exists():
        return {"generated_at": None, "decisions": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_active_decision_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    registry["version"] = registry.get("version", 0) + 1
    atomic_json_write(registry, path)
    log.info("保存 active decision registry -> %s (v%d, %d items)",
             path, registry["version"], len(registry.get("decisions", [])))


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

    _db_sync_active_decision(
        family, timeframe, current_status,
        active_parameter_set_id=active_parameter_set_id,
        last_recommendation_id=last_recommendation_id,
        notes=notes,
    )


# ── Evidence Bundle Index ────────────────────────────────────────────


def load_evidence_bundle_index(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": None, "bundles": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_evidence_bundle_index(
    index: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    index["generated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(index, path)


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


def _sync_registries_to_db_or_raise(
    rec_reg: dict[str, Any],
    dec_reg: dict[str, Any],
) -> None:
    """将最新 registry 状态批量同步到 DB。

    这一步是 Phase 5/6 在 daemon 容器内可见性的兜底收口：
    gateway/UI 读取 recommendation / active decision 时优先走 DB，
    因此这里需要确保最新 registry 至少在 DB 中是可见的。
    """
    engine, ok = try_governance_db()
    if not ok:
        log.warning("recommendation_registry: governance DB 不可用，跳过强制 registry 同步")
        return
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_upsert_active_decision,
            db_upsert_recommendation,
        )

        with Session(engine) as session, session.begin():
            for rec in rec_reg.get("recommendations", []):
                db_upsert_recommendation(
                    session,
                    recommendation_id=rec["recommendation_id"],
                    family=rec["family"],
                    symbol=rec.get("symbol", "BTC-USDT-SWAP"),
                    timeframe=rec["timeframe"],
                    recommendation_type=rec.get("recommendation_type", "require_review"),
                    target_parameter_set_id=rec.get("target_parameter_set_id"),
                    confidence=rec.get("confidence", "low"),
                    reason=rec.get("reason", ""),
                    evidence_bundle_ref=rec.get("evidence_bundle_ref"),
                    status=rec.get("status", "draft"),
                    approved_by=rec.get("approved_by"),
                    approved_at=rec.get("approved_at"),
                    review_notes=rec.get("review_notes"),
                    rejected_by=rec.get("rejected_by"),
                    rejected_at=rec.get("rejected_at"),
                    superseded_by=rec.get("superseded_by"),
                    superseded_at=rec.get("superseded_at"),
                    superseded_by_recommendation_id=rec.get("superseded_by_recommendation_id"),
                    created_at=rec.get("created_at"),
                )
            for decision in dec_reg.get("decisions", []):
                db_upsert_active_decision(
                    session,
                    family=decision["family"],
                    symbol=decision.get("symbol", "BTC-USDT-SWAP"),
                    timeframe=decision["timeframe"],
                    current_status=decision.get("current_status", "require_review"),
                    active_parameter_set_id=decision.get("active_parameter_set_id"),
                    last_recommendation_id=decision.get("last_recommendation_id"),
                    notes=decision.get("notes"),
                )
    finally:
        if engine is not None:
            engine.dispose()


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
    _sync_registries_to_db_or_raise(rec_reg, dec_reg)

    return stats
