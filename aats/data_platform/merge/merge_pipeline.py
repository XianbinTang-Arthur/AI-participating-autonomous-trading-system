"""End-to-end merge pipeline: staging -> bronze -> silver.

Combines validation, bronze merge, and silver merge into a single orchestrated flow.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.jobs.run_registry import finish_run_item
from aats.data_platform.merge.bronze_merger import merge_candles_to_bronze, merge_funding_to_bronze
from aats.data_platform.merge.silver_merger import merge_candles_to_silver, merge_funding_to_silver
from aats.data_platform.models import candle_table_name, funding_table_name, instrument_type_for_symbol
from aats.data_platform.validate.candle_quality_checker import validate_candles
from aats.data_platform.validate.funding_quality_checker import validate_funding

log = logging.getLogger(__name__)


def run_candle_merge_pipeline(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    ingest_run_id: str,
    dataset_version: str = "v1.0",
    run_item_id: str | None = None,
) -> dict[str, Any]:
    """Validate staging, merge to bronze, then merge to silver."""
    inst_type = instrument_type_for_symbol(symbol)
    stg_table = candle_table_name("staging", symbol, timeframe)

    # 1. Validate staging
    quality = validate_candles(
        session,
        table=stg_table,
        ingest_run_id=ingest_run_id,
        symbol=symbol.upper(),
        timeframe=timeframe,
        dataset_version=dataset_version,
        dataset_layer="staging",
        instrument_type=inst_type,
    )
    log.info("Candle quality: %s (%d rows)", quality["quality_status"], quality["total_rows"])

    # 2. Merge staging -> bronze
    bronze_count = merge_candles_to_bronze(
        session, symbol=symbol, timeframe=timeframe, ingest_run_id=ingest_run_id,
    )

    # 3. Merge bronze -> silver
    silver_count = merge_candles_to_silver(
        session, symbol=symbol, timeframe=timeframe, ingest_run_id=ingest_run_id,
    )

    if run_item_id:
        finish_run_item(
            session, run_item_id,
            rows_written_bronze=bronze_count,
            rows_written_silver=silver_count,
        )

    return dict(
        quality=quality,
        bronze_count=bronze_count,
        silver_count=silver_count,
    )


def run_funding_merge_pipeline(
    session: Session,
    *,
    symbol: str,
    ingest_run_id: str,
    dataset_version: str = "v1.0",
    run_item_id: str | None = None,
) -> dict[str, Any]:
    """Validate staging funding, merge to bronze, then merge to silver."""
    stg_table = funding_table_name("staging")

    quality = validate_funding(
        session,
        table=stg_table,
        ingest_run_id=ingest_run_id,
        symbol=symbol.upper(),
        dataset_version=dataset_version,
        dataset_layer="staging",
        instrument_type="swap",
    )
    log.info("Funding quality: %s (%d rows)", quality["quality_status"], quality["total_rows"])

    bronze_count = merge_funding_to_bronze(
        session, symbol=symbol, ingest_run_id=ingest_run_id,
    )
    silver_count = merge_funding_to_silver(
        session, symbol=symbol, ingest_run_id=ingest_run_id,
    )

    if run_item_id:
        finish_run_item(
            session, run_item_id,
            rows_written_bronze=bronze_count,
            rows_written_silver=silver_count,
        )

    return dict(
        quality=quality,
        bronze_count=bronze_count,
        silver_count=silver_count,
    )
