"""参数版本治理 — Parameter Registry.

将分散在各 round 产物中的参数结论收口为受治理对象。
每个 parameter set 有明确状态：draft / candidate / frozen / deprecated。

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

from ._db_util import try_governance_db

log = logging.getLogger(__name__)


def _db_sync_single(ps: dict[str, Any]) -> None:
    """将单个 parameter_set dict 同步到 DB（best-effort）."""
    engine, ok = try_governance_db()
    if not ok:
        return
    try:
        from sqlalchemy.orm import Session

        from .parameter_sets_db import db_upsert_parameter_set

        with Session(engine) as session, session.begin():
            db_upsert_parameter_set(
                session,
                parameter_set_id=ps["parameter_set_id"],
                family=ps["family"],
                timeframe=ps["timeframe"],
                values=ps.get("values", {}),
                status=ps.get("status", "draft"),
                symbol=ps.get("symbol", "BTC-USDT-SWAP"),
                source_round_id=ps.get("source_round_id"),
                source_phase=ps.get("source_phase"),
                dataset_version=ps.get("dataset_version", "v1.0"),
                confidence=ps.get("confidence"),
                created_at=ps.get("created_at"),
                frozen_at=ps.get("frozen_at"),
                deprecated_at=ps.get("deprecated_at"),
                notes=ps.get("notes"),
            )
    except Exception as exc:
        log.warning("parameter_registry: DB 写入失败 (%s)", exc)
    finally:
        if engine is not None:
            engine.dispose()


def _db_update_status(ps_id: str, status: str, frozen_at: str | None, deprecated_at: str | None, notes: str | None) -> None:
    """更新 DB 中的 parameter_set 状态（best-effort）."""
    engine, ok = try_governance_db()
    if not ok:
        return
    try:
        from sqlalchemy.orm import Session

        from .parameter_sets_db import db_update_parameter_set_status

        with Session(engine) as session, session.begin():
            db_update_parameter_set_status(
                session, ps_id,
                status=status,
                frozen_at=frozen_at,
                deprecated_at=deprecated_at,
                notes=notes,
            )
    except Exception as exc:
        log.warning("parameter_registry: DB 状态更新失败 (%s)", exc)
    finally:
        if engine is not None:
            engine.dispose()

# ── 状态定义 ─────────────────────────────────────────────────────────

VALID_STATUSES: set[str] = {"draft", "candidate", "frozen", "deprecated"}

# ── Parameter Set 结构 ───────────────────────────────────────────────


def _make_parameter_set_id() -> str:
    return f"ps_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def create_parameter_set(
    *,
    family: str,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str,
    source_round_id: str | None = None,
    source_phase: str | None = None,
    dataset_version: str = "v1.0",
    values: dict[str, Any],
    confidence: str | None = None,
    status: str = "draft",
    notes: str | None = None,
) -> dict[str, Any]:
    """创建一个新的 parameter set 记录."""
    if status not in VALID_STATUSES:
        raise ValueError(f"非法 status: {status}, 合法值: {sorted(VALID_STATUSES)}")

    return {
        "parameter_set_id": _make_parameter_set_id(),
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "source_round_id": source_round_id,
        "source_phase": source_phase,
        "dataset_version": dataset_version,
        "values": dict(values),
        "confidence": confidence,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "frozen_at": None,
        "deprecated_at": None,
        "notes": notes,
    }


def _sanitize_candidate_values(
    ft_key: str,
    values: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if "_note" in values:
        return None, f"{ft_key}: placeholder candidate (_note) is not importable"

    sanitized: dict[str, Any] = {}
    for key, value in values.items():
        if str(key).startswith("_"):
            continue
        if value is None:
            return None, f"{ft_key}: contains None for '{key}'"
        sanitized[key] = value

    if not sanitized:
        return None, f"{ft_key}: candidate is empty after sanitization"

    return sanitized, None


def _infer_candidate_confidence(
    ft_key: str,
    pending_validation: list[str],
) -> str | None:
    if not pending_validation:
        return "medium"
    suffix = f" in {ft_key}"
    return "low" if any(item.endswith(suffix) for item in pending_validation) else "medium"


# ── Registry 操作 ───────────────────────────────────────────────────


def load_registry(path: pathlib.Path, *, skip_db: bool = False) -> dict[str, Any]:
    """加载 parameter registry.

    优先级: DB (AATS_ACTIVE_PARAMETER_DB_URL) → JSON 文件 → 空 registry。
    skip_db=True 时跳过 DB 直接读文件（用于 seed-db 等需要文件数据的场景）。
    """
    if not skip_db:
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from .parameter_sets_db import db_load_full_registry

                with Session(engine) as session:
                    registry = db_load_full_registry(session)
                # DB 是真源：空表也直接返回，避免把旧 JSON 参数集重新污染 replay/scan/Step3 默认值
                log.info("从数据库加载 parameter registry (%d parameter sets)",
                         len(registry.get("parameter_sets", [])))
                return registry
            except Exception as exc:
                log.warning("parameter_registry: DB 读取失败 (%s)，fallback 到文件（stale 风险）", exc)
            finally:
                if engine is not None:
                    engine.dispose()

    if not path.exists():
        return {
            "generated_at": None,
            "parameter_sets": [],
        }
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry: dict[str, Any], path: pathlib.Path) -> None:
    """保存 registry（原子写入，防止并发损坏）."""
    from ._atomic_io import atomic_json_write

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    # 版本号递增
    registry["version"] = registry.get("version", 0) + 1
    atomic_json_write(registry, path)
    log.info("保存 registry -> %s (v%d, %d parameter sets)",
             path, registry["version"], len(registry.get("parameter_sets", [])))


def add_parameter_set(registry: dict[str, Any], ps: dict[str, Any]) -> None:
    """向 registry 添加一个 parameter set."""
    registry.setdefault("parameter_sets", []).append(ps)
    _db_sync_single(ps)


def find_parameter_sets(
    registry: dict[str, Any],
    *,
    family: str | None = None,
    timeframe: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """按条件检索 parameter sets."""
    results = []
    for ps in registry.get("parameter_sets", []):
        if family and ps.get("family") != family:
            continue
        if timeframe and ps.get("timeframe") != timeframe:
            continue
        if status and ps.get("status") != status:
            continue
        results.append(ps)
    return results


def freeze_parameter_set(
    registry: dict[str, Any],
    parameter_set_id: str,
    *,
    notes: str | None = None,
) -> bool:
    """将一个 parameter set 冻结.

    Returns True 如果成功冻结.

    TODO(rdp-future-freeze-api) Bug 9 forward-compat / R4 roadmap
    ================================================================
    当前状态 (2026-04-19):
      - 本函数零调用方 (grep 过 aats/ scripts/ tests/)
      - scripts/rdp_freeze_parameter_set.py 已 stub 化 (exit=2)，原 CLI 通道断
      - API 端点 POST /rdp/parameters/freeze 未实现 (apply_token 白名单预留
        'freeze' action 但无对应 route)
      - DB 里 parameter_sets.status='frozen' 从未被写入过

    Bug 9 (2026-04-19) 的 apply 路径让 candidate 直升 released，跳过 frozen。
    当前 OK 因为 frozen 本身就是 "计划未交付" 状态。

    未来 freeze API 恢复时需要同步改 (grep TODO(rdp-future-freeze-api) 定位):
      1. active_parameter_apply.apply_approved_recommendation: 改为
         候选 → (freeze API) frozen → (apply) released 双阶段，保留 frozen
         作为"审批后冻结"阶段。
      2. validate_rollback_target 规则 2 已经接受 frozen，Bug 8 的 deprecated
         时间门控退化为备选路径。
      3. evidence_bundle.py / baseline_comparison.py 的 status=='frozen' 读端
         会自动激活。
      4. rdp_routes.py 实现 POST /rdp/parameters/freeze 端点。
    """
    for ps in registry.get("parameter_sets", []):
        if ps["parameter_set_id"] == parameter_set_id:
            if ps["status"] == "frozen":
                log.warning("parameter set %s 已经是 frozen 状态", parameter_set_id)
                return False
            if ps["status"] == "deprecated":
                log.warning("parameter set %s 已 deprecated，不能冻结", parameter_set_id)
                return False
            ps["status"] = "frozen"
            ps["frozen_at"] = datetime.now(timezone.utc).isoformat()
            if notes:
                ps["notes"] = notes
            log.info("已冻结 parameter set: %s", parameter_set_id)
            _db_update_status(parameter_set_id, "frozen", ps["frozen_at"], None, notes)
            return True
    log.error("未找到 parameter set: %s", parameter_set_id)
    return False


def deprecate_parameter_set(
    registry: dict[str, Any],
    parameter_set_id: str,
    *,
    notes: str | None = None,
) -> bool:
    """将一个 parameter set 标记为 deprecated."""
    for ps in registry.get("parameter_sets", []):
        if ps["parameter_set_id"] == parameter_set_id:
            ps["status"] = "deprecated"
            ps["deprecated_at"] = datetime.now(timezone.utc).isoformat()
            if notes:
                ps["notes"] = notes
            log.info("已 deprecate parameter set: %s", parameter_set_id)
            _db_update_status(parameter_set_id, "deprecated", None, ps["deprecated_at"], notes)
            return True
    log.error("未找到 parameter set: %s", parameter_set_id)
    return False


# ── 从已有 artifact 导入 ─────────────────────────────────────────────


def import_from_parameter_candidates(
    candidates_path: pathlib.Path,
    *,
    source_round_id: str | None = None,
    source_phase: str = "phase2_step2",
    dataset_version: str = "v1.0",
    symbol: str = "BTC-USDT-SWAP",
    initial_status: str = "draft",
) -> list[dict[str, Any]]:
    """从 parameter_candidates.json 导入参数.

    parameter_candidates.json 格式:
    {
      "candidates": {
        "independent_15m": {"signal_edge_scale_bps": 12.0, ...},
        "directional_1h":  {"min_confirm_ticks": 3, ...}
      }
    }
    """
    with candidates_path.open(encoding="utf-8") as f:
        data = json.load(f)

    candidates = data.get("candidates", data)
    pending_validation = [
        str(item) for item in data.get("pending_validation", [])
        if str(item).strip()
    ]
    if not isinstance(candidates, dict):
        raise ValueError(f"无法解析 candidates 结构: {candidates_path}")

    result = []
    for ft_key, values in candidates.items():
        if not isinstance(values, dict):
            log.warning("Skip non-dict candidate %s from %s", ft_key, candidates_path)
            continue
        sanitized_values, skip_reason = _sanitize_candidate_values(ft_key, values)
        if sanitized_values is None:
            log.warning("Skip candidate %s from %s: %s", ft_key, candidates_path, skip_reason)
            continue
        # 解析 family_timeframe
        parts = ft_key.rsplit("_", 1)
        if len(parts) == 2:
            family, timeframe = parts
        else:
            family, timeframe = ft_key, "unknown"

        ps = create_parameter_set(
            family=family,
            symbol=symbol,
            timeframe=timeframe,
            source_round_id=source_round_id,
            source_phase=source_phase,
            dataset_version=dataset_version,
            values=sanitized_values,
            confidence=_infer_candidate_confidence(ft_key, pending_validation),
            status=initial_status,
            notes=f"从 {candidates_path.name} 导入",
        )
        result.append(ps)

    return result


def import_from_parameter_recommendations(
    recommendations_path: pathlib.Path,
    *,
    family: str,
    timeframe: str,
    source_round_id: str | None = None,
    source_phase: str = "phase2_step1",
    dataset_version: str = "v1.0",
    symbol: str = "BTC-USDT-SWAP",
    initial_status: str = "draft",
) -> dict[str, Any]:
    """从 parameter_recommendations.json 导入参数.

    parameter_recommendations.json 格式:
    {
      "recommendations": {
        "param_name": {"value": ..., "confidence": ..., "reason": ...},
        ...
      }
    }
    """
    with recommendations_path.open(encoding="utf-8") as f:
        data = json.load(f)

    recs = data.get("recommendations", data)
    values = {}
    confidences = []

    for param_name, rec_info in recs.items():
        if isinstance(rec_info, dict):
            values[param_name] = rec_info.get("value", rec_info)
            if rec_info.get("confidence"):
                confidences.append(rec_info["confidence"])
        else:
            values[param_name] = rec_info

    # 聚合 confidence
    overall_confidence = None
    if confidences:
        conf_map = {"high": 3, "medium": 2, "low": 1}
        avg = sum(conf_map.get(c, 1) for c in confidences) / len(confidences)
        if avg >= 2.5:
            overall_confidence = "high"
        elif avg >= 1.5:
            overall_confidence = "medium"
        else:
            overall_confidence = "low"

    return create_parameter_set(
        family=family,
        symbol=symbol,
        timeframe=timeframe,
        source_round_id=source_round_id,
        source_phase=source_phase,
        dataset_version=dataset_version,
        values=values,
        confidence=overall_confidence,
        status=initial_status,
        notes=f"从 {recommendations_path.name} 导入",
    )
