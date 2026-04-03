"""Rolling funding API collector.

Fetches incremental funding rate history from OKX REST API
GET /api/v5/public/funding-rate-history
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.config import ResearchPlatformSettings
from aats.data_platform.jobs.checkpoint_manager import get_checkpoint, upsert_checkpoint
from aats.data_platform.jobs.run_registry import (
    create_ingest_run,
    create_run_item,
    finish_ingest_run,
    finish_run_item,
)
from aats.data_platform.models import FundingRow, funding_table_name, utc_now

log = logging.getLogger(__name__)

BATCH_SIZE = 2000
_API_LIMIT = 100


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _parse_api_funding(item: dict[str, Any], symbol: str) -> FundingRow | None:
    """Parse one funding record from API JSON object."""
    try:
        ts = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
    except (ValueError, KeyError, OSError):
        return None
    try:
        rate = Decimal(item["fundingRate"])
    except (InvalidOperation, KeyError):
        return None
    return FundingRow(
        symbol=symbol.upper(),
        ts=ts,
        funding_rate=rate,
        inst_type=item.get("instType"),
        formula_type=item.get("formulaType"),
        method=item.get("method"),
        realized_rate=Decimal(item["realizedRate"]) if item.get("realizedRate") else None,
        raw_symbol=item.get("instId", symbol),
        raw_ts=item.get("fundingTime"),
    )


def _fetch_funding(
    client: httpx.Client,
    settings: ResearchPlatformSettings,
    symbol: str,
    after_ms: int | None = None,
    before_ms: int | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "instId": symbol,
        "limit": str(_API_LIMIT),
    }
    if after_ms is not None:
        params["after"] = str(after_ms)
    if before_ms is not None:
        params["before"] = str(before_ms)

    url = f"{settings.okx_rest_url}/api/v5/public/funding-rate-history"
    resp = client.get(url, params=params, timeout=settings.okx_timeout_seconds)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "0":
        raise RuntimeError(f"OKX funding API error: {body.get('msg', body)}")
    return body.get("data", [])


def _write_staging(
    session: Session,
    table: str,
    rows: list[FundingRow],
    run_id: str,
    dataset_version: str,
) -> int:
    if not rows:
        return 0
    now = utc_now()
    values = [
        dict(
            symbol=r.symbol, ts=r.ts,
            funding_rate=r.funding_rate,
            inst_type=r.inst_type,
            formula_type=r.formula_type,
            method=r.method,
            realized_rate=r.realized_rate,
            raw_symbol=r.raw_symbol, raw_ts=r.raw_ts,
            source_file_id=None,
            ingest_run_id=run_id,
            dataset_version=dataset_version,
            now=now,
        )
        for r in rows
    ]
    total = 0
    for i in range(0, len(values), BATCH_SIZE):
        batch = values[i : i + BATCH_SIZE]
        session.execute(
            text(f"""
                INSERT INTO {table}
                    (symbol, ts, funding_rate, inst_type, formula_type,
                     method, realized_rate,
                     raw_symbol, raw_ts, source_file_id,
                     ingest_run_id, dataset_version, created_at, updated_at)
                VALUES
                    (:symbol, :ts, :funding_rate, :inst_type, :formula_type,
                     :method, :realized_rate,
                     :raw_symbol, :raw_ts, :source_file_id,
                     :ingest_run_id, :dataset_version, :now, :now)
            """),
            batch,
        )
        total += len(batch)
    return total


def collect_funding_incremental(
    session: Session,
    settings: ResearchPlatformSettings,
    *,
    symbol: str,
    dataset_version: str = "v1.0",
    max_pages: int = 10,
) -> str:
    """Fetch recent funding rates and write to staging. Returns ingest_run_id."""
    table = funding_table_name("staging")

    cp = get_checkpoint(
        session,
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol.upper(),
        timeframe=None,
    )

    run_id = create_ingest_run(
        session,
        run_type="rolling",
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol.upper(),
        trigger_mode="scheduler",
    )
    item_id = create_run_item(
        session,
        ingest_run_id=run_id,
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol.upper(),
    )

    try:
        all_rows: list[FundingRow] = []
        checkpoint_ts: datetime | None = None
        if cp and cp.get("last_successful_ts"):
            checkpoint_ts = cp["last_successful_ts"]

        # OKX funding-rate-history semantics (results newest-first):
        #   before=X -> records with fundingTime > X  (NEWER)
        #   after=X  -> records with fundingTime < X  (OLDER)
        #
        # Rolling: fetch latest, page backward toward checkpoint.

        with httpx.Client() as client:
            raw_data = _fetch_funding(client, settings, symbol)
            page = 0
            while raw_data and page < max_pages:
                for item in raw_data:
                    row = _parse_api_funding(item, symbol)
                    if row:
                        all_rows.append(row)
                oldest_ts = min(int(d["fundingTime"]) for d in raw_data)
                if checkpoint_ts and oldest_ts <= _ts_ms(checkpoint_ts):
                    break
                if len(raw_data) < _API_LIMIT:
                    break
                time.sleep(settings.okx_rate_limit_sleep)
                raw_data = _fetch_funding(
                    client, settings, symbol, after_ms=oldest_ts,
                )
                page += 1

        # Filter: only keep rows strictly newer than checkpoint
        if checkpoint_ts:
            all_rows = [r for r in all_rows if r.ts > checkpoint_ts]

        count = _write_staging(session, table, all_rows, run_id, dataset_version)

        if all_rows:
            newest_ts = max(r.ts for r in all_rows)
            upsert_checkpoint(
                session,
                dataset_domain="funding",
                instrument_type="swap",
                symbol=symbol.upper(),
                timeframe=None,
                last_successful_ts=newest_ts,
                next_expected_ts=newest_ts + timedelta(hours=8),
                last_ingest_run_id=run_id,
            )

        finish_run_item(session, item_id, status="succeeded",
                        raw_rows_read=len(all_rows), rows_written_staging=count)
        finish_ingest_run(session, run_id, status="succeeded")
        log.info("Rolling funding OK: %s — %d rows", symbol, count)
    except Exception as exc:
        finish_run_item(session, item_id, status="failed", error_message=str(exc))
        finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
        raise

    return run_id
