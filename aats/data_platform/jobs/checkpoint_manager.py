"""Rolling ingestion checkpoint (watermark) management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.models import utc_now


def get_checkpoint(
    session: Session,
    *,
    dataset_domain: str,
    instrument_type: str,
    symbol: str,
    timeframe: str | None,
) -> dict[str, Any] | None:
    """Load the current checkpoint row, or None if not yet created."""
    row = session.execute(
        text("""
            SELECT checkpoint_id, last_successful_ts, last_attempted_ts,
                   next_expected_ts, backfill_completed, gap_detected,
                   gap_start_ts, gap_end_ts, checkpoint_status, last_ingest_run_id
            FROM meta.ingest_checkpoints
            WHERE dataset_domain = :domain
              AND instrument_type = :inst
              AND symbol = :symbol
              AND timeframe IS NOT DISTINCT FROM :tf
        """),
        dict(domain=dataset_domain, inst=instrument_type, symbol=symbol, tf=timeframe),
    ).mappings().first()
    return dict(row) if row else None


def upsert_checkpoint(
    session: Session,
    *,
    dataset_domain: str,
    instrument_type: str,
    symbol: str,
    timeframe: str | None,
    last_successful_ts: datetime | None = None,
    last_attempted_ts: datetime | None = None,
    next_expected_ts: datetime | None = None,
    gap_detected: bool = False,
    gap_start_ts: datetime | None = None,
    gap_end_ts: datetime | None = None,
    checkpoint_status: str = "active",
    last_ingest_run_id: str | None = None,
) -> str:
    """Insert or update a checkpoint. Returns checkpoint_id."""
    now = utc_now()
    cp_id = str(uuid.uuid4())
    session.execute(
        text("""
            INSERT INTO meta.ingest_checkpoints
                (checkpoint_id, dataset_domain, instrument_type, symbol, timeframe,
                 last_successful_ts, last_attempted_ts, next_expected_ts,
                 gap_detected, gap_start_ts, gap_end_ts,
                 checkpoint_status, last_ingest_run_id,
                 created_at, updated_at)
            VALUES
                (:cp_id, :domain, :inst, :symbol, :tf,
                 :ls_ts, :la_ts, :ne_ts,
                 :gap, :gs, :ge,
                 :status, :run_id, :now, :now)
            ON CONFLICT (dataset_domain, instrument_type, symbol, timeframe)
            DO UPDATE SET
                last_successful_ts  = COALESCE(EXCLUDED.last_successful_ts, meta.ingest_checkpoints.last_successful_ts),
                last_attempted_ts   = COALESCE(EXCLUDED.last_attempted_ts, meta.ingest_checkpoints.last_attempted_ts),
                next_expected_ts    = COALESCE(EXCLUDED.next_expected_ts, meta.ingest_checkpoints.next_expected_ts),
                gap_detected        = EXCLUDED.gap_detected,
                gap_start_ts        = EXCLUDED.gap_start_ts,
                gap_end_ts          = EXCLUDED.gap_end_ts,
                checkpoint_status   = EXCLUDED.checkpoint_status,
                last_ingest_run_id  = EXCLUDED.last_ingest_run_id,
                updated_at          = EXCLUDED.updated_at
            RETURNING checkpoint_id
        """),
        dict(
            cp_id=cp_id, domain=dataset_domain, inst=instrument_type,
            symbol=symbol, tf=timeframe,
            ls_ts=last_successful_ts, la_ts=last_attempted_ts, ne_ts=next_expected_ts,
            gap=gap_detected, gs=gap_start_ts, ge=gap_end_ts,
            status=checkpoint_status, run_id=last_ingest_run_id, now=now,
        ),
    )
    result = session.execute(
        text("""
            SELECT checkpoint_id FROM meta.ingest_checkpoints
            WHERE dataset_domain = :domain AND instrument_type = :inst
              AND symbol = :symbol AND timeframe IS NOT DISTINCT FROM :tf
        """),
        dict(domain=dataset_domain, inst=instrument_type, symbol=symbol, tf=timeframe),
    ).scalar()
    return str(result)


def advance_checkpoint(
    session: Session,
    *,
    dataset_domain: str,
    instrument_type: str,
    symbol: str,
    timeframe: str | None,
    new_successful_ts: datetime,
    next_expected_ts: datetime,
    ingest_run_id: str,
) -> None:
    """Move the checkpoint forward after a successful merge."""
    now = utc_now()
    session.execute(
        text("""
            UPDATE meta.ingest_checkpoints
            SET last_successful_ts = :ls,
                last_attempted_ts = :ls,
                next_expected_ts = :ne,
                gap_detected = FALSE,
                gap_start_ts = NULL,
                gap_end_ts = NULL,
                checkpoint_status = 'active',
                last_ingest_run_id = :run_id,
                updated_at = :now
            WHERE dataset_domain = :domain
              AND instrument_type = :inst
              AND symbol = :symbol
              AND timeframe IS NOT DISTINCT FROM :tf
        """),
        dict(
            ls=new_successful_ts, ne=next_expected_ts,
            run_id=ingest_run_id, now=now,
            domain=dataset_domain, inst=instrument_type,
            symbol=symbol, tf=timeframe,
        ),
    )
