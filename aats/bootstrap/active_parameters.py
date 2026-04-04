"""Active Parameter Set 加载器.

主交易系统启动时读取 active parameter set，
将 RDP 治理层产出的研究参数注入 family/timeframe 配置。

参数优先级（从低到高）:
    hardcoded defaults
      < strategy_profiles/*.yaml
      < active parameter set          ← 本模块负责
      < runtime emergency override

支持两种存储格式:
  1. 单文件 registry:  configs/active_parameter_sets/active_parameter_registry.json
  2. 多文件模式:       configs/active_parameter_sets/<family>_<timeframe>.json

API:
  load_active_parameter_registry(path) -> dict
  get_active_parameters(registry, family, timeframe) -> dict
  merge_active_parameters(base_params, active_params) -> dict
  build_settings_overrides(...) -> dict[str, Any]
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── 已知的 family × timeframe 组合 ─────────────────────────────────

KNOWN_COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1h"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1h"},
]

# ── RDP 参数 → 主系统设置字段的映射 ────────────────────────────────
#
# ⚠️  语义映射说明（P1 review item）
#
# 本映射表定义了 RDP 研究层参数名 → 主系统 AATSSettings 字段名的对应关系。
# 每条映射标注了映射类型:
#   [DIRECT]      — 同义映射，RDP 参数与生产字段描述同一概念
#   [APPROXIMATE]  — 近似映射，RDP 参数语义接近但不完全等同生产字段
#   [PLACEHOLDER] — 第一版占位，需后续确认语义是否准确
#
# 修改此映射前，必须同时更新:
#   1. docs/operations/parameter_mapping_reference.md
#   2. 确认 RDP 研究层计算该参数时使用的单位与生产端一致
#
# ────────────────────────────────────────────────────────────────────

PARAMETER_MAPPING_INDEPENDENT: dict[str, str] = {
    # [DIRECT] RDP 回测优化的信号边际阈值 (bps)
    # → 生产端 independent hedge 的 de-risk 净边际阈值 (bps)
    # 单位一致: bps; 语义: 最低要求的信号净收益边际
    "signal_edge_scale_bps": "strategy_hedge_independent_de_risk_net_edge_bps",

    # [DIRECT] RDP 回测优化的最小确认 tick 数
    # → 生产端 independent hedge 的确认 tick 数
    # 单位一致: tick count; 语义: 信号确认所需的最少 tick
    "min_confirm_ticks": "strategy_hedge_independent_min_confirm_ticks",

    # [DIRECT] RDP 回测的最小安全净边际 (bps)
    # → 生产端 independent hedge 的最小安全净边际 (bps)
    # 单位一致: bps; 语义: 交易执行的净边际安全线
    "min_safe_net_edge_bps": "strategy_hedge_independent_min_safe_net_edge_bps",

    # [APPROXIMATE] RDP 回测的分数稳定性阈值 → 生产端最小 score drawdown (bps)
    # RDP 端: score_stability_threshold 衡量分数波动容忍度（无量纲比率 0~1）
    # 生产端: min_score_drawdown_bps 是分数回撤的 bps 阈值
    # ⚠️ 语义张力: RDP 是"稳定性容忍度"，生产是"回撤 bps 门槛"
    #    第一版假设: threshold * 100 ≈ bps 的近似换算在回测校准中完成
    #    TODO: 确认 RDP Phase 2 输出此值时的单位是否已经是 bps
    "score_stability_threshold": "strategy_hedge_independent_min_score_drawdown_bps",
}

PARAMETER_MAPPING_DIRECTIONAL: dict[str, str] = {
    # [PLACEHOLDER] RDP 方向性策略的趋势权重 → 生产端 entry alpha 最小值
    # RDP 端: directional_trend_weight 是趋势信号在综合评分中的权重 (0~1)
    # 生产端: strategy_entry_alpha_min 是入场信号的最小 alpha 阈值
    # ⚠️ 语义张力较大: "权重" ≠ "最小阈值"
    #    第一版占位: 假设 trend_weight 越高 → 要求的 alpha_min 越高
    #    TODO: 需要明确两者的数学关系，或拆成独立映射
    "directional_trend_weight": "strategy_entry_alpha_min",

    # [DIRECT] RDP 回测使用的 taker 手续费 → 生产端衍生品 taker 费
    # 单位一致: bps; 语义: 同一概念
    "taker_fee_bps": "trade_cost_derivatives_taker_fee_bps",

    # [DIRECT] RDP 回测使用的滑点估计 → 生产端衍生品滑点
    # 单位一致: bps; 语义: 同一概念
    "slippage_bps": "trade_cost_derivatives_slippage_bps",
}

FAMILY_PARAMETER_MAPPINGS: dict[str, dict[str, str]] = {
    "independent": PARAMETER_MAPPING_INDEPENDENT,
    "directional": PARAMETER_MAPPING_DIRECTIONAL,
}

# ── 默认路径 ───────────────────────────────────────────────────────

DEFAULT_ACTIVE_DIR = "configs/active_parameter_sets"
DEFAULT_REGISTRY_FILENAME = "active_parameter_registry.json"


# ══════════════════════════════════════════════════════════════════
#  单文件 Registry 格式（MVP 推荐）
# ══════════════════════════════════════════════════════════════════


def _default_registry_path(project_root: Path | str | None = None) -> Path:
    root = Path(project_root) if project_root else Path(".")
    return root / DEFAULT_ACTIVE_DIR / DEFAULT_REGISTRY_FILENAME


def load_active_parameter_registry(
    path: Path | str | None = None,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """加载 active_parameter_registry.json.

    格式::

        {
          "generated_at": "...",
          "active_sets": {
            "independent_15m": {
              "parameter_set_id": "ps_xxx",
              "family": "independent",
              "timeframe": "15m",
              "values": { ... }
            },
            ...
          }
        }

    如果文件不存在，返回空 registry（不中断主系统）。
    """
    if path is None:
        path = _default_registry_path(project_root)
    else:
        path = Path(path)

    if not path.exists():
        log.info("active parameter registry 不存在: %s（使用默认配置）", path)
        return {"generated_at": None, "active_sets": {}}

    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("无法加载 active parameter registry %s: %s（fallback 默认配置）", path, exc)
        return {"generated_at": None, "active_sets": {}}

    if "active_sets" not in data:
        log.warning("active parameter registry %s 缺少 active_sets 字段", path)
        return {"generated_at": data.get("generated_at"), "active_sets": {}}

    loaded_count = len(data["active_sets"])
    log.info("已加载 active parameter registry: %s (%d active sets)", path, loaded_count)
    return data


def get_active_parameters(
    registry: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    """从 registry 获取指定 combo 的参数值.

    如果不存在，返回空 dict（fallback 到原始配置）。
    """
    combo_key = f"{family}_{timeframe.lower()}"
    entry = registry.get("active_sets", {}).get(combo_key)
    if entry is None:
        return {}
    return entry.get("values", {})


def merge_active_parameters(
    base_params: dict[str, Any],
    active_params: dict[str, Any],
) -> dict[str, Any]:
    """合并 active 参数到 base 参数.

    active 覆盖 base，未提供的字段 fallback 原值。
    """
    merged = dict(base_params)
    for key, val in active_params.items():
        if val is not None:
            merged[key] = val
    return merged


def save_active_parameter_registry(
    registry: dict[str, Any],
    path: Path | str | None = None,
    *,
    project_root: Path | str | None = None,
) -> Path:
    """保存 active_parameter_registry.json（原子写入）."""
    from aats.data_platform.governance._atomic_io import atomic_json_write

    if path is None:
        path = _default_registry_path(project_root)
    else:
        path = Path(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(registry, path)
    log.info("已保存 active parameter registry -> %s", path)
    return path


# ══════════════════════════════════════════════════════════════════
#  多文件模式（兼容上一版实现）
# ══════════════════════════════════════════════════════════════════


def _resolve_active_dir(project_root: Path | str | None = None) -> Path:
    root = Path(project_root) if project_root else Path(".")
    return root / DEFAULT_ACTIVE_DIR


def load_active_parameter_set(
    family: str,
    timeframe: str,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """加载单个 family/timeframe 的 per-file active parameter set."""
    active_dir = _resolve_active_dir(project_root)
    combo_key = f"{family}_{timeframe.lower()}"
    file_path = active_dir / f"{combo_key}.json"
    if not file_path.exists():
        return None
    try:
        with file_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("无法加载 active parameter set %s: %s", file_path, exc)
        return None
    if "values" not in data:
        log.warning("active parameter set %s 缺少 values 字段", file_path)
        return None
    return data


def load_all_active_parameter_sets(
    *,
    project_root: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """加载所有 active parameter sets（registry 优先，per-file fallback）."""
    # 优先尝试 registry
    reg_path = _default_registry_path(project_root)
    if reg_path.exists():
        registry = load_active_parameter_registry(reg_path)
        active_sets = registry.get("active_sets", {})
        if active_sets:
            # 转换为与 per-file 兼容的格式
            result: dict[str, dict[str, Any]] = {}
            for combo_key, entry in active_sets.items():
                result[combo_key] = {
                    "meta": {
                        "parameter_set_id": entry.get("parameter_set_id", ""),
                        "family": entry.get("family", ""),
                        "timeframe": entry.get("timeframe", ""),
                        "status": "active",
                    },
                    "values": entry.get("values", {}),
                }
            return result

    # Fallback: per-file 模式
    active_dir = _resolve_active_dir(project_root)
    if not active_dir.exists():
        return {}

    result = {}
    for file_path in sorted(active_dir.glob("*.json")):
        if file_path.name == DEFAULT_REGISTRY_FILENAME:
            continue
        combo_key = file_path.stem
        try:
            with file_path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if "values" not in data:
            continue
        result[combo_key] = data
        log.info("已加载 per-file active parameter: %s", combo_key)

    return result


# ══════════════════════════════════════════════════════════════════
#  主系统 settings 集成
# ══════════════════════════════════════════════════════════════════


def get_active_parameter_values(
    family: str,
    timeframe: str,
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """获取指定 combo 的参数值（仅 values 部分）."""
    all_sets = load_all_active_parameter_sets(project_root=project_root)
    combo_key = f"{family}_{timeframe.lower()}"
    data = all_sets.get(combo_key)
    if data is None:
        return {}
    return data.get("values", {})


def build_settings_overrides(
    *,
    project_root: Path | str | None = None,
    registry_path: Path | str | None = None,
    families: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    """构建可合并到 AATSSettings 的参数覆盖 dict.

    根据 FAMILY_PARAMETER_MAPPINGS 将 RDP 参数名
    映射为主系统设置字段名。

    这是 active parameter → settings 注入的核心函数。
    在 build_runtime() 中被调用。
    """
    if registry_path:
        registry = load_active_parameter_registry(registry_path)
        all_sets_raw = registry.get("active_sets", {})
        # 转换
        all_sets: dict[str, dict[str, Any]] = {}
        for k, v in all_sets_raw.items():
            all_sets[k] = {"values": v.get("values", {})}
    else:
        all_sets = load_all_active_parameter_sets(project_root=project_root)

    if not all_sets:
        return {}

    overrides: dict[str, Any] = {}
    applied_combos: list[str] = []

    for combo_key, data in all_sets.items():
        parts = combo_key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        family, timeframe = parts

        if families and family not in families:
            continue
        if timeframes and timeframe.lower() not in [t.lower() for t in timeframes]:
            continue

        mapping = FAMILY_PARAMETER_MAPPINGS.get(family, {})
        values = data.get("values", {})

        for rdp_param, settings_field in mapping.items():
            if rdp_param in values:
                overrides[settings_field] = values[rdp_param]

        applied_combos.append(combo_key)

    if applied_combos:
        log.info(
            "Active parameter overrides: %d fields from %s",
            len(overrides),
            ", ".join(applied_combos),
        )

    return overrides


def apply_active_parameters_to_settings(
    resolved_settings: dict[str, Any],
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """将 active parameters 合并到已解析的 settings dict.

    用于在 build_runtime() 中注入 active parameters。
    Fail-soft: 加载失败时打 warning，返回原 settings 不修改。

    Parameters
    ----------
    resolved_settings : dict
        profile_resolution.resolved_settings
    project_root : Path, optional
        项目根目录

    Returns
    -------
    dict  合并后的 settings dict
    """
    # 检查主开关
    enabled = resolved_settings.get("active_parameters_enabled", False)
    if not enabled:
        return resolved_settings

    registry_path = resolved_settings.get("active_parameter_registry_path")

    try:
        overrides = build_settings_overrides(
            project_root=project_root,
            registry_path=registry_path,
        )
    except Exception as exc:
        log.warning(
            "Active parameter 加载失败（fallback 原配置）: %s", exc,
        )
        return resolved_settings

    if not overrides:
        log.info("Active parameter: 无覆盖项")
        return resolved_settings

    # 合并
    merged = dict(resolved_settings)
    for key, val in overrides.items():
        if key in merged:
            log.info("Active parameter override: %s = %s (was %s)", key, val, merged[key])
        merged[key] = val

    return merged


# ══════════════════════════════════════════════════════════════════
#  写入
# ══════════════════════════════════════════════════════════════════


def write_active_parameter_set(
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict[str, Any],
    source_round_id: str | None = None,
    approval_recommendation_id: str | None = None,
    applied_by: str = "manual",
    project_root: Path | str | None = None,
) -> Path:
    """写入一个 active parameter set（per-file 模式）."""
    from aats.data_platform.governance._atomic_io import atomic_json_write

    active_dir = _resolve_active_dir(project_root)
    active_dir.mkdir(parents=True, exist_ok=True)

    combo_key = f"{family}_{timeframe.lower()}"
    file_path = active_dir / f"{combo_key}.json"

    data = {
        "meta": {
            "parameter_set_id": parameter_set_id,
            "family": family,
            "timeframe": timeframe,
            "status": "active",
            "source_round_id": source_round_id,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "applied_by": applied_by,
            "approval_recommendation_id": approval_recommendation_id,
        },
        "values": dict(values),
    }

    atomic_json_write(data, file_path)
    log.info("已写入 active parameter set: %s -> %s", combo_key, file_path)
    return file_path


def upsert_active_registry(
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict[str, Any],
    project_root: Path | str | None = None,
) -> Path:
    """更新或插入 active_parameter_registry.json 中的一个 combo."""
    reg_path = _default_registry_path(project_root)
    registry = load_active_parameter_registry(reg_path)

    combo_key = f"{family}_{timeframe.lower()}"
    registry.setdefault("active_sets", {})[combo_key] = {
        "parameter_set_id": parameter_set_id,
        "family": family,
        "timeframe": timeframe,
        "values": dict(values),
    }

    return save_active_parameter_registry(registry, reg_path)


# ══════════════════════════════════════════════════════════════════
#  查询摘要（供 operator / API 使用）
# ══════════════════════════════════════════════════════════════════


def get_active_parameter_summary(
    *,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """获取所有 active parameter sets 的摘要."""
    all_sets = load_all_active_parameter_sets(project_root=project_root)

    summary_items: list[dict[str, Any]] = []
    for combo_key, data in all_sets.items():
        meta = data.get("meta", {})
        values = data.get("values", {})
        summary_items.append({
            "combo_key": combo_key,
            "family": meta.get("family", ""),
            "timeframe": meta.get("timeframe", ""),
            "parameter_set_id": meta.get("parameter_set_id", ""),
            "status": meta.get("status", ""),
            "applied_at": meta.get("applied_at", ""),
            "applied_by": meta.get("applied_by", ""),
            "approval_recommendation_id": meta.get("approval_recommendation_id"),
            "source_round_id": meta.get("source_round_id"),
            "parameter_count": len(values),
            "values": values,
        })

    return {
        "total_active_sets": len(summary_items),
        "known_combos": [c["key"] for c in KNOWN_COMBOS],
        "active_combos": [s["combo_key"] for s in summary_items],
        "missing_combos": [
            c["key"] for c in KNOWN_COMBOS
            if c["key"] not in {s["combo_key"] for s in summary_items}
        ],
        "parameter_sets": summary_items,
    }
