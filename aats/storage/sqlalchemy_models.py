from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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


class PortfolioSnapshotModel(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("ix_portfolio_snapshots_scope_seq", "product_type", "margin_mode", "sequence_id"),
    )

    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_equity: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    primary_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
    requested_qty: Mapped[float] = mapped_column(Float, nullable=False)
    filled_qty: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_qty: Mapped[float] = mapped_column(Float, nullable=False)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, nullable=False)
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
    fill_qty: Mapped[float] = mapped_column(Float, nullable=False)
    fill_price: Mapped[float] = mapped_column(Float, nullable=False)
    fee_amount: Mapped[float] = mapped_column(Float, nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    position_intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
    reserved_amount: Mapped[float] = mapped_column(Float, nullable=False)
    consumed_amount: Mapped[float] = mapped_column(Float, nullable=False)
    released_amount: Mapped[float] = mapped_column(Float, nullable=False)
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


class DecisionAuditRecordModel(Base):
    __tablename__ = "decision_audit_records"

    audit_revision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision_context_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_assessment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_market_assessment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_action_proposal_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position_target_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_decision_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_decision_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_plan_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class RuntimeProfileRevisionModel(Base):
    __tablename__ = "runtime_profile_revisions"

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_label: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    change_classification: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supersedes_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activation_note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)


class RuntimeProfileActivationModel(Base):
    __tablename__ = "runtime_profile_activation"

    activation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
