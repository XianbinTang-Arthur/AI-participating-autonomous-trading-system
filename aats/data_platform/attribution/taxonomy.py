"""Attribution Taxonomy — 统一归因分类.

定义所有 attribution category 和 reason code，确保口径一致。
归因顺序为严格瀑布（waterfall）：遇到第一层失败即停止归因。

层序：
  1. strategy_blocked
  2. permission_disabled
  3. allocator_rejected
  4. budget_rejected
  5. risk_rejected
  6. execution_blocked
  7. order_not_created
  8. fill_not_observed
  9. reconciliation_restricted
  10. unknown
"""

from __future__ import annotations

# =========================================================================
# 归因类别（严格瀑布顺序）
# =========================================================================

ATTRIBUTION_CATEGORIES: list[str] = [
    "strategy_blocked",
    "permission_disabled",
    "allocator_rejected",
    "budget_rejected",
    "risk_rejected",
    "execution_blocked",
    "order_not_created",
    "fill_not_observed",
    "reconciliation_restricted",
    "unknown",
]

# =========================================================================
# 每类标准 Reason Code
# =========================================================================

REASON_CODES: dict[str, list[str]] = {
    "strategy_blocked": [
        "score_below_entry_threshold",
        "score_not_stable",
        "net_edge_below_safe_minimum",
        "cost_exceeds_max_acceptable",
        "rebalance_cooldown",
        "no_intent_in_window",
        "intent_route_action_hold_current",
        "intent_route_action_advisory_only",
        "intent_route_action_protective_fallback",
        "intent_route_action_missing",
    ],
    "permission_disabled": [
        "automatic_enabled_false",
        "automatic_enabled_missing",
        "family_disabled",
    ],
    "allocator_rejected": [
        "no_allocation_found",
        "approved_notional_zero",
        "route_action_hold_current",
        "route_action_advisory_only",
        "route_action_protective_fallback",
        "route_action_missing",
    ],
    "budget_rejected": [
        "budget_snapshot_missing",
        "budget_multiplier_zero",
        "portfolio_budget_cut_full",
        "budget_clamped_to_zero",
    ],
    "risk_rejected": [
        "only_reduce_required",
        "halt_required",
        "review_required",
        "bundle_recovery_required",
        "reconciliation_snapshot_missing",
        "resume_not_eligible",
        "safe_to_trade_false",
    ],
    "execution_blocked": [
        "bundle_status_blocked",
        "bundle_status_review_required",
        "bundle_status_unknown",
        "bundle_not_found",
        "net_approved_exposure_zero",
    ],
    "order_not_created": [
        "no_order_found",
        "order_state_canceled",
        "order_state_cancelled",
        "order_state_rejected",
        "order_state_failed",
        "order_state_blocked",
        "order_state_dry_run",
        "order_state_expired",
    ],
    "fill_not_observed": [
        "no_fill_found",
        "partial_fill_only",
    ],
    "reconciliation_restricted": [
        "reconciliation_halted",
        "reconciliation_only_reduce",
    ],
    "unknown": [
        "unclassified",
    ],
}

# 所有 reason code 的平坦集合（用于验证）
ALL_REASON_CODES: set[str] = set()
for _codes in REASON_CODES.values():
    ALL_REASON_CODES.update(_codes)


# =========================================================================
# 对齐状态
# =========================================================================

ALIGNMENT_STATUS_ALIGNED = "aligned"
ALIGNMENT_STATUS_REPLAY_ONLY = "replay_only"
ALIGNMENT_STATUS_LIVE_ONLY = "live_only"
ALIGNMENT_STATUS_UNATTRIBUTABLE = "unattributable"

# =========================================================================
# 特殊标记
# =========================================================================

# 当 replay 本身就没有 opening（action != "open" 且 selectable == False）
# 不需要归因，标记为 not_applicable
ATTRIBUTION_NOT_APPLICABLE = "not_applicable"

# 当 live 也成功交易，无失败
ATTRIBUTION_SUCCESS = "live_traded"


# =========================================================================
# Timeframe → 秒数
# =========================================================================

TF_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1H": 3600,
    "1h": 3600,
    "4H": 14400,
    "4h": 14400,
    "1D": 86400,
    "1d": 86400,
}
