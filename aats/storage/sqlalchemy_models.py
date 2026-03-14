from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EventEnvelopeModel(Base):
    __tablename__ = "event_store"

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
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class PortfolioSnapshotModel(Base):
    __tablename__ = "portfolio_snapshots"

    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_equity: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class OrderStateModel(Base):
    __tablename__ = "order_states"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
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
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class FillEventModel(Base):
    __tablename__ = "fill_events"

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
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ReconciliationReportModel(Base):
    __tablename__ = "reconciliation_reports"

    reconciliation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    as_of_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    halt_required: Mapped[bool] = mapped_column(nullable=False)
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
    order_intent_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    fill_event_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    portfolio_delta_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
