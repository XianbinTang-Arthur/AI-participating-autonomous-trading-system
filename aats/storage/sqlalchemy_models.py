from __future__ import annotations

from decimal import Decimal
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


DECIMAL_36_18 = Numeric(36, 18, asdecimal=True)


class SchemaMigrationModel(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(256), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class EventEnvelopeModel(Base):
    __tablename__ = "event_store"
    __table_args__ = (
        Index("ix_event_store_topic_symbol_seq", "topic", "symbol", "sequence_id"),
        Index("ix_event_store_topic_scope_seq", "topic", "product_type", "margin_mode", "sequence_id"),
    )

    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source_component: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    timeframe: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class EventEnvelopeArchiveModel(Base):
    __tablename__ = "event_store_archive"
    __table_args__ = (
        Index("ix_event_store_archive_topic_symbol_seq", "topic", "symbol", "source_sequence_id"),
        Index("ix_event_store_archive_topic_scope_seq", "topic", "product_type", "margin_mode", "source_sequence_id"),
        Index("ix_event_store_archive_timestamp", "event_timestamp"),
    )

    archive_sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_sequence_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source_component: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    timeframe: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ReplayProjectionOffsetModel(Base):
    __tablename__ = "projection_replay_offsets"
    __table_args__ = (
        Index("ix_projection_replay_offsets_scope_updated", "product_type", "margin_mode", "updated_at"),
        Index("ix_projection_replay_offsets_projection", "projection_key"),
        Index(
            "ux_projection_replay_offsets_projection_scope",
            "projection_key",
            "product_type",
            "margin_mode",
            "allowed_symbols_hash",
            unique=True,
        ),
    )

    offset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    projection_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    allowed_symbols_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    allowed_symbols_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    last_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    baseline_generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    exchange_ack_watermark_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class PortfolioSnapshotModel(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("ix_portfolio_snapshots_scope_seq", "product_type", "margin_mode", "sequence_id"),
    )

    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_equity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    primary_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategySleeveModel(Base):
    __tablename__ = "strategy_sleeves"
    __table_args__ = (
        Index("ix_strategy_sleeves_family_status", "family", "status"),
        Index("ix_strategy_sleeves_product_margin", "product_scope", "margin_scope"),
    )

    sleeve_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_scope: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_scope: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Legacy column name retained for compatibility. The persisted meaning is now
    # "currently eligible to auto-enter the execution chain", not merely "config switch on".
    automatic_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    inventory_policy: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategySleeveIntentModel(Base):
    __tablename__ = "strategy_sleeve_intents"
    __table_args__ = (
        Index("ix_strategy_sleeve_intents_scope_created", "product_type", "margin_mode", "created_at"),
        Index("ix_strategy_sleeve_intents_sleeve_created", "strategy_sleeve_id", "created_at"),
        Index("ix_strategy_sleeve_intents_decision_created", "decision_id", "created_at"),
        Index("ix_strategy_sleeve_intents_symbol_created", "symbol", "created_at"),
    )

    sleeve_intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    strategy_sleeve_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    inventory_policy: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    route_action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Legacy column name retained for compatibility. The persisted meaning is now
    # "currently eligible to auto-enter the execution chain", aligned with approved_for_execution.
    automatic_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    budget_multiplier: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("1"))
    allocator_weight: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class PortfolioAllocationDecisionModel(Base):
    __tablename__ = "portfolio_allocation_decisions"
    __table_args__ = (
        Index("ix_portfolio_allocation_decisions_scope_created", "product_type", "margin_mode", "created_at"),
        Index("ix_portfolio_allocation_decisions_decision_created", "decision_id", "created_at"),
        Index("ix_portfolio_allocation_decisions_symbol_created", "symbol", "created_at"),
        Index("ix_portfolio_allocation_decisions_primary_family_created", "primary_family", "created_at"),
    )

    allocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    allocator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Legacy column name retained for compatibility. The persisted meaning is now
    # "currently eligible to auto-enter the execution chain", aligned with approved_for_execution.
    automatic_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    route_action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    primary_family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    primary_strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    portfolio_requested_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    portfolio_approved_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    portfolio_budget_cut_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    expected_edge_bps: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    expected_cost_bps: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategyExecutionBundleModel(Base):
    __tablename__ = "strategy_execution_bundles"
    __table_args__ = (
        Index("ix_strategy_execution_bundles_scope_created", "product_type", "margin_mode", "created_at"),
        Index("ix_strategy_execution_bundles_decision_created", "decision_id", "created_at"),
        Index("ix_strategy_execution_bundles_allocation_created", "allocation_id", "created_at"),
        Index("ix_strategy_execution_bundles_symbol_created", "selected_symbol", "created_at"),
        Index("ix_strategy_execution_bundles_status_created", "status", "created_at"),
    )

    bundle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    route_action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    bundle_type: Mapped[str] = mapped_column(String(32), nullable=False, default="single_sleeve", index=True)
    bundle_priority: Mapped[str] = mapped_column(String(32), nullable=False, default="standard", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    selected_symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gross_requested_exposure: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    net_approved_exposure: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    expected_cost_bps: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    expected_edge_bps: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    portfolio_risk_budget_state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Stage 5：乐观并发控制版本号。每次 save_execution_bundle 后 +1。
    # 多进程同时写同一 bundle 时，CAS 会拒绝过期请求，调用方需要重读后重试。
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


class SleeveBudgetProfileModel(Base):
    __tablename__ = "sleeve_budget_profiles"
    __table_args__ = (
        Index("ix_sleeve_budget_profiles_scope_updated", "product_type", "margin_mode", "updated_at"),
        Index("ix_sleeve_budget_profiles_family_updated", "family", "updated_at"),
    )

    budget_profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol_scope_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    quote_budget_limit: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    margin_budget_limit: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    notional_cap: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    max_symbol_notional: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    max_drawdown_usdt: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    allocator_base_weight: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("1"))
    hedge_priority_class: Mapped[str] = mapped_column(String(32), nullable=False, default="standard", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class SleeveBudgetAssignmentModel(Base):
    __tablename__ = "sleeve_budget_assignments"
    __table_args__ = (
        Index("ix_sleeve_budget_assignments_scope_updated", "product_type", "margin_mode", "updated_at"),
        Index("ix_sleeve_budget_assignments_sleeve_updated", "strategy_sleeve_id", "updated_at"),
    )

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    budget_profile_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy_sleeve_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    active_budget_multiplier: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("1"))
    allocator_base_weight: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("1"))
    effective_quote_budget_limit: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    effective_margin_budget_limit: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    effective_notional_cap: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    effective_max_symbol_notional: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    hedge_priority_class: Mapped[str] = mapped_column(String(32), nullable=False, default="standard", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class AllocatorBudgetSnapshotModel(Base):
    __tablename__ = "allocator_budget_snapshots"
    __table_args__ = (
        Index("ix_allocator_budget_snapshots_allocation_created", "allocation_id", "created_at"),
        Index("ix_allocator_budget_snapshots_sleeve_created", "strategy_sleeve_id", "created_at"),
    )

    budget_snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy_sleeve_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    requested_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    approved_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    requested_delta_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    approved_delta_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    budget_multiplier: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    allocator_weight: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    quote_budget_limit: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    margin_budget_limit: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    notional_cap: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    max_symbol_notional: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    hedge_priority_class: Mapped[str] = mapped_column(String(32), nullable=False, default="standard", index=True)
    priority_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    portfolio_requested_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    portfolio_approved_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    portfolio_budget_cut_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    clamped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class AllocatorConflictResolutionModel(Base):
    __tablename__ = "allocator_conflict_resolutions"
    __table_args__ = (
        Index("ix_allocator_conflict_resolutions_allocation_created", "allocation_id", "created_at"),
        Index("ix_allocator_conflict_resolutions_symbol_created", "symbol", "created_at"),
    )

    conflict_resolution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    conflict_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resolution_action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gross_requested_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    net_approved_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    blocked_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    protected_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    reduced_notional: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class AllocatorNettingDecisionModel(Base):
    __tablename__ = "allocator_netting_decisions"
    __table_args__ = (
        Index("ix_allocator_netting_decisions_allocation_created", "allocation_id", "created_at"),
        Index("ix_allocator_netting_decisions_symbol_created", "symbol", "created_at"),
    )

    netting_decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    gross_buy_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    gross_sell_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    net_approved_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class OrderStateModel(Base):
    __tablename__ = "order_states"
    __table_args__ = (
        Index("ix_order_states_scope_status", "product_type", "margin_mode", "status"),
        Index("ix_order_states_scope_symbol_update", "product_type", "margin_mode", "symbol", "last_update_ts"),
    )

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    submitted_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    filled_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    remaining_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    average_fill_price: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    fees: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    close_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    td_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_mode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    pos_side: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    reduce_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    settle_currency: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class FillEventModel(Base):
    __tablename__ = "fill_events"
    __table_args__ = (
        Index("ix_fill_events_scope_symbol_ts", "product_type", "margin_mode", "symbol", "ingestion_timestamp"),
    )

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exchange_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    fill_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    close_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    td_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_mode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    pos_side: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    reduce_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    settle_currency: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class FillOutcomeModel(Base):
    __tablename__ = "fill_outcomes"
    __table_args__ = (
        Index("ix_fill_outcomes_scope_symbol_ts", "product_type", "margin_mode", "symbol", "created_at"),
        Index("ix_fill_outcomes_order_id", "order_id", "created_at"),
        Index("ix_fill_outcomes_execution_attempt_id", "execution_attempt_id"),
    )

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    execution_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    venue: Mapped[str | None] = mapped_column(String(16), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    fill_qty: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    fill_price: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    fill_notional: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    fee_amount: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    fee_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    liquidity_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingestion_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    order_status_after_fill: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    target_leverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    exposure_side: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    position_intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    starting_position_qty: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    starting_avg_entry_price: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    ending_position_qty: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    ending_avg_entry_price: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    realized_pnl_delta: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    fee_delta: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class FundingFeeRecordModel(Base):
    __tablename__ = "funding_fee_records"
    __table_args__ = (
        Index("ix_funding_fee_records_scope_symbol_ts", "product_type", "margin_mode", "symbol", "bill_ts"),
        Index("ix_funding_fee_records_currency_ts", "currency", "bill_ts"),
    )

    bill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    balance_after: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    bill_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sub_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    type_label: Mapped[str] = mapped_column(String(64), nullable=False)
    sub_type_label: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_group: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    funding_direction: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    bill_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ledger_posting_state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    ledger_journal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ledger_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class SleevePnLRecordModel(Base):
    __tablename__ = "sleeve_pnl_records"
    __table_args__ = (
        Index("ix_sleeve_pnl_records_sleeve_created", "strategy_sleeve_id", "created_at"),
        Index("ix_sleeve_pnl_records_family_created", "strategy_family", "created_at"),
        Index("ix_sleeve_pnl_records_scope_symbol_created", "product_type", "margin_mode", "symbol", "created_at"),
        Index("ix_sleeve_pnl_records_allocation_created", "allocation_id", "created_at"),
        Index("ix_sleeve_pnl_records_bundle_created", "strategy_bundle_id", "created_at"),
    )

    record_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    fill_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    funding_fee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    fee_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    funding_fee_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    inventory_move_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    attribution_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class OrderObligationModel(Base):
    __tablename__ = "order_obligations"
    __table_args__ = (
        Index("ix_order_obligations_scope_status", "product_type", "margin_mode", "status"),
        Index("ix_order_obligations_currency_status", "reserve_currency", "status"),
    )

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    obligation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reserve_currency: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reserved_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    consumed_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    released_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    last_update_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_status_created", "status", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_component: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ExecutionOrderModel(Base):
    __tablename__ = "execution_orders"
    __table_args__ = (
        Index("ix_execution_orders_symbol_state", "symbol", "state"),
        Index("ix_execution_orders_decision_created", "decision_id", "created_at"),
        Index("ix_execution_orders_execution_attempt_id", "execution_attempt_id"),
    )

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    execution_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    venue_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str | None] = mapped_column(String(16), nullable=True)
    requested_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    close_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    td_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_mode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    pos_side: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    reduce_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    settle_currency: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    execution_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position_intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False, default="aats")
    last_exchange_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ExecutionOrderStateHistoryModel(Base):
    __tablename__ = "execution_order_state_history"
    __table_args__ = (
        Index("ix_execution_order_state_history_order", "order_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_orders.order_id"), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionCommandModel(Base):
    __tablename__ = "execution_commands"
    __table_args__ = (
        Index("ix_execution_commands_order_state", "order_id", "state"),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_orders.order_id"), nullable=False, index=True)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionFillModelV2(Base):
    __tablename__ = "execution_fills"
    __table_args__ = (
        Index("ix_execution_fills_order_ts", "order_id", "ingestion_ts"),
        Index("ix_execution_fills_symbol_ts", "symbol", "ingestion_ts"),
        Index("ix_execution_fills_source_venue_fill", "source_system", "venue_fill_id", unique=True),
        Index("ix_execution_fills_execution_attempt_id", "execution_attempt_id"),
    )

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    venue_fill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_orders.order_id"), nullable=False, index=True)
    execution_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    venue_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    fill_qty: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    fee_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    close_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    td_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_mode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    pos_side: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    reduce_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_only_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    settle_currency: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    strategy_family: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_leg_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    liquidity_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exchange_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LedgerAccountModel(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        Index(
            "ux_ledger_accounts_identity",
            "account_type",
            "currency",
            "product_type",
            "margin_mode",
            "symbol",
            unique=True,
        ),
    )

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LedgerJournalModel(Base):
    __tablename__ = "ledger_journals"
    __table_args__ = (
        Index("ix_ledger_journals_status_created", "status", "created_at"),
        Index("ux_ledger_journals_source", "source_type", "source_id", unique=True),
    )

    journal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    journal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)


class LedgerEntryModel(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_entries_journal", "journal_id"),
        Index("ix_ledger_entries_account_effective", "account_id", "effective_at", "created_at"),
    )

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    journal_id: Mapped[str] = mapped_column(String(64), ForeignKey("ledger_journals.journal_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(64), ForeignKey("ledger_accounts.account_id"), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReservationModel(Base):
    __tablename__ = "reservations"

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_orders.order_id"), nullable=False, unique=True, index=True)
    reserve_account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ledger_accounts.account_id"), nullable=False, index=True
    )
    reserved_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    consumed_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    released_amount: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SettlementModel(Base):
    __tablename__ = "settlements"

    settlement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fill_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_fills.fill_id"), nullable=False, unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_orders.order_id"), nullable=False, index=True)
    journal_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("ledger_journals.journal_id"), nullable=True, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PositionLotModel(Base):
    __tablename__ = "position_lots"
    __table_args__ = (
        Index("ix_position_lots_scope_symbol_status", "product_type", "margin_mode", "symbol", "status"),
        Index("ix_position_lots_source_fill", "source_fill_id"),
    )

    lot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    signed_quantity_open: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    source_fill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_leverage: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    exposure_side: Mapped[str] = mapped_column(String(16), nullable=False, default="flat")
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)


