"""Slippage / Execution Cost 估计器 (Phase 4-C).

估计 hypothetical order 的实际滑点和执行成本。

V1 实现（Bar-Based Proxy Model）：
  - 使用 bar range (high - low) 作为 spread/depth 代理
  - 使用 sqrt(volume_ratio) 作为 market impact 代理（square root law）
  - 透明、可解释、可审查

模型公式：
  half_spread_bps = max(MIN_HALF_SPREAD, bar_range_bps * SPREAD_FRACTION)
  volume_impact_bps = bar_range_bps * sqrt(volume_ratio) * IMPACT_COEFFICIENT
  estimated_slippage_bps = half_spread_bps + volume_impact_bps

参考：
  - Kyle (1985): price impact ~ sqrt(order size / volume)
  - Almgren-Chriss (2001): 临时冲击 ~ sigma * sqrt(participation rate)
  - BTC-USDT-SWAP 实际盘口：spread 通常 0.3-0.5 bps
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)

# =========================================================================
# V1 模型参数（可配置，后续可通过真实数据校准）
# =========================================================================

# 最小半 spread（bps），即使 bar range 很小也不低于此值
# BTC-USDT-SWAP 盘口通常 ~0.3-0.5 bps
MIN_HALF_SPREAD_BPS = 0.5

# bar range 中有多少比例是 spread（V1 粗略估计）
# 对于 15m bar，bar range 包含很多 random walk，spread 占比很小
SPREAD_FRACTION_OF_RANGE = 0.02

# Impact 系数（调节 sqrt impact 的强度）
IMPACT_COEFFICIENT = 1.0

# 默认 taker fee（OKX swap taker 5 bps）
DEFAULT_TAKER_FEE_BPS = 5.0


def estimate_slippage(
    feasibility_rows: list[dict[str, Any]],
    *,
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
    min_half_spread_bps: float = MIN_HALF_SPREAD_BPS,
    spread_fraction: float = SPREAD_FRACTION_OF_RANGE,
    impact_coefficient: float = IMPACT_COEFFICIENT,
) -> list[dict[str, Any]]:
    """估计每笔候选订单的滑点和执行成本。

    Args:
        feasibility_rows: 来自 fill_feasibility 的结果行。
        taker_fee_bps: Taker fee（bps）。
        min_half_spread_bps: 最小半 spread（bps）。
        spread_fraction: bar range 中 spread 占比。
        impact_coefficient: Impact 缩放系数。

    Returns:
        每条增加了 slippage 字段的结果列表。
    """
    taker_fee_bps = _require_nonnegative_finite("taker_fee_bps", taker_fee_bps)
    min_half_spread_bps = _require_nonnegative_finite(
        "min_half_spread_bps", min_half_spread_bps,
    )
    spread_fraction = _require_nonnegative_finite("spread_fraction", spread_fraction)
    impact_coefficient = _require_nonnegative_finite(
        "impact_coefficient", impact_coefficient,
    )
    results: list[dict[str, Any]] = []

    for row in feasibility_rows:
        bar_close = _finite_number(row.get("bar_close"))
        bar_range_bps = _finite_number(row.get("bar_range_bps"))
        volume_ratio = _finite_number(row.get("volume_ratio"))
        candidate_side = row.get("candidate_side", "buy")
        feasibility_category = row.get("feasibility_category", "")

        # 无市场数据或不可成交的情况
        if (bar_close is None or bar_close <= 0
                or bar_range_bps is None or bar_range_bps < 0
                or volume_ratio is None or volume_ratio < 0
                or feasibility_category == "insufficient_market_data"):
            results.append(_make_slippage_row(
                row,
                arrival_mid_px=bar_close,
                estimated_fill_vwap_px=None,
                half_spread_bps=None,
                volume_impact_bps=None,
                estimated_slippage_bps=None,
                estimated_fee_bps=taker_fee_bps,
                estimated_total_execution_cost_bps=None,
                cost_vs_assumed_bps=None,
                cost_adjusted_edge_bps=None,
                slippage_model="v1_bar_proxy",
                slippage_data_quality="no_data",
            ))
            continue

        # ---- 1. Half-spread 估计 ----
        half_spread = max(min_half_spread_bps, bar_range_bps * spread_fraction)

        # ---- 2. Volume impact 估计 (square root law) ----
        if volume_ratio is not None and volume_ratio > 0:
            vol_impact = bar_range_bps * math.sqrt(volume_ratio) * impact_coefficient
        else:
            vol_impact = 0.0

        # ---- 3. 总滑点 ----
        slippage_bps = half_spread + vol_impact

        # ---- 4. 预估成交 VWAP ----
        arrival_px = bar_close
        if candidate_side == "buy":
            fill_vwap = arrival_px * (1 + slippage_bps / 10000)
        elif candidate_side == "sell":
            fill_vwap = arrival_px * (1 - slippage_bps / 10000)
        else:
            raise ValueError(f"unsupported candidate_side: {candidate_side!r}")

        # ---- 5. 总执行成本 ----
        total_cost_bps = slippage_bps + taker_fee_bps

        # ---- 6. 与 replay 假设成本的比较 ----
        assumed_cost = _finite_or_default(row, "cost_bps", default=0.0)
        cost_vs_assumed = total_cost_bps - assumed_cost if assumed_cost > 0 else None

        # ---- 7. 成本调整后的 edge ----
        expected_net_edge = _finite_or_default(
            row,
            "expected_net_edge_bps",
            default=0.0,
        )
        if assumed_cost > 0:
            # cost_adjusted = net_edge + assumed_cost - realistic_cost
            cost_adjusted_edge = expected_net_edge + assumed_cost - total_cost_bps
        else:
            cost_adjusted_edge = expected_net_edge - total_cost_bps

        results.append(_make_slippage_row(
            row,
            arrival_mid_px=arrival_px,
            estimated_fill_vwap_px=round(fill_vwap, 4),
            half_spread_bps=round(half_spread, 3),
            volume_impact_bps=round(vol_impact, 3),
            estimated_slippage_bps=round(slippage_bps, 3),
            estimated_fee_bps=taker_fee_bps,
            estimated_total_execution_cost_bps=round(total_cost_bps, 3),
            cost_vs_assumed_bps=round(cost_vs_assumed, 3) if cost_vs_assumed is not None else None,
            cost_adjusted_edge_bps=round(cost_adjusted_edge, 3),
            slippage_model="v1_bar_proxy",
            slippage_data_quality="bar_only",
        ))

    # 汇总日志
    valid = [r for r in results if r.get("estimated_slippage_bps") is not None]
    if valid:
        avg_slip = sum(r["estimated_slippage_bps"] for r in valid) / len(valid)
        avg_cost = sum(r["estimated_total_execution_cost_bps"] for r in valid) / len(valid)
        log.info("Slippage estimate: %d candidates, avg slippage=%.2f bps, avg total cost=%.2f bps",
                 len(valid), avg_slip, avg_cost)
    else:
        log.warning("No valid slippage estimates produced")

    return results


def _finite_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _require_nonnegative_finite(name: str, value: Any) -> float:
    parsed = _finite_number(value)
    if parsed is None or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def _finite_or_default(row: dict[str, Any], key: str, *, default: float) -> float:
    raw_value = row.get(key)
    if raw_value is None or raw_value == "":
        return default
    parsed = _finite_number(raw_value)
    if parsed is None:
        raise ValueError(f"{key} must be a finite number")
    return parsed


def _make_slippage_row(
    source_row: dict[str, Any],
    *,
    arrival_mid_px: float | None,
    estimated_fill_vwap_px: float | None,
    half_spread_bps: float | None,
    volume_impact_bps: float | None,
    estimated_slippage_bps: float | None,
    estimated_fee_bps: float,
    estimated_total_execution_cost_bps: float | None,
    cost_vs_assumed_bps: float | None,
    cost_adjusted_edge_bps: float | None,
    slippage_model: str,
    slippage_data_quality: str,
) -> dict[str, Any]:
    """构造 slippage 输出行。"""
    return {
        **source_row,
        "arrival_mid_px": arrival_mid_px,
        "estimated_fill_vwap_px": estimated_fill_vwap_px,
        "half_spread_bps": half_spread_bps,
        "volume_impact_bps": volume_impact_bps,
        "estimated_slippage_bps": estimated_slippage_bps,
        "estimated_fee_bps": estimated_fee_bps,
        "estimated_total_execution_cost_bps": estimated_total_execution_cost_bps,
        "cost_vs_assumed_bps": cost_vs_assumed_bps,
        "cost_adjusted_edge_bps": cost_adjusted_edge_bps,
        "slippage_model": slippage_model,
        "slippage_data_quality": slippage_data_quality,
    }
