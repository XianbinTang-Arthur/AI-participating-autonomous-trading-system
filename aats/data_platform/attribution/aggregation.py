"""Attribution 聚合模块.

将逐条归因结果汇总为：
  - attribution_summary (按 category × reason 聚合)
  - top_failure_modes (最常见失败原因排名)
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from aats.data_platform.attribution.taxonomy import (
    ATTRIBUTION_NOT_APPLICABLE,
    ATTRIBUTION_SUCCESS,
)

log = logging.getLogger(__name__)


def build_attribution_summary(
    classified_rows: list[dict[str, Any]],
    *,
    family: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    """按 category + reason 聚合归因结果.

    返回列表，每行:
      family, timeframe, category, reason, count, ratio
    """
    total = len(classified_rows)
    if total == 0:
        return []

    counter: Counter[tuple[str, str]] = Counter()
    for row in classified_rows:
        cat = row.get("final_attribution_category", "unknown")
        reason = row.get("final_attribution_reason", "unclassified")
        counter[(cat, reason)] += 1

    summary: list[dict[str, Any]] = []
    for (cat, reason), count in counter.most_common():
        summary.append({
            "family": family,
            "timeframe": timeframe,
            "category": cat,
            "reason": reason,
            "count": count,
            "ratio": round(count / total, 4),
        })

    log.info("Attribution summary: %d categories for %s/%s", len(summary), family, timeframe)
    return summary


def build_top_failure_modes(
    classified_rows: list[dict[str, Any]],
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """统计最常见的失败原因（排除 not_applicable 和 success）.

    返回:
      {
        "total_events": int,
        "total_failures": int,
        "top_categories": [...],
        "top_reasons": [...],
      }
    """
    # 只看有意义的行（replay 想开 or aligned）
    failure_rows = [
        r for r in classified_rows
        if r.get("final_attribution_category") not in (
            ATTRIBUTION_NOT_APPLICABLE,
            ATTRIBUTION_SUCCESS,
        )
    ]

    total = len(classified_rows)
    total_failures = len(failure_rows)

    cat_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    for row in failure_rows:
        cat = row.get("final_attribution_category", "unknown")
        reason = row.get("final_attribution_reason", "unclassified")
        cat_counter[cat] += 1
        reason_counter[reason] += 1

    top_categories = [
        {"category": cat, "count": count, "ratio": round(count / max(total, 1), 4)}
        for cat, count in cat_counter.most_common(top_n)
    ]

    top_reasons = [
        {"reason": reason, "count": count, "ratio": round(count / max(total, 1), 4)}
        for reason, count in reason_counter.most_common(top_n)
    ]

    result = {
        "total_events": total,
        "total_failures": total_failures,
        "failure_ratio": round(total_failures / max(total, 1), 4),
        "total_success": sum(
            1 for r in classified_rows
            if r.get("final_attribution_category") == ATTRIBUTION_SUCCESS
        ),
        "total_not_applicable": sum(
            1 for r in classified_rows
            if r.get("final_attribution_category") == ATTRIBUTION_NOT_APPLICABLE
        ),
        "top_categories": top_categories,
        "top_reasons": top_reasons,
    }
    log.info("Top failure modes: %d failures / %d total", total_failures, total)
    return result


def build_layer_analysis(
    classified_rows: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """按层聚合 pass / fail 统计.

    返回:
      {
        "strategy": {"passed": N, "failed": M},
        "permission": ...,
        "allocator": ...,
        "budget": ...,
        "risk": ...,
        "execution": ...,
        "order": ...,
        "fill": ...,
      }
    """
    layers = [
        ("strategy", "strategy_reason"),
        ("permission", "permission_reason"),
        ("allocator", "allocator_reason"),
        ("budget", "budget_reason"),
        ("risk", "risk_reason"),
        ("execution", "execution_reason"),
        ("order", "order_status"),
        ("fill", "fill_status"),
    ]

    analysis: dict[str, dict[str, int]] = {}
    for layer_name, field in layers:
        passed = 0
        failed = 0
        not_reached = 0
        for row in classified_rows:
            val = row.get(field)
            if val == "passed" or val == "filled":
                passed += 1
            elif val is None:
                not_reached += 1
            else:
                failed += 1
        analysis[layer_name] = {
            "passed": passed,
            "failed": failed,
            "not_reached": not_reached,
        }

    return analysis
