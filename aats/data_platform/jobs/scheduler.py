"""Minimal rolling scheduler for Phase 1.

Drives periodic candle and funding ingestion and merge cycles.

Cadence rules (from Phase 1 design freeze):
- 1m  candles: every 1 minute
- 5m  candles: every 5 minutes
- 15m candles: every 15 minutes
- 1H  candles: every 1 hour
- funding:     every 15 minutes
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from aats.data_platform.collectors.rolling.candles_api_collector import collect_candles_incremental
from aats.data_platform.collectors.rolling.funding_api_collector import collect_funding_incremental
from aats.data_platform.config import ResearchPlatformSettings, get_settings
from aats.data_platform.db import get_session
from aats.data_platform.merge.merge_pipeline import (
    ValidationBlockedError,
    run_candle_merge_pipeline,
    run_funding_merge_pipeline,
)
from aats.data_platform.models import SUPPORTED_SYMBOLS, FUNDING_SYMBOLS

log = logging.getLogger(__name__)

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1H": 3600,
}

_FUNDING_CADENCE_SECONDS = 900  # 15 minutes

# ---------------------------------------------------------------------------
# Bucket-based cadence dedup (single-process, in-memory)
# ---------------------------------------------------------------------------
# NOTE: This is a best-effort, single-process dedup mechanism.  The bucket
# state lives in process memory only.  Known limitations:
#   - After a process restart, the first tick may re-fire a bucket that
#     already ran before the restart.  This is acceptable because the
#     downstream merge pipeline is idempotent (upsert on PK).
#   - Multiple worker processes would each maintain independent state,
#     potentially causing duplicate fires.  Phase 1 runs a single
#     scheduler process, so this is not a current concern.
# If persistent dedup is needed later, store the last-fired bucket in
# meta.ingest_checkpoints.notes or a dedicated meta.scheduler_state table.
_last_candle_bucket: dict[tuple[str, str], int] = {}
_last_funding_bucket: dict[str, int] = {}


def _bucket_for_timeframe(now_utc: datetime, timeframe: str) -> int:
    """Compute the cadence bucket index for a given timeframe.

    Each bucket represents one cadence period.  E.g. for 5m, bucket 0 covers
    epoch [0, 300), bucket 1 covers [300, 600), etc.
    """
    seconds = _TF_SECONDS[timeframe]
    return int(now_utc.timestamp()) // seconds


def _bucket_for_funding(now_utc: datetime) -> int:
    """Compute the 15-minute cadence bucket for funding collection."""
    return int(now_utc.timestamp()) // _FUNDING_CADENCE_SECONDS


def _is_on_cadence_boundary(now_utc: datetime, cadence_seconds: int) -> bool:
    """Return True if *now_utc* falls within the first 60 seconds of a cadence window."""
    return (int(now_utc.timestamp()) % cadence_seconds) < 60


def _should_fire_candle(now_utc: datetime, symbol: str, timeframe: str) -> bool:
    """Return True if this (symbol, timeframe) should fire now.

    Checks two conditions:
      1. Current time is within 60s of a cadence boundary for *timeframe*.
      2. This cadence bucket has not already been fired (in-memory dedup).
    """
    seconds = _TF_SECONDS.get(timeframe)
    if seconds is None:
        return True  # unknown timeframe — always eligible
    if not _is_on_cadence_boundary(now_utc, seconds):
        return False
    bucket = _bucket_for_timeframe(now_utc, timeframe)
    key = (symbol, timeframe)
    if _last_candle_bucket.get(key) == bucket:
        return False
    _last_candle_bucket[key] = bucket
    return True


def _should_fire_funding(now_utc: datetime, symbol: str) -> bool:
    """Return True if this symbol's funding should fire now.

    Checks 15-min cadence boundary AND ensures the same bucket is not
    fired twice (in-memory dedup — see module-level note on limitations).
    """
    if not _is_on_cadence_boundary(now_utc, _FUNDING_CADENCE_SECONDS):
        return False
    bucket = _bucket_for_funding(now_utc)
    if _last_funding_bucket.get(symbol) == bucket:
        return False
    _last_funding_bucket[symbol] = bucket
    return True


def run_one_rolling_cycle(settings: ResearchPlatformSettings | None = None) -> None:
    """Execute a single rolling ingestion + merge cycle.

    Only runs each timeframe if the current UTC time is on its cadence boundary.
    """
    settings = settings or get_settings()
    now_utc = datetime.now(timezone.utc)

    # Candles — only fire timeframes on cadence, with bucket dedup
    if settings.rolling_candles_enabled:
        for symbol in settings.rolling_candles_symbols:
            for tf in settings.rolling_candles_timeframes:
                if not _should_fire_candle(now_utc, symbol, tf):
                    continue
                try:
                    with get_session(settings) as session:
                        run_id = collect_candles_incremental(
                            session, settings, symbol=symbol, timeframe=tf,
                        )
                    with get_session(settings) as session:
                        run_candle_merge_pipeline(
                            session, symbol=symbol, timeframe=tf, ingest_run_id=run_id,
                        )
                except ValidationBlockedError:
                    log.warning("Candle merge blocked by quality gate: %s %s", symbol, tf)
                except Exception:
                    log.exception("Rolling candle failed: %s %s", symbol, tf)

    # Funding — every 15 minutes, with bucket dedup
    if settings.rolling_funding_enabled:
        for symbol in settings.rolling_funding_symbols:
            if not _should_fire_funding(now_utc, symbol):
                continue
            try:
                with get_session(settings) as session:
                    run_id = collect_funding_incremental(
                        session, settings, symbol=symbol,
                    )
                with get_session(settings) as session:
                    run_funding_merge_pipeline(
                        session, symbol=symbol, ingest_run_id=run_id,
                    )
            except ValidationBlockedError:
                log.warning("Funding merge blocked by quality gate: %s", symbol)
            except Exception:
                log.exception("Rolling funding failed: %s", symbol)


def run_scheduler_loop(
    settings: ResearchPlatformSettings | None = None,
    interval_seconds: int = 60,
    max_iterations: int | None = None,
) -> None:
    """Run the rolling scheduler as a long-lived loop.

    The loop ticks every *interval_seconds* (default 60s).  Each tick,
    ``run_one_rolling_cycle`` checks cadence boundaries and only fires
    the timeframes that are due.

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
