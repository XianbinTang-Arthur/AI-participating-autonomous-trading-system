"""Ingest run and run-item lifecycle management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import utc_now


def create_ingest_run(
    session: Session,
    *,
    run_type: str,
    dataset_domain: str,
    instrument_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    trigger_mode: str = "manual",
) -> str:
    """Create a new ingest_run and return its UUID string."""
    run_id = str(uuid.uuid4())
    now = utc_now()
    session.execute(
        text("""
            INSERT INTO meta.ingest_runs
                (ingest_run_id, run_type, dataset_domain, instrument_type,
                 symbol, timeframe, trigger_mode, status, started_at,
                 created_at, updated_at)
            VALUES
                (:run_id, :run_type, :domain, :inst, :symbol, :tf,
                 :trigger, 'running', :now, :now, :now)
        """),
        dict(
            run_id=run_id, run_type=run_type, domain=dataset_domain,
            inst=instrument_type, symbol=symbol, tf=timeframe,
            trigger=trigger_mode, now=now,
        ),
    )
    return run_id


def finish_ingest_run(
    session: Session,
    run_id: str,
    *,
    status: str = "succeeded",
    error_message: str | None = None,
    checkpoint_after: dict[str, Any] | None = None,
) -> None:
    import json
    now = utc_now()
    session.execute(
        text("""
            UPDATE meta.ingest_runs
            SET status = :status,
                ended_at = :now,
                error_message = :err,
                checkpoint_after = :cp_after,
                updated_at = :now
            WHERE ingest_run_id = :run_id
        """),
        dict(
            status=status, now=now, err=error_message,
            cp_after=json.dumps(checkpoint_after) if checkpoint_after else None,
            run_id=run_id,
        ),
    )


def mark_orphaned_ingest_runs(
    session: Session,
    *,
    run_type: str,
    dataset_domain: str,
    instrument_type: str | None = None,
    trigger_mode: str | None = None,
    reason: str,
) -> int:
    """Close stale ``running`` ingest runs before replacing a daemon process.

    Long-running collectors normally finish their run in ``finally``. Container
    kills and host restarts bypass that path, leaving old rows permanently
    ``running``. This helper is intentionally narrow: callers must provide the
    exact run/domain/trigger tuple they are replacing.
    """
    now = utc_now()
    result = session.execute(
        text("""
            UPDATE meta.ingest_runs
            SET status = 'failed',
                ended_at = COALESCE(ended_at, :now),
                error_message = COALESCE(error_message, :reason),
                updated_at = :now
            WHERE status = 'running'
              AND run_type = :run_type
              AND dataset_domain = :domain
              AND (:instrument_type IS NULL OR instrument_type = :instrument_type)
              AND (:trigger_mode IS NULL OR trigger_mode = :trigger_mode)
        """),
        dict(
            now=now,
            reason=reason,
            run_type=run_type,
            domain=dataset_domain,
            instrument_type=instrument_type,
            trigger_mode=trigger_mode,
        ),
    )
    rowcount = getattr(result, "rowcount", None)
    return int(rowcount) if rowcount is not None and rowcount >= 0 else 0


def create_run_item(
    session: Session,
    *,
    ingest_run_id: str,
    dataset_domain: str,
    instrument_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    window_start_ts: datetime | None = None,
    window_end_ts: datetime | None = None,
    source_file_id: str | None = None,
) -> str:
    """Create a run item and return its UUID string."""
    item_id = str(uuid.uuid4())
    now = utc_now()
    session.execute(
        text("""
            INSERT INTO meta.ingest_run_items
                (ingest_run_item_id, ingest_run_id, dataset_domain,
                 instrument_type, symbol, timeframe,
                 window_start_ts, window_end_ts, source_file_id,
                 status, created_at, updated_at)
            VALUES
                (:item_id, :run_id, :domain, :inst, :symbol, :tf,
                 :ws, :we, :sf_id, 'running', :now, :now)
        """),
        dict(
            item_id=item_id, run_id=ingest_run_id, domain=dataset_domain,
            inst=instrument_type, symbol=symbol, tf=timeframe,
            ws=window_start_ts, we=window_end_ts,
            sf_id=source_file_id, now=now,
        ),
    )
    return item_id


def finish_run_item(
    session: Session,
    item_id: str,
    *,
    status: str = "succeeded",
    raw_rows_read: int | None = None,
    rows_written_staging: int | None = None,
    rows_written_bronze: int | None = None,
    rows_written_silver: int | None = None,
    rows_written_gold: int | None = None,
    error_message: str | None = None,
) -> None:
    now = utc_now()
    session.execute(
        text("""
            UPDATE meta.ingest_run_items
            SET status = :status,
                raw_rows_read = :rr,
                rows_written_staging = :ws,
                rows_written_bronze = :wb,
                rows_written_silver = :wslv,
                rows_written_gold = :wg,
                error_message = :err,
                updated_at = :now
            WHERE ingest_run_item_id = :item_id
        """),
        dict(
            status=status, rr=raw_rows_read,
            ws=rows_written_staging, wb=rows_written_bronze,
            wslv=rows_written_silver, wg=rows_written_gold,
            err=error_message, now=now, item_id=item_id,
        ),
    )
