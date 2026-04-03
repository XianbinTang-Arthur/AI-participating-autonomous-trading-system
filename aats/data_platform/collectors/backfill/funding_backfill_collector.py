"""Funding historical backfill collector."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.collectors.backfill.file_parser import parse_funding_zip
from aats.data_platform.jobs.run_registry import (
    create_ingest_run,
    create_run_item,
    finish_ingest_run,
    finish_run_item,
)
from aats.data_platform.models import FundingRow, funding_table_name, utc_now

log = logging.getLogger(__name__)

BATCH_SIZE = 2000


def _write_funding_staging_batch(
    session: Session,
    table: str,
    rows: list[FundingRow],
    ingest_run_id: str,
    dataset_version: str,
    source_file_id: str | None,
) -> int:
    if not rows:
        return 0
    values: list[dict[str, Any]] = []
    for r in rows:
        values.append(dict(
            symbol=r.symbol, ts=r.ts,
            funding_rate=r.funding_rate,
            inst_type=r.inst_type,
            formula_type=r.formula_type,
            method=r.method,
            realized_rate=r.realized_rate,
            raw_symbol=r.raw_symbol, raw_ts=r.raw_ts,
            source_file_id=source_file_id,
            ingest_run_id=ingest_run_id,
            dataset_version=dataset_version,
            now=utc_now(),
        ))
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


def collect_backfill_funding_file(
    session: Session,
    *,
    source_file_id: str,
    zip_path: str,
    symbol_hint: str,
    dataset_version: str = "v1.0",
) -> str:
    """Run a complete backfill for one funding ZIP file. Returns ingest_run_id."""
    table = funding_table_name("staging")

    run_id = create_ingest_run(
        session,
        run_type="backfill",
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol_hint.upper(),
        trigger_mode="manual",
    )
    item_id = create_run_item(
        session,
        ingest_run_id=run_id,
        dataset_domain="funding",
        instrument_type="swap",
        symbol=symbol_hint.upper(),
        source_file_id=source_file_id,
    )

    try:
        rows = parse_funding_zip(zip_path, symbol_hint)
        count = _write_funding_staging_batch(
            session, table, rows, run_id, dataset_version, source_file_id,
        )
        session.execute(
            text("""
                UPDATE meta.raw_source_files
                SET parse_status = 'parsed', raw_row_count = :cnt,
                    ingested_status = 'ingested', updated_at = :now
                WHERE source_file_id = :fid
            """),
            dict(cnt=len(rows), now=utc_now(), fid=source_file_id),
        )
        finish_run_item(session, item_id, status="succeeded",
                        raw_rows_read=len(rows), rows_written_staging=count)
        finish_ingest_run(session, run_id, status="succeeded")
        log.info("Backfill funding OK: %s rows -> %s", count, table)
    except Exception as exc:
        finish_run_item(session, item_id, status="failed", error_message=str(exc))
        finish_ingest_run(session, run_id, status="failed", error_message=str(exc))
        session.execute(
            text("""
                UPDATE meta.raw_source_files
                SET parse_status = 'failed', parse_error = :err, updated_at = :now
                WHERE source_file_id = :fid
            """),
            dict(err=str(exc), now=utc_now(), fid=source_file_id),
        )
        raise

    return run_id
