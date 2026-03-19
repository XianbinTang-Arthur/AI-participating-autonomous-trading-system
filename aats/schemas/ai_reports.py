from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field

from aats.schemas.common import SchemaBase, new_id, utc_now


class AIPerformanceWindowReport(SchemaBase):
    label: str
    sample_size: int
    outperformed_rate: float = 0.0
    baseline_net_pnl_total: Decimal | None = None
    shadow_net_pnl_total: Decimal | None = None
    net_pnl_delta_total: Decimal | None = None
    avg_fee_ratio_delta: float | None = None
    avg_churn_ratio_delta: float | None = None
    review_required_count: int = 0


class AIPerformanceReport(SchemaBase):
    report_id: str = Field(default_factory=lambda: new_id("ai_perf"))
    generated_at: datetime = Field(default_factory=utc_now)
    symbol: str
    timeframe: str
    product_type: str
    margin_mode: str
    allowed_symbols: tuple[str, ...] = Field(default_factory=tuple)
    configured_operating_mode: str
    effective_operating_mode: str
    window_count: int = 0
    latest_evaluation_ref: str | None = None
    latest_evaluation_id: str | None = None
    latest_status: str = "insufficient_data"
    review_required: bool = False
    windows: dict[str, AIPerformanceWindowReport] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