class LotEventModel(Base):
    __tablename__ = "lot_events"
    __table_args__ = (
        Index("ix_lot_events_scope_symbol", "product_type", "margin_mode", "symbol"),
        Index("ix_lot_events_fill_id", "fill_id"),
        Index("ix_lot_events_lot_id", "lot_id"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("position_lots.lot_id", ondelete="CASCADE"),
        nullable=False,
    )
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    exit_price: Mapped[Decimal | None] = mapped_column(DECIMAL_36_18, nullable=True)
    realized_pnl_delta: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ExternalEventInboxModel(Base):
    __tablename__ = "external_event_inbox"
    __table_args__ = (
        Index("ix_external_event_inbox_source_received", "source_system", "received_at"),
    )

    inbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_result: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommandOutboxModel(Base):
    __tablename__ = "command_outbox"
    __table_args__ = (
        Index("ix_command_outbox_status_created", "status", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconciliationReportModel(Base):
    __tablename__ = "reconciliation_reports"
    __table_args__ = (
        Index("ix_reconciliation_reports_scope_ts", "product_type", "margin_mode", "as_of_ts"),
    )

    reconciliation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    as_of_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    halt_required: Mapped[bool] = mapped_column(nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    primary_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ReconciliationFindingModel(Base):
    __tablename__ = "reconciliation_findings"
    __table_args__ = (
        Index("ix_reconciliation_findings_recon_created", "reconciliation_id", "created_at"),
        Index("ix_reconciliation_findings_scope_created", "product_type", "margin_mode", "created_at"),
        Index("ix_reconciliation_findings_layer_created", "layer", "created_at"),
        Index("ix_reconciliation_findings_sleeve_created", "strategy_sleeve_id", "created_at"),
        Index("ix_reconciliation_findings_bundle_created", "strategy_bundle_id", "created_at"),
    )

    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reconciliation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("reconciliation_reports.reconciliation_id"), nullable=False, index=True
    )
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    primary_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    layer: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    finding_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity_class: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    structural: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    financial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observational: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    only_reduce_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halt_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocks_resume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    details_json: Mapped[dict] = mapped_column("details", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BaselineGenerationModel(Base):
    __tablename__ = "baseline_generations"
    __table_args__ = (
        Index("ix_baseline_generations_scope_imported", "product_type", "margin_mode", "imported_at"),
        Index("ix_baseline_generations_account_source_imported", "account_source", "imported_at"),
    )

    generation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    baseline_event_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    baseline_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    baseline_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    allowed_symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    exchange_snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    safe_for_automatic_continuation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_operator_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    previous_generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    previous_baseline_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    exchange_ack_watermark_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operator_action_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trigger_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False)
    balance_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExchangeAckWatermarkModel(Base):
    __tablename__ = "exchange_ack_watermarks"
    __table_args__ = (
        Index("ix_exchange_ack_watermarks_scope_ack", "product_type", "margin_mode", "acknowledged_at"),
        Index("ix_exchange_ack_watermarks_account_source_ack", "account_source", "acknowledged_at"),
    )

    watermark_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    allowed_symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    latest_bill_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latest_bill_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_fill_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latest_fill_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_order_snapshot_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_reconciliation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    baseline_event_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operator_action_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    details_json: Mapped[dict] = mapped_column("details", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReconciliationStateSnapshotModel(Base):
    __tablename__ = "reconciliation_state_snapshots"
    __table_args__ = (
        Index("ix_reconciliation_state_scope_created", "product_type", "margin_mode", "created_at"),
        Index("ix_reconciliation_state_recovery_created", "recovery_state", "created_at"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reconciliation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("reconciliation_reports.reconciliation_id"), nullable=False, index=True
    )
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    primary_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    recovery_state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resume_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    safe_to_trade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    only_reduce_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halt_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bundle_recovery_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resume_blocked_reasons_json: Mapped[list] = mapped_column(JSON, nullable=False)
    derived_from_generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    exchange_ack_watermark_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    details_json: Mapped[dict] = mapped_column("details", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExitExecutionIntentModel(Base):
    __tablename__ = "exit_execution_intents"
    __table_args__ = (
        Index("ix_exit_execution_intents_chain", "execution_chain_id"),
        Index("ix_exit_execution_intents_symbol_updated", "symbol", "updated_at"),
        Index("ix_exit_execution_intents_status_updated", "aggregate_status", "updated_at"),
    )

    parent_intent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution_chain_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    aggregate_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reconciliation_state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_exit_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    aggregated_filled_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    open_child_working_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    open_child_unknown_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    remaining_dispatchable_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    remaining_unresolved_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    operator_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ExitExecutionChildRefModel(Base):
    __tablename__ = "exit_execution_child_refs"
    __table_args__ = (
        Index("ix_exit_execution_child_refs_parent_updated", "parent_intent_id", "updated_at"),
        Index("ix_exit_execution_child_refs_chain", "execution_chain_id"),
    )

    client_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_intent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("exit_execution_intents.parent_intent_id"), nullable=False, index=True
    )
    child_order_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    execution_chain_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    planned_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False)
    known_filled_quantity: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    remaining_quantity_estimate: Mapped[Decimal] = mapped_column(DECIMAL_36_18, nullable=False, default=Decimal("0"))
    child_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    aggregate_category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange_truth_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operator_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_reducing_invariant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class DecisionAuditRecordModel(Base):
    __tablename__ = "decision_audit_records"

    audit_revision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    selected_strategy_sleeve_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    decision_context_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_coordinator_snapshot_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_sleeve_intent_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    portfolio_allocation_decision_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_assessment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_decision_brief_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_market_assessment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_action_proposal_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_shadow_decision_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ai_shadow_evaluation_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    position_target_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_outcome_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_decision_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_decision_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_plan_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_plan_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    strategy_execution_bundle_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_intent_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    order_state_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    fill_event_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    portfolio_delta_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class OperatorUserModel(Base):
    __tablename__ = "operator_users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failed_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategyProfileRevisionModel(Base):
    __tablename__ = "strategy_profile_revisions"
    __table_args__ = (
        Index("ix_strategy_profile_revisions_profile_status", "profile_id", "status"),
        Index("ix_strategy_profile_revisions_scope", "product_type", "margin_mode"),
    )

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_family: Mapped[str] = mapped_column(String(32), nullable=False, default="strategy_tuning")
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile_label: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    market_intent: Mapped[str] = mapped_column(String(32), nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    allowed_symbols_json: Mapped[list] = mapped_column(JSON, nullable=False)
    hot_safe_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_switch_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    guardrails_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expected_behavior_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    source_recommendation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategyProfileActivationModel(Base):
    __tablename__ = "strategy_profile_activation"
    __table_args__ = (
        Index(
            "ix_strategy_profile_activation_scope",
            "product_type",
            "margin_mode",
            "allowed_symbols_hash",
            unique=True,
        ),
    )

    activation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    allowed_symbols_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategyProfileRecommendationModel(Base):
    __tablename__ = "strategy_profile_recommendations"
    __table_args__ = (
        Index("ix_strategy_profile_recommendations_scope_time", "product_type", "margin_mode", "generated_at"),
    )

    recommendation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    allowed_symbols_json: Mapped[list] = mapped_column(JSON, nullable=False)
    decision_status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategyProfileActivationHistoryModel(Base):
    __tablename__ = "strategy_profile_activation_history"
    __table_args__ = (
        Index("ix_strategy_profile_activation_history_scope_time", "product_type", "margin_mode", "executed_at"),
    )

    activation_event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategyProfileRejectionModel(Base):
    __tablename__ = "strategy_profile_rejections"
    __table_args__ = (
        Index("ix_strategy_profile_rejections_scope_time", "product_type", "margin_mode", "created_at"),
    )

    rejection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StrategyProfileEvaluationModel(Base):
    __tablename__ = "strategy_profile_evaluations"
    __table_args__ = (
        Index("ix_strategy_profile_evaluations_scope_time", "product_type", "margin_mode", "created_at"),
    )

    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
