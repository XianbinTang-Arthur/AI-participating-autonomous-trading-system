from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id
from aats.schemas.system import MarginModelType, ProductType


ReconciliationFindingLayer = Literal["structural", "financial", "observational"]
ReconciliationFindingSeverity = Literal["info", "soft", "review", "halt"]


class ReconciliationFinding(SchemaBase):
    finding_id: str = Field(default_factory=lambda: new_id("reconfinding"))
    reconciliation_id: str
    scope_kind: Literal[
        "account",
        "symbol",
        "position",
        "order",
        "fill",
        "bundle",
        "sleeve",
        "allocation",
    ] = "account"
    scope_ref: str | None = None
    product_type: ProductType | None = None
    margin_mode: MarginModelType | None = None
    primary_symbol: str | None = None
    strategy_sleeve_id: str | None = None
    allocation_id: str | None = None
    strategy_bundle_id: str | None = None
    layer: ReconciliationFindingLayer
    finding_type: str
    severity_class: ReconciliationFindingSeverity
    structural: bool = False
    financial: bool = False
    observational: bool = False
    review_required: bool = False
    only_reduce_required: bool = False
    halt_required: bool = False
    blocks_resume: bool = False
    reason_code: str
    details_json: dict[str, Any] = Field(default_factory=dict)


class ExchangeAckWatermark(SchemaBase):
    watermark_id: str = Field(default_factory=lambda: new_id("watermark"))
    account_source: str
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: list[str] = Field(default_factory=list)
    acknowledged_at: datetime
    latest_bill_id: str | None = None
    latest_bill_ts: datetime | None = None
    latest_fill_id: str | None = None
    latest_fill_ts: datetime | None = None
    latest_order_snapshot_ts: datetime | None = None
    latest_reconciliation_id: str | None = None
    baseline_event_ref: str | None = None
    operator_action_ref: str | None = None
    details_json: dict[str, Any] = Field(default_factory=dict)


class BaselineGenerationRecord(SchemaBase):
    generation_id: str = Field(default_factory=lambda: new_id("baselinegen"))
    baseline_event_ref: str
    baseline_id: str | None = None
    baseline_kind: Literal["startup_import", "operator_rebaseline"]
    account_source: str
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: list[str] = Field(default_factory=list)
    exchange_snapshot_ts: datetime
    imported_at: datetime
    safe_for_automatic_continuation: bool = True
    requires_operator_review: bool = False
    previous_generation_id: str | None = None
    previous_baseline_ref: str | None = None
    exchange_ack_watermark_id: str | None = None
    operator_action_ref: str | None = None
    trigger_reason: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    balance_count: int = 0
    position_count: int = 0
    open_order_count: int = 0
    fill_count: int = 0


class ReconciliationStateSnapshot(SchemaBase):
    snapshot_id: str = Field(default_factory=lambda: new_id("reconstate"))
    reconciliation_id: str
    product_type: ProductType | None = None
    margin_mode: MarginModelType | None = None
    primary_symbol: str | None = None
    recovery_state: str
    resume_eligible: bool = False
    safe_to_trade: bool = False
    review_required: bool = False
    only_reduce_required: bool = False
    halt_required: bool = False
    bundle_recovery_required: bool = False
    resume_blocked_reasons_json: list[str] = Field(default_factory=list)
    derived_from_generation_id: str | None = None
    exchange_ack_watermark_id: str | None = None
    details_json: dict[str, Any] = Field(default_factory=dict)


class ReconciliationReport(SchemaBase):
    reconciliation_id: str
    decision_id: str | None = None
    portfolio_snapshot_ref: str | None = None
    as_of_ts: datetime
    exchange_snapshot_ts: datetime | None = None
    product_type: ProductType | None = None
    margin_mode: MarginModelType | None = None
    allowed_symbols: list[str] = Field(default_factory=list)
    exchange_comparison_enabled: bool = False
    order_diff: dict[str, Any]
    fill_diff: dict[str, Any]
    balance_diff: dict[str, Any]
    position_diff: dict[str, Any]
    exchange_bills_summary: dict[str, Any] = Field(default_factory=dict)
    exchange_bills_explanations: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[ReconciliationFinding] = Field(default_factory=list)
    finding_summary: dict[str, Any] = Field(default_factory=dict)
    baseline_generation_id: str | None = None
    exchange_ack_watermark_id: str | None = None
    mismatch_categories: list[str] = Field(default_factory=list)
    mismatch_reasons: list[str] = Field(default_factory=list)
    safety_impacts: list[str] = Field(default_factory=list)
    severity: str
    recovery_classification: str | None = None
    auto_repairable: bool = False
    resume_blocking: bool = False
    review_required: bool = False
    only_reduce_required: bool = False
    only_reduce_reasons: list[str] = Field(default_factory=list)
    unknown_state_details: list[dict[str, Any]] = Field(default_factory=list)
    recommended_operator_action: str | None = None
    remediation_action: str | None = None
    halt_required: bool = False
    structural_review_required: bool = False
    financial_review_required: bool = False
    observational_only: bool = False
