"""Minimal rolling scheduler for Phase 1.

Drives periodic candle and funding ingestion and merge cycles.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from aats.data_platform.collectors.rolling.candles_api_collector import collect_candles_incremental
from aats.data_platform.collectors.rolling.funding_api_collector import collect_funding_incremental
from aats.data_platform.config import ResearchPlatformSettings, get_settings
from aats.data_platform.db import get_session
from aats.data_platform.merge.merge_pipeline import run_candle_merge_pipeline, run_funding_merge_pipeline
from aats.data_platform.models import SUPPORTED_SYMBOLS, FUNDING_SYMBOLS

log = logging.getLogger(__name__)

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1H": 3600,
}


def run_one_rolling_cycle(settings: ResearchPlatformSettings | None = None) -> None:
    """Execute a single rolling ingestion + merge cycle for all symbols/timeframes."""
    settings = settings or get_settings()

    # Candles
    if settings.rolling_candles_enabled:
        for symbol in settings.rolling_candles_symbols:
            for tf in settings.rolling_candles_timeframes:
                try:
                    with get_session(settings) as session:
                        run_id = collect_candles_incremental(
                            session, settings, symbol=symbol, timeframe=tf,
                        )
                    with get_session(settings) as session:
                        run_candle_merge_pipeline(
                            session, symbol=symbol, timeframe=tf, ingest_run_id=run_id,
                        )
                except Exception:
                    log.exception("Rolling candle failed: %s %s", symbol, tf)

    # Funding
    if settings.rolling_funding_enabled:
        for symbol in settings.rolling_funding_symbols:
            try:
                with get_session(settings) as session:
                    run_id = collect_funding_incremental(
                        session, settings, symbol=symbol,
                    )
                with get_session(settings) as session:
                    run_funding_merge_pipeline(
                        session, symbol=symbol, ingest_run_id=run_id,
                    )
            except Exception:
                log.exception("Rolling funding failed: %s", symbol)


def run_scheduler_loop(
    settings: ResearchPlatformSettings | None = None,
    interval_seconds: int = 60,
    max_iterations: int | None = None,
) -> None:
    """Run the rolling scheduler as a long-lived loop.

    Args:
        interval_seconds: Sleep between cycles.
        max_iterations: If set, exit after N iterations (for testing).
    """
    settings = settings or get_settings()
    iteration = 0
    log.info("Scheduler starting, interval=%ds", interval_seconds)

    while True:
        try:
            run_one_rolling_cycle(settings)
        except Exception:
            log.exception("Scheduler cycle error")

        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            log.info("Scheduler reached max iterations (%d), exiting", max_iterations)
            break

        time.sleep(interval_seconds)
