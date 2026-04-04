"""分层 Attribution 引擎 — 瀑布式归因.

对每个 alignment row 按固定层序由上往下归因，
停在第一层失败处，不跨层乱归因。

层序：
  1. Strategy: replay 是否想开 → live strategy 是否也想开
  2. Permission: automatic_enabled?
  3. Allocator: allocation 存在且 approved?
  4. Budget: budget 足够?
  5. Risk: reconciliation 不阻止?
  6. Execution: bundle 状态?
  7. Order: order 创建?
  8. Fill: fill 出现?
"""

from __future__ import annotations

import logging
from typing import Any

from aats.data_platform.attribution.taxonomy import (
    ALIGNMENT_STATUS_ALIGNED,
    ALIGNMENT_STATUS_LIVE_ONLY,
    ALIGNMENT_STATUS_REPLAY_ONLY,
    ATTRIBUTION_NOT_APPLICABLE,
    ATTRIBUTION_SUCCESS,
)

log = logging.getLogger(__name__)


def classify_all(
    alignment_rows: list[dict[str, Any]],
    *,
    allocations: dict[str, dict[str, Any]] | None = None,
    budgets: dict[str, dict[str, Any]] | None = None,
    bundles: dict[str, list[dict[str, Any]]] | None = None,
    orders: dict[str, list[dict[str, Any]]] | None = None,
    fills: dict[str, list[dict[str, Any]]] | None = None,
    recon_snapshots: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """对所有 alignment rows 做瀑布式归因.

    在每行上附加：
      final_attribution_category
      final_attribution_reason
      strategy_reason
      permission_reason
      allocator_reason
      budget_reason
      risk_reason
      execution_reason
      order_status
      fill_status
    """
    allocations = allocations or {}
    budgets = budgets or {}
    bundles = bundles or {}
    orders = orders or {}
    fills = fills or {}
    recon_snapshots = recon_snapshots or []

    # 取时间窗口内最新的 reconciliation snapshot
    latest_recon = recon_snapshots[0] if recon_snapshots else None

    results: list[dict[str, Any]] = []
    for row in alignment_rows:
        classified = _classify_single(
            row,
            allocations=allocations,
            budgets=budgets,
            bundles=bundles,
            orders=orders,
            fills=fills,
            latest_recon=latest_recon,
        )
        results.append(classified)

    _log_summary(results)
    return results


def _classify_single(
    row: dict[str, Any],
    *,
    allocations: dict[str, dict[str, Any]],
    budgets: dict[str, dict[str, Any]],
    bundles: dict[str, list[dict[str, Any]]],
    orders: dict[str, list[dict[str, Any]]],
    fills: dict[str, list[dict[str, Any]]],
    latest_recon: dict[str, Any] | None,
) -> dict[str, Any]:
    """对单行做瀑布式归因。"""
    result = dict(row)

    # 初始化归因字段
    result["strategy_reason"] = None
    result["permission_reason"] = None
    result["allocator_reason"] = None
    result["budget_reason"] = None
    result["risk_reason"] = None
    result["execution_reason"] = None
    result["order_status"] = None
    result["fill_status"] = None

    status = row["alignment_status"]

    # ---- Live-only: replay 没有对应 bar ----
    if status == ALIGNMENT_STATUS_LIVE_ONLY:
        result["final_attribution_category"] = ATTRIBUTION_NOT_APPLICABLE
        result["final_attribution_reason"] = "live_only_no_replay_bar"
        return result

    # ---- Replay 没想开：不需要归因 ----
    replay_selectable = row.get("replay_selectable", False)
    replay_opening = row.get("replay_opening", False)
    if not replay_selectable and not replay_opening:
        result["final_attribution_category"] = ATTRIBUTION_NOT_APPLICABLE
        result["final_attribution_reason"] = "replay_not_selectable"
        # 记录 replay 的 blocking reason
        blocking = row.get("replay_blocking_reasons", "")
        if blocking:
            reasons = blocking.split("|") if isinstance(blocking, str) else blocking
            result["strategy_reason"] = reasons[0] if reasons else None
        return result

    # ---- Replay 想开但被自身 block ----
    if replay_selectable and not replay_opening:
        blocking = row.get("replay_blocking_reasons", "")
        reasons = blocking.split("|") if isinstance(blocking, str) else (blocking or [])
        result["final_attribution_category"] = "strategy_blocked"
        result["final_attribution_reason"] = reasons[0] if reasons else "score_not_stable"
        result["strategy_reason"] = reasons[0] if reasons else "score_not_stable"
        return result

    # ==== 以下：replay_opening == True → replay 想开且可执行 ====

    # ---- Layer 1: Strategy — live 有 intent 吗？ ----
    if status == ALIGNMENT_STATUS_REPLAY_ONLY:
        # 没有 live intent 与此 bar 对齐
        result["final_attribution_category"] = "strategy_blocked"
        result["final_attribution_reason"] = "no_intent_in_window"
        result["strategy_reason"] = "no_intent_in_window"
        return result

    # 已对齐（aligned）
    live_route = row.get("live_route_action", "")
    if live_route == "hold" or live_route == "":
        result["final_attribution_category"] = "strategy_blocked"
        result["final_attribution_reason"] = "intent_route_action_hold"
        result["strategy_reason"] = "intent_route_action_hold"
        return result

    result["strategy_reason"] = "passed"

    # ---- Layer 2: Permission — automatic_enabled? ----
    auto_enabled = row.get("live_automatic_enabled")
    if auto_enabled is False:
        result["final_attribution_category"] = "permission_disabled"
        result["final_attribution_reason"] = "automatic_enabled_false"
        result["permission_reason"] = "automatic_enabled_false"
        return result

    result["permission_reason"] = "passed"

    # ---- Layer 3: Allocator — allocation 存在且 approved? ----
    alloc_id = row.get("live_allocation_id", "")
    alloc = allocations.get(alloc_id)
    if not alloc_id or not alloc:
        result["final_attribution_category"] = "allocator_rejected"
        result["final_attribution_reason"] = "no_allocation_found"
        result["allocator_reason"] = "no_allocation_found"
        return result

    approved_notional = float(alloc.get("portfolio_approved_notional") or 0)
    if approved_notional <= 0:
        result["final_attribution_category"] = "allocator_rejected"
        result["final_attribution_reason"] = "approved_notional_zero"
        result["allocator_reason"] = "approved_notional_zero"
        return result

    alloc_route = alloc.get("route_action", "")
    if alloc_route == "hold":
        result["final_attribution_category"] = "allocator_rejected"
        result["final_attribution_reason"] = "route_action_hold"
        result["allocator_reason"] = "route_action_hold"
        return result

    result["allocator_reason"] = "passed"

    # ---- Layer 4: Budget — budget 足够? ----
    budget = budgets.get(alloc_id)
    if budget:
        bm = float(budget.get("budget_multiplier") or 0)
        if bm <= 0:
            result["final_attribution_category"] = "budget_rejected"
            result["final_attribution_reason"] = "budget_multiplier_zero"
            result["budget_reason"] = "budget_multiplier_zero"
            return result

        budget_cut = float(budget.get("portfolio_budget_cut_notional") or 0)
        budget_approved = float(budget.get("approved_notional") or 0)
        if budget_approved <= 0 and budget_cut > 0:
            result["final_attribution_category"] = "budget_rejected"
            result["final_attribution_reason"] = "portfolio_budget_cut_full"
            result["budget_reason"] = "portfolio_budget_cut_full"
            return result

        if budget.get("clamped") and budget_approved <= 0:
            result["final_attribution_category"] = "budget_rejected"
            result["final_attribution_reason"] = "budget_clamped_to_zero"
            result["budget_reason"] = "budget_clamped_to_zero"
            return result

    result["budget_reason"] = "passed"

    # ---- Layer 5: Risk — reconciliation 不阻止? ----
    if latest_recon:
        if latest_recon.get("halt_required"):
            result["final_attribution_category"] = "risk_rejected"
            result["final_attribution_reason"] = "halt_required"
            result["risk_reason"] = "halt_required"
            return result

        if latest_recon.get("only_reduce_required"):
            result["final_attribution_category"] = "risk_rejected"
            result["final_attribution_reason"] = "only_reduce_required"
            result["risk_reason"] = "only_reduce_required"
            return result

        if latest_recon.get("review_required"):
            result["final_attribution_category"] = "risk_rejected"
            result["final_attribution_reason"] = "review_required"
            result["risk_reason"] = "review_required"
            return result

        if latest_recon.get("safe_to_trade") is False:
            result["final_attribution_category"] = "risk_rejected"
            result["final_attribution_reason"] = "safe_to_trade_false"
            result["risk_reason"] = "safe_to_trade_false"
            return result

    result["risk_reason"] = "passed"

    # ---- Layer 6: Execution — bundle 状态? ----
    decision_id = row.get("live_decision_id", "")
    decision_bundles = bundles.get(decision_id, [])
    if not decision_bundles:
        result["final_attribution_category"] = "execution_blocked"
        result["final_attribution_reason"] = "bundle_not_found"
        result["execution_reason"] = "bundle_not_found"
        return result

    latest_bundle = decision_bundles[-1]
    bundle_status = latest_bundle.get("status", "")
    if bundle_status in ("rejected", "cancelled"):
        result["final_attribution_category"] = "execution_blocked"
        result["final_attribution_reason"] = f"bundle_status_{bundle_status}"
        result["execution_reason"] = f"bundle_status_{bundle_status}"
        return result

    net_exposure = float(latest_bundle.get("net_approved_exposure") or 0)
    if net_exposure <= 0 and bundle_status not in ("executed", "submitted"):
        result["final_attribution_category"] = "execution_blocked"
        result["final_attribution_reason"] = "net_approved_exposure_zero"
        result["execution_reason"] = "net_approved_exposure_zero"
        return result

    result["execution_reason"] = "passed"

    # ---- Layer 7: Order — order 创建? ----
    decision_orders = orders.get(decision_id, [])
    if not decision_orders:
        result["final_attribution_category"] = "order_not_created"
        result["final_attribution_reason"] = "no_order_found"
        result["order_status"] = "not_found"
        return result

    latest_order = decision_orders[-1]
    order_state = latest_order.get("state", "")
    result["order_status"] = order_state

    if order_state in ("rejected", "cancelled"):
        result["final_attribution_category"] = "order_not_created"
        result["final_attribution_reason"] = f"order_state_{order_state}"
        return result

    # ---- Layer 8: Fill — fill 出现? ----
    order_id = str(latest_order.get("order_id", ""))
    order_fills = fills.get(order_id, [])
    if not order_fills:
        result["final_attribution_category"] = "fill_not_observed"
        result["final_attribution_reason"] = "no_fill_found"
        result["fill_status"] = "not_found"
        return result

    total_fill_qty = sum(float(f.get("fill_qty") or 0) for f in order_fills)
    requested_qty = float(latest_order.get("requested_qty") or 0)
    if requested_qty > 0 and total_fill_qty < requested_qty * 0.5:
        result["final_attribution_category"] = "fill_not_observed"
        result["final_attribution_reason"] = "partial_fill_only"
        result["fill_status"] = "partial"
        return result

    result["fill_status"] = "filled"

    # ==== 全部通过 → live 也成功交易 ====
    result["final_attribution_category"] = ATTRIBUTION_SUCCESS
    result["final_attribution_reason"] = "all_layers_passed"
    return result


def _log_summary(results: list[dict[str, Any]]) -> None:
    """日志摘要。"""
    from collections import Counter

    cats = Counter(r.get("final_attribution_category", "?") for r in results)
    log.info("Attribution summary: %s", dict(cats.most_common()))
