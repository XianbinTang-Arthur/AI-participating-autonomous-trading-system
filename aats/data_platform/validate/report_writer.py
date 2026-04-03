"""Quality report writer — persists validation results to meta.quality_reports."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import utc_now


def write_quality_report(
    session: Session,
    *,
    ingest_run_id: str | None = None,
    dataset_layer: str,
    dataset_domain: str,
    instrument_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    dataset_version: str,
    window_start_ts: datetime | None = None,
    window_end_ts: datetime | None = None,
    total_rows: int = 0,
    missing_intervals_count: int = 0,
    duplicate_rows_count: int = 0,
    out_of_order_rows_count: int = 0,
    invalid_price_rows_count: int = 0,
    invalid_volume_rows_count: int = 0,
    suspect_rows_count: int = 0,
    quality_status: str = "pass",
    details: dict[str, Any] | None = None,
) -> str:
    """Insert a quality report row. Returns the report UUID."""
    report_id = str(uuid.uuid4())
    now = utc_now()
    session.execute(
        text("""
            INSERT INTO meta.quality_reports
                (quality_report_id, ingest_run_id, dataset_layer, dataset_domain,
                 instrument_type, symbol, timeframe, dataset_version,
                 window_start_ts, window_end_ts, total_rows,
                 missing_intervals_count, duplicate_rows_count,
                 out_of_order_rows_count, invalid_price_rows_count,
                 invalid_volume_rows_count, suspect_rows_count,
                 quality_status, details, created_at, updated_at)
            VALUES
                (:id, :run_id, :layer, :domain, :inst, :symbol, :tf, :ver,
                 :ws, :we, :total,
                 :missing, :dup, :ooo, :price, :vol, :suspect,
                 :status, :details, :now, :now)
        """),
        dict(
            id=report_id, run_id=ingest_run_id,
            layer=dataset_layer, domain=dataset_domain,
            inst=instrument_type, symbol=symbol, tf=timeframe, ver=dataset_version,
            ws=window_start_ts, we=window_end_ts, total=total_rows,
            missing=missing_intervals_count, dup=duplicate_rows_count,
            ooo=out_of_order_rows_count, price=invalid_price_rows_count,
            vol=invalid_volume_rows_count, suspect=suspect_rows_count,
            status=quality_status,
            details=json.dumps(details) if details else None,
            now=now,
        ),
    )
    return report_id
