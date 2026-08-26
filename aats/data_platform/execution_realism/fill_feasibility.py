"""Fill Feasibility 引擎 (Phase 4-B).

判断 hypothetical order 在当时市场条件下是否可成交。

V1 实现：
  - 使用 bar volume 作为流动性代理
  - candidate_qty / bar_volume 比率判断可成交性
  - 基于 volume ratio 估算吃单层数（proxy）

成交性类别:
  - fully_fillable:           volume_ratio < 1%，轻松成交
  - partially_fillable:       1% <= volume_ratio < 10%，有一定市场冲击
  - not_fillable:             volume_ratio >= 10%，市场冲击过大
  - insufficient_market_data: 无 volume 数据
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)

# =========================================================================
# 可成交性阈值（V1 默认值，可通过 config 覆盖）
# =========================================================================

# volume_ratio = candidate_qty / bar_volume
FULLY_FILLABLE_RATIO = 0.01      # < 1% of bar volume
PARTIALLY_FILLABLE_RATIO = 0.10  # < 10% of bar volume

# 假设最多可消耗 bar volume 的 10% 不造成过度冲击
MAX_FILLABLE_VOLUME_FRACTION = 0.10

# Feasibility category 常量
CATEGORY_FULLY_FILLABLE = "fully_fillable"
CATEGORY_PARTIALLY_FILLABLE = "partially_fillable"
CATEGORY_NOT_FILLABLE = "not_fillable"
CATEGORY_INSUFFICIENT_DATA = "insufficient_market_data"


def evaluate_fill_feasibility(
    aligned_rows: list[dict[str, Any]],
    *,
    fully_fillable_ratio: float = FULLY_FILLABLE_RATIO,
    partially_fillable_ratio: float = PARTIALLY_FILLABLE_RATIO,
    max_fillable_fraction: float = MAX_FILLABLE_VOLUME_FRACTION,
) -> list[dict[str, Any]]:
    """评估每笔候选订单的可成交性。

    Args:
        aligned_rows: 来自 market_alignment 的对齐行。
        fully_fillable_ratio: 完全可成交的 volume ratio 阈值。
        partially_fillable_ratio: 部分可成交的 volume ratio 阈值。
        max_fillable_fraction: bar volume 中可消耗的最大比例。

    Returns:
        每条增加了 feasibility 字段的结果列表。
    """
    fully_fillable_ratio = _require_nonnegative_finite(
        "fully_fillable_ratio", fully_fillable_ratio,
    )
    partially_fillable_ratio = _require_nonnegative_finite(
        "partially_fillable_ratio", partially_fillable_ratio,
    )
    max_fillable_fraction = _require_nonnegative_finite(
        "max_fillable_fraction", max_fillable_fraction,
    )
    if partially_fillable_ratio <= fully_fillable_ratio:
        raise ValueError("partially_fillable_ratio must be greater than fully_fillable_ratio")
    if max_fillable_fraction > 1:
        raise ValueError("max_fillable_fraction must be <= 1")
    results: list[dict[str, Any]] = []

    for row in aligned_rows:
        candidate_qty = _finite_number(row.get("candidate_qty"))
        if candidate_qty is None or candidate_qty <= 0:
            raise ValueError("candidate_qty must be a finite positive number")
        bar_volume = _finite_number(row.get("bar_volume"))
        alignment_status = row.get("alignment_status", "")

        # 无市场数据
        if alignment_status != "matched" or bar_volume is None or bar_volume <= 0:
            results.append(_make_feasibility_row(
                row,
                volume_ratio=None,
                fillable_qty=0,
                fillable_ratio=0,
                levels_consumed=0,
                full_fill_possible=False,
                partial_fill_possible=False,
                feasibility_category=CATEGORY_INSUFFICIENT_DATA,
                book_depth_available_qty=0,
            ))
            continue

        bar_vol = bar_volume
        volume_ratio = candidate_qty / bar_vol if bar_vol > 0 else float("inf")

        # 分类
        if volume_ratio < fully_fillable_ratio:
            category = CATEGORY_FULLY_FILLABLE
            full_fill = True
            partial_fill = True
        elif volume_ratio < partially_fillable_ratio:
            category = CATEGORY_PARTIALLY_FILLABLE
            full_fill = False
            partial_fill = True
        else:
            category = CATEGORY_NOT_FILLABLE
            full_fill = False
            partial_fill = False

        # 可成交量估算
        max_fillable = bar_vol * max_fillable_fraction
        fillable_qty = min(candidate_qty, max_fillable)
        fillable_ratio = fillable_qty / candidate_qty if candidate_qty > 0 else 0

        # 吃单层数估算（proxy）
        # 假设 bar 内价格均匀分布，volume ratio 越大需要穿越越多层
        levels_consumed = _estimate_levels_consumed(volume_ratio)

        results.append(_make_feasibility_row(
            row,
            volume_ratio=round(volume_ratio, 6),
            fillable_qty=round(fillable_qty, 4),
            fillable_ratio=round(fillable_ratio, 4),
            levels_consumed=levels_consumed,
            full_fill_possible=full_fill,
            partial_fill_possible=partial_fill,
            feasibility_category=category,
            book_depth_available_qty=round(bar_vol, 4),
        ))

    # 汇总日志
    cats = {}
    for r in results:
        cat = r["feasibility_category"]
        cats[cat] = cats.get(cat, 0) + 1
    log.info("Fill feasibility: %s", cats)

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


def _estimate_levels_consumed(volume_ratio: float) -> int:
    """基于 volume ratio 估算穿越档数（proxy）。

    V1 简化模型：
      - < 0.1% → 1 档（吃盘口）
      - 0.1% ~ 1% → 2-3 档
      - 1% ~ 5% → 4-6 档
      - > 5% → 7+ 档

    这是粗略估计，后续用 orderbook depth 数据替代。
    """
    if volume_ratio < 0.001:
        return 1
    elif volume_ratio < 0.01:
        return max(2, round(2 + volume_ratio / 0.01 * 2))
    elif volume_ratio < 0.05:
        return max(4, round(4 + (volume_ratio - 0.01) / 0.04 * 3))
    else:
        return max(7, round(7 + math.log10(max(volume_ratio / 0.05, 1)) * 5))


def _make_feasibility_row(
    aligned_row: dict[str, Any],
    *,
    volume_ratio: float | None,
    fillable_qty: float,
    fillable_ratio: float,
    levels_consumed: int,
    full_fill_possible: bool,
    partial_fill_possible: bool,
    feasibility_category: str,
    book_depth_available_qty: float,
) -> dict[str, Any]:
    """构造 feasibility 输出行。"""
    # 用 ts + family + timeframe 作为 candidate_id
    candidate_id = (
        f"{aligned_row.get('candidate_ts', '')}|"
        f"{aligned_row.get('family', '')}|"
        f"{aligned_row.get('timeframe', '')}"
    )

    return {
        # 从 aligned_row 继承
        **aligned_row,
        # Feasibility 字段
        "candidate_id": candidate_id,
        "volume_ratio": volume_ratio,
        "book_depth_available_qty": book_depth_available_qty,
        "fillable_qty": fillable_qty,
        "fillable_ratio": fillable_ratio,
        "levels_consumed": levels_consumed,
        "full_fill_possible": full_fill_possible,
        "partial_fill_possible": partial_fill_possible,
        "feasibility_category": feasibility_category,
    }
