from __future__ import annotations

from datetime import datetime
from typing import Any

from aats.schemas.common import SchemaBase


class ReconciliationReport(SchemaBase):
    reconciliation_id: str
    as_of_ts: datetime
    order_diff: dict[str, Any]
    fill_diff: dict[str, Any]
    balance_diff: dict[str, Any]
    position_diff: dict[str, Any]
    severity: str
    remediation_action: str | None = None
    halt_required: bool = False

