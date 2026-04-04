#!/usr/bin/env python3
"""Phase 1 Minimum Acceptance Test — 5 end-to-end cases.

Case 1: Spot candles   historical -> staging -> bronze -> silver
Case 2: Swap candles   historical -> staging -> bronze -> silver
Case 3: Swap funding   historical -> staging -> bronze -> silver
Case 4: Rolling candles API -> staging -> bronze -> silver + checkpoint
Case 5: Gold swap replay bars  silver candles + funding -> gold
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("phase1_acceptance")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DATA = PROJECT_ROOT / "aats" / "data_platform" / "test_data"

# ── helpers ──────────────────────────────────────────────────────────────────

def _count(session, table: str, where: str = "1=1", params: dict | None = None) -> int:
    from sqlalchemy import text
    r = session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
    return r.scalar()


def _sample(session, table: str, limit: int = 3, where: str = "1=1", params: dict | None = None):
    from sqlalchemy import text
    return session.execute(
        text(f"SELECT * FROM {table} WHERE {where} ORDER BY ts LIMIT :lim"),
        {**(params or {}), "lim": limit},
    ).fetchall()


class Result:
    def __init__(self, case: str):
        self.case = case
        self.ok = False
        self.run_id: str | None = None
        self.item_id: str | None = None
        self.staging_rows = 0
        self.bronze_rows = 0
        self.silver_rows = 0
        self.gold_rows = 0
        self.quality_status: str | None = None
        self.checkpoint: dict | None = None
        self.sample_rows: list = []
        self.error: str | None = None

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [
            f"  {status}  {self.case}",
            f"    run_id          = {self.run_id}",
            f"    staging_rows    = {self.staging_rows}",
            f"    bronze_rows     = {self.bronze_rows}",
            f"    silver_rows     = {self.silver_rows}",
        ]
        if self.gold_rows:
            lines.append(f"    gold_rows       = {self.gold_rows}")
        lines.append(f"    quality_status  = {self.quality_status}")
        if self.checkpoint:
            lines.append(f"    checkpoint      = {json.dumps({k: str(v) for k, v in self.checkpoint.items()}, ensure_ascii=False)}")
        if self.sample_rows:
            lines.append("    sample (first 3):")
            for r in self.sample_rows[:3]:
                lines.append(f"      {r}")
        if self.error:
            lines.append(f"    ERROR: {self.error}")
        return "\n".join(lines)


# ── Case 1: Spot candles backfill ────────────────────────────────────────────

def case_1_spot_candles(settings) -> Result:
    res = Result("Case 1: Spot candles BTC-USDT backfill -> Silver")
    from sqlalchemy import text
    from aats.data_platform.collectors.backfill.candles_backfill_collector import collect_backfill_candle_file
    from aats.data_platform.collectors.backfill.file_discovery import register_source_file
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import run_candle_merge_pipeline

    zip_path = str(TEST_DATA / "BTC-USDT-candlesticks-2026-04-01.zip")
    symbol, timeframe = "BTC-USDT", "1m"

    with get_session(settings) as session:
        fid = register_source_file(
            session,
            source_path=zip_path,
            dataset_domain="candles",
            symbol_hint=symbol,
            timeframe_hint=timeframe,
            source_granularity="day",
        )
        if not fid:
            # 已注册过，查出 id
            fid = session.execute(
                text("SELECT source_file_id FROM meta.raw_source_files WHERE source_path = :p"),
                dict(p=zip_path),
            ).scalar()

    with get_session(settings) as session:
        run_id = collect_backfill_candle_file(
            session,
            source_file_id=fid,
            zip_path=zip_path,
            symbol_hint=symbol,
            timeframe=timeframe,
        )
        res.run_id = run_id
        res.staging_rows = _count(session, "staging.market_spot_candles_1m",
                                  "ingest_run_id = :rid", {"rid": run_id})

    with get_session(settings) as session:
        result = run_candle_merge_pipeline(
            session, symbol=symbol, timeframe=timeframe, ingest_run_id=run_id,
        )
        res.quality_status = result["quality"]["quality_status"]
        res.bronze_rows = result["bronze_count"]
        res.silver_rows = result["silver_count"]
        res.sample_rows = _sample(session, "silver.market_spot_candles_1m",
                                  where="symbol = :sym", params={"sym": symbol})

    res.ok = res.silver_rows > 0 and res.quality_status in ("pass", "warn")
    return res


# ── Case 2: Swap candles backfill ────────────────────────────────────────────

def case_2_swap_candles(settings) -> Result:
    res = Result("Case 2: Swap candles BTC-USDT-SWAP backfill -> Silver")
    from sqlalchemy import text
    from aats.data_platform.collectors.backfill.candles_backfill_collector import collect_backfill_candle_file
    from aats.data_platform.collectors.backfill.file_discovery import register_source_file
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import run_candle_merge_pipeline

    zip_path = str(TEST_DATA / "BTC-USDT-SWAP-candlesticks-2026-04-01.zip")
    symbol, timeframe = "BTC-USDT-SWAP", "1m"

    with get_session(settings) as session:
        fid = register_source_file(
            session,
            source_path=zip_path,
            dataset_domain="candles",
            symbol_hint=symbol,
            timeframe_hint=timeframe,
            source_granularity="day",
        )
        if not fid:
            fid = session.execute(
                text("SELECT source_file_id FROM meta.raw_source_files WHERE source_path = :p"),
                dict(p=zip_path),
            ).scalar()

    with get_session(settings) as session:
        run_id = collect_backfill_candle_file(
            session,
            source_file_id=fid,
            zip_path=zip_path,
            symbol_hint=symbol,
            timeframe=timeframe,
        )
        res.run_id = run_id
        res.staging_rows = _count(session, "staging.market_swap_candles_1m",
                                  "ingest_run_id = :rid", {"rid": run_id})

    with get_session(settings) as session:
        result = run_candle_merge_pipeline(
            session, symbol=symbol, timeframe=timeframe, ingest_run_id=run_id,
        )
        res.quality_status = result["quality"]["quality_status"]
        res.bronze_rows = result["bronze_count"]
        res.silver_rows = result["silver_count"]
        res.sample_rows = _sample(session, "silver.market_swap_candles_1m",
                                  where="symbol = :sym", params={"sym": symbol})

    res.ok = res.silver_rows > 0 and res.quality_status in ("pass", "warn")
    return res


# ── Case 3: Swap funding backfill ────────────────────────────────────────────

def case_3_swap_funding(settings) -> Result:
    res = Result("Case 3: Swap funding BTC-USDT-SWAP backfill -> Silver")
    from sqlalchemy import text
    from aats.data_platform.collectors.backfill.funding_backfill_collector import collect_backfill_funding_file
    from aats.data_platform.collectors.backfill.file_discovery import register_source_file
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import run_funding_merge_pipeline

    zip_path = str(TEST_DATA / "BTC-USDT-SWAP-fundingrates-2026-03.zip")
    symbol = "BTC-USDT-SWAP"

    with get_session(settings) as session:
        fid = register_source_file(
            session,
            source_path=zip_path,
            dataset_domain="funding",
            symbol_hint=symbol,
            timeframe_hint=None,
            source_granularity="month",
        )
        if not fid:
            fid = session.execute(
                text("SELECT source_file_id FROM meta.raw_source_files WHERE source_path = :p"),
                dict(p=zip_path),
            ).scalar()

    with get_session(settings) as session:
        run_id = collect_backfill_funding_file(
            session,
            source_file_id=fid,
            zip_path=zip_path,
            symbol_hint=symbol,
        )
        res.run_id = run_id
        res.staging_rows = _count(session, "staging.market_swap_funding",
                                  "ingest_run_id = :rid", {"rid": run_id})

    with get_session(settings) as session:
        result = run_funding_merge_pipeline(
            session, symbol=symbol, ingest_run_id=run_id,
        )
        res.quality_status = result["quality"]["quality_status"]
        res.bronze_rows = result["bronze_count"]
        res.silver_rows = result["silver_count"]
        res.sample_rows = _sample(session, "silver.market_swap_funding",
                                  where="symbol = :sym", params={"sym": symbol})

    res.ok = res.silver_rows > 0 and res.quality_status in ("pass", "warn")
    return res


# ── Case 4: Rolling candles (simulated — no live API needed) ─────────────────
# We simulate rolling by using the same backfill data but through the
# incremental checkpoint path, proving checkpoint read / write / advance.

def case_4_rolling_candles_checkpoint(settings) -> Result:
    res = Result("Case 4: Rolling candles checkpoint flow (simulated)")
    from sqlalchemy import text
    from aats.data_platform.collectors.backfill.candles_backfill_collector import collect_backfill_candle_file
    from aats.data_platform.collectors.backfill.file_discovery import register_source_file
    from aats.data_platform.db import get_session
    from aats.data_platform.jobs.checkpoint_manager import get_checkpoint, upsert_checkpoint
    from aats.data_platform.merge.merge_pipeline import run_candle_merge_pipeline

    # Use ETH spot as the rolling test target
    zip_path = str(TEST_DATA / "ETH-USDT-candlesticks-2026-04-01.zip")
    symbol, timeframe = "ETH-USDT", "1m"

    # Step 1: Register and ingest
    with get_session(settings) as session:
        fid = register_source_file(
            session,
            source_path=zip_path,
            dataset_domain="candles",
            symbol_hint=symbol,
            timeframe_hint=timeframe,
            source_granularity="day",
        )
        if not fid:
            fid = session.execute(
                text("SELECT source_file_id FROM meta.raw_source_files WHERE source_path = :p"),
                dict(p=zip_path),
            ).scalar()

    with get_session(settings) as session:
        run_id = collect_backfill_candle_file(
            session,
            source_file_id=fid,
            zip_path=zip_path,
            symbol_hint=symbol,
            timeframe=timeframe,
        )
        res.run_id = run_id
        res.staging_rows = _count(session, "staging.market_spot_candles_1m",
                                  "ingest_run_id = :rid AND symbol = :sym",
                                  {"rid": run_id, "sym": symbol})

    # Step 2: Merge
    with get_session(settings) as session:
        result = run_candle_merge_pipeline(
            session, symbol=symbol, timeframe=timeframe, ingest_run_id=run_id,
        )
        res.quality_status = result["quality"]["quality_status"]
        res.bronze_rows = result["bronze_count"]
        res.silver_rows = result["silver_count"]

    # Step 3: Simulate checkpoint advance (as rolling collector would)
    with get_session(settings) as session:
        # Find max ts in silver
        max_ts = session.execute(
            text("SELECT max(ts) FROM silver.market_spot_candles_1m WHERE symbol = :sym"),
            {"sym": symbol},
        ).scalar()

        if max_ts:
            upsert_checkpoint(
                session,
                dataset_domain="candles",
                instrument_type="spot",
                symbol=symbol,
                timeframe=timeframe,
                last_successful_ts=max_ts,
                next_expected_ts=max_ts + timedelta(minutes=1),
                last_ingest_run_id=run_id,
            )

        cp = get_checkpoint(
            session,
            dataset_domain="candles",
            instrument_type="spot",
            symbol=symbol,
            timeframe=timeframe,
        )
        res.checkpoint = {
            "last_successful_ts": cp["last_successful_ts"] if cp else None,
            "next_expected_ts": cp["next_expected_ts"] if cp else None,
            "checkpoint_status": cp["checkpoint_status"] if cp else None,
        }
        res.sample_rows = _sample(session, "silver.market_spot_candles_1m",
                                  where="symbol = :sym", params={"sym": symbol})

    res.ok = (
        res.silver_rows > 0
        and res.checkpoint is not None
        and res.checkpoint.get("last_successful_ts") is not None
    )
    return res


# ── Case 5: Gold swap replay bars ───────────────────────────────────────────

def case_5_gold_replay_bars(settings) -> Result:
    res = Result("Case 5: Gold swap replay bars BTC-USDT-SWAP")
    from sqlalchemy import text
    from aats.data_platform.db import get_session
    from aats.data_platform.gold.replay_bar_builder import build_gold_replay_bars

    symbol, timeframe = "BTC-USDT-SWAP", "1m"

    with get_session(settings) as session:
        # Get time range from silver candles (written by Case 2)
        bounds = session.execute(
            text("SELECT min(ts), max(ts) FROM silver.market_swap_candles_1m WHERE symbol = :sym"),
            {"sym": symbol},
        ).fetchone()
        if not bounds or bounds[0] is None:
            res.error = "No Silver swap candles found — did Case 2 run?"
            return res

        window_start, window_end = bounds[0], bounds[1]

        run_id = build_gold_replay_bars(
            session,
            symbol=symbol,
            timeframe=timeframe,
            window_start=window_start,
            window_end=window_end,
        )
        res.run_id = run_id
        res.gold_rows = _count(session, "gold.market_swap_replay_bars_1m",
                               "symbol = :sym", {"sym": symbol})
        res.sample_rows = _sample(session, "gold.market_swap_replay_bars_1m",
                                  where="symbol = :sym", params={"sym": symbol})

        # Verify funding alignment is present
        funded = session.execute(
            text("""
                SELECT count(*) FROM gold.market_swap_replay_bars_1m
                WHERE symbol = :sym AND aligned_funding_rate IS NOT NULL
            """),
            {"sym": symbol},
        ).scalar()
        res.quality_status = f"gold_ok, {funded} bars with funding aligned"

    res.ok = res.gold_rows > 0
    return res


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import reset_engine

    reset_engine()
    settings = get_settings()

    cases = [
        case_1_spot_candles,
        case_2_swap_candles,
        case_3_swap_funding,
        case_4_rolling_candles_checkpoint,
        case_5_gold_replay_bars,
    ]

    results: list[Result] = []
    for fn in cases:
        log.info("=" * 60)
        log.info("Running: %s", fn.__name__)
        try:
            r = fn(settings)
        except Exception as exc:
            r = Result(fn.__name__)
            r.error = traceback.format_exc()
        results.append(r)

    # ── Summary ──
    print("\n" + "=" * 70)
    print("Phase 1 Acceptance Test Results")
    print("=" * 70)
    for r in results:
        print(r.summary())
        print()

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print(f"Result: {passed}/{total} passed")
    if passed == total:
        print(">>> Phase 1 READY TO CLOSE <<<")
    else:
        print("!!! Phase 1 NOT ready -- see failures above")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
