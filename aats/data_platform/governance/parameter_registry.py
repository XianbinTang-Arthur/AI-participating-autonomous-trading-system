"""参数版本治理 — Parameter Registry.

将分散在各 round 产物中的参数结论收口为受治理对象。
每个 parameter set 有明确状态：draft / candidate / frozen / deprecated。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

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


# ── Registry 操作 ───────────────────────────────────────────────────


def load_registry(path: pathlib.Path) -> dict[str, Any]:
    """加载 current_parameter_registry.json."""
    if not path.exists():
        return {
            "generated_at": None,
            "parameter_sets": [],
        }
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry: dict[str, Any], path: pathlib.Path) -> None:
    """保存 registry."""
    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False, default=str)
    log.info("保存 registry -> %s (%d parameter sets)",
             path, len(registry.get("parameter_sets", [])))


def add_parameter_set(registry: dict[str, Any], ps: dict[str, Any]) -> None:
    """向 registry 添加一个 parameter set."""
    registry.setdefault("parameter_sets", []).append(ps)


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
    if not isinstance(candidates, dict):
        raise ValueError(f"无法解析 candidates 结构: {candidates_path}")

    result = []
    for ft_key, values in candidates.items():
        if not isinstance(values, dict):
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
            values=values,
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
