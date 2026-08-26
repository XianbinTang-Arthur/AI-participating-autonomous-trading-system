"""Live Facts 字段契约.

定义 RDP 需要从 production DB 只读访问的 7 张表的:
  - 最小必需列
  - 主键 / 关联键
  - 时间字段
  - family / symbol / timeframe 字段
  - nullable 规则
  - 与 attribution / execution_realism 模块的对应关系
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 表级契约 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class TableContract:
    """单张表的字段契约."""

    table_name: str
    primary_key: str
    time_column: str
    symbol_column: str
    minimum_required_columns: tuple[str, ...]
    nullable_columns: tuple[str, ...] = ()
    family_column: str | None = None
    used_by_phases: tuple[str, ...] = ()
    notes: str = ""


# ── 7 张关键表的契约定义 ────────────────────────────────────────────

STRATEGY_SLEEVE_INTENTS = TableContract(
    table_name="strategy_sleeve_intents",
    primary_key="sleeve_intent_id",
    time_column="created_at",
    symbol_column="symbol",
    minimum_required_columns=(
        "sleeve_intent_id", "decision_id", "strategy_sleeve_id",
        "allocation_id", "state", "route_action", "inventory_policy",
        "product_type", "margin_mode", "symbol",
        "budget_multiplier", "automatic_enabled",
        "timeframe", "signal_bar_start", "signal_bar_end", "market_data_asof",
        "parameter_set_id", "runtime_generation", "code_version",
        "market_snapshot_ref", "feature_snapshot_ref",
        "payload", "created_at",
    ),
    nullable_columns=(
        "payload",
        "timeframe", "signal_bar_start", "signal_bar_end", "market_data_asof",
        "parameter_set_id", "runtime_generation", "code_version",
        "market_snapshot_ref", "feature_snapshot_ref",
    ),
    used_by_phases=("phase3",),
    notes="Phase 3 attribution: 策略意图分析 — 谁想做什么",
)

PORTFOLIO_ALLOCATION_DECISIONS = TableContract(
    table_name="portfolio_allocation_decisions",
    primary_key="allocation_id",
    time_column="created_at",
    symbol_column="symbol",
    family_column="primary_family",
    minimum_required_columns=(
        "allocation_id", "decision_id", "symbol",
        "allocator_version", "route_action", "primary_family",
        "portfolio_requested_notional", "portfolio_approved_notional",
        "portfolio_budget_cut_notional",
        "product_type", "created_at",
    ),
    nullable_columns=(
        "expected_edge_bps", "expected_cost_bps",
        "primary_strategy_sleeve_id", "payload",
    ),
    used_by_phases=("phase3",),
    notes="Phase 3 attribution: 组合分配决策 — 多少钱给了谁",
)

ALLOCATOR_BUDGET_SNAPSHOTS = TableContract(
    table_name="allocator_budget_snapshots",
    primary_key="budget_snapshot_id",
    time_column="created_at",
    symbol_column="symbol",
    family_column="family",
    minimum_required_columns=(
        "budget_snapshot_id", "allocation_id",
        "family", "symbol",
        "requested_notional", "approved_notional",
        "budget_multiplier", "priority_rank", "clamped",
        "product_type", "created_at",
    ),
    nullable_columns=(
        "quote_budget_limit", "margin_budget_limit",
        "notional_cap", "max_symbol_notional",
        "hedge_priority_class", "payload",
    ),
    used_by_phases=("phase3",),
    notes="Phase 3 attribution: 预算快照 — 预算分配细节",
)

RECONCILIATION_STATE_SNAPSHOTS = TableContract(
    table_name="reconciliation_state_snapshots",
    primary_key="snapshot_id",
    time_column="created_at",
    symbol_column="primary_symbol",
    minimum_required_columns=(
        "snapshot_id", "reconciliation_id", "recovery_state",
        "resume_eligible", "safe_to_trade", "review_required",
        "halt_required",
        "product_type", "primary_symbol", "created_at",
    ),
    nullable_columns=(
        "derived_from_generation_id", "exchange_ack_watermark_id",
        "details_json", "resume_blocked_reasons_json",
    ),
    used_by_phases=("phase3",),
    notes="Phase 3 attribution: 对账状态 — 系统是否健康到可以交易",
)

STRATEGY_EXECUTION_BUNDLES = TableContract(
    table_name="strategy_execution_bundles",
    primary_key="bundle_id",
    time_column="created_at",
    symbol_column="selected_symbol",
    family_column="family",
    minimum_required_columns=(
        "bundle_id", "decision_id", "family",
        "route_action", "status", "selected_symbol",
        "gross_requested_exposure", "net_approved_exposure",
        "product_type", "created_at",
    ),
    nullable_columns=(
        "expected_cost_bps", "expected_edge_bps",
        "portfolio_risk_budget_state", "payload",
    ),
    used_by_phases=("phase3", "phase4"),
    notes="Phase 3/4: 执行包 — 决策到执行的桥梁",
)

EXECUTION_ORDERS = TableContract(
    table_name="execution_orders",
    primary_key="order_id",
    time_column="created_at",
    symbol_column="symbol",
    family_column="strategy_family",
    minimum_required_columns=(
        "order_id", "intent_id", "decision_id",
        "symbol", "side", "order_type",
        "requested_qty", "state",
        "product_type", "created_at",
    ),
    nullable_columns=(
        "limit_price", "venue_order_id",
        "strategy_family", "strategy_sleeve_id",
        "raw_payload",
    ),
    used_by_phases=("phase3", "phase4"),
    notes="Phase 3/4: 订单记录 — 下单了什么",
)

EXECUTION_FILLS = TableContract(
    table_name="execution_fills",
    primary_key="fill_id",
    time_column="ingestion_ts",
    symbol_column="symbol",
    family_column="strategy_family",
    minimum_required_columns=(
        "fill_id", "order_id", "symbol", "side",
        "fill_qty", "fill_price", "fee_amount",
        "ingestion_ts",
    ),
    nullable_columns=(
        "venue_fill_id", "fee_currency",
        "liquidity_role", "strategy_family",
        "strategy_sleeve_id", "raw_payload",
    ),
    used_by_phases=("phase4",),
    notes="Phase 4 execution realism: 成交记录 — 实际成交了什么",
)

# ── 全部契约汇总 ───────────────────────────────────────────────────

ALL_TABLE_CONTRACTS: dict[str, TableContract] = {
    c.table_name: c
    for c in (
        STRATEGY_SLEEVE_INTENTS,
        PORTFOLIO_ALLOCATION_DECISIONS,
        ALLOCATOR_BUDGET_SNAPSHOTS,
        RECONCILIATION_STATE_SNAPSHOTS,
        STRATEGY_EXECUTION_BUNDLES,
        EXECUTION_ORDERS,
        EXECUTION_FILLS,
    )
}

LIVE_TABLE_NAMES: list[str] = list(ALL_TABLE_CONTRACTS.keys())

# ── 用于快速查找的辅助结构 ──────────────────────────────────────────


def get_contract(table_name: str) -> TableContract:
    """获取指定表的契约，不存在则 raise."""
    if table_name not in ALL_TABLE_CONTRACTS:
        raise ValueError(
            f"未知表: {table_name}，"
            f"已知表: {sorted(ALL_TABLE_CONTRACTS.keys())}"
        )
    return ALL_TABLE_CONTRACTS[table_name]


def tables_for_phase(phase: str) -> list[TableContract]:
    """获取指定 Phase 需要的所有表."""
    return [c for c in ALL_TABLE_CONTRACTS.values() if phase in c.used_by_phases]
