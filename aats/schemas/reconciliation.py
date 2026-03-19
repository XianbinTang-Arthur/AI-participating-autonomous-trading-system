from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from aats.schemas.common import SchemaBase
from aats.schemas.system import MarginModelType, ProductType


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
    mismatch_categories: list[str] = Field(default_factory=list)
    mismatch_reasons: list[str] = Field(default_factory=list)
    safety_impacts: list[str] = Field(default_factory=list)
    severity: str
    review_required: bool = False
    recommended_operator_action: str | None = None
    remediation_action: str | None = None
    halt_required: bool = False
