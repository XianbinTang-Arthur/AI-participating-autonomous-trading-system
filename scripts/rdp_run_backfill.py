#!/usr/bin/env python3
"""Run historical backfill for the Research Data Platform.

Discovers ZIP files in the configured download directory, parses them,
writes to staging, and runs the merge pipeline through to Silver.

Usage:
    python scripts/rdp_run_backfill.py [--dir /path/to/downloads]
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rdp_backfill")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run historical backfill")
    parser.add_argument("--dir", type=str, default=None, help="Override historical download directory")
    parser.add_argument("--timeframe", type=str, default=None,
                        help="Timeframe for candle files (e.g. 1m, 5m, 15m, 1H). "
                             "Required when OKX filenames/directories don't carry timeframe.")
    args = parser.parse_args()

    from sqlalchemy import text

    from aats.data_platform.collectors.backfill.candles_backfill_collector import (
        collect_backfill_candle_file,
        resolve_candle_timeframe,
    )
    from aats.data_platform.collectors.backfill.file_discovery import (
        discover_and_register,
        mark_source_file_status,
    )
    from aats.data_platform.collectors.backfill.funding_backfill_collector import collect_backfill_funding_file
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import get_session
    from aats.data_platform.merge.merge_pipeline import run_candle_merge_pipeline, run_funding_merge_pipeline

    settings = get_settings()
    download_dir = args.dir or settings.historical_download_dir

    # Step 1: Discover and register new files
    log.info("Discovering files in: %s", download_dir)
    with get_session(settings) as session:
        new_ids = discover_and_register(session, download_dir)
    log.info("Registered %d new files", len(new_ids))

    if not new_ids:
        log.info("No new files to process.")
        return

    # Step 2: Process each new file
    with get_session(settings) as session:
        for file_id in new_ids:
            row = session.execute(
                text("""
                    SELECT source_file_id, source_path, dataset_domain, symbol_hint,
                           timeframe_hint, ingested_status
                    FROM meta.raw_source_files
                    WHERE source_file_id = :fid
                """),
                dict(fid=file_id),
            ).mappings().first()

            if not row or row["ingested_status"] != "pending":
                continue

            domain = row["dataset_domain"]
            symbol = row["symbol_hint"]
            path = row["source_path"]

            log.info("Processing: %s (%s, %s)", path, domain, symbol)

            try:
                if domain == "candles":
                    tf = resolve_candle_timeframe(
                        cli_timeframe=args.timeframe,
                        timeframe_hint=row["timeframe_hint"],
                    )
                    if not tf:
                        reason = (
                            "Missing timeframe: OKX candle filenames do not carry "
                            "timeframe. Use --timeframe or organize files under "
                            "timeframe directories (1m/, 5m/, 15m/, 1H/)."
                        )
                        log.warning("Skipping candle file: %s — %s", path, reason)
                        mark_source_file_status(
                            session,
                            file_id,
                            ingested_status="skipped",
                            parse_error=reason,
                        )
                        session.commit()
                        continue
                    run_id = collect_backfill_candle_file(
                        session,
                        source_file_id=file_id,
                        zip_path=path,
                        symbol_hint=symbol,
                        timeframe=tf,
                    )
                    session.commit()
                    run_candle_merge_pipeline(
                        session,
                        symbol=symbol,
                        timeframe=tf,
                        ingest_run_id=run_id,
                    )
                elif domain == "funding":
                    run_id = collect_backfill_funding_file(
                        session,
                        source_file_id=file_id,
                        zip_path=path,
                        symbol_hint=symbol,
                    )
                    session.commit()
                    run_funding_merge_pipeline(
                        session,
                        symbol=symbol,
                        ingest_run_id=run_id,
                    )
                else:
                    log.warning("Skipping unknown domain: %s for %s", domain, path)
                    mark_source_file_status(
                        session,
                        file_id,
                        ingested_status="skipped",
                        parse_error=f"Unsupported dataset_domain: {domain}",
                    )
                    session.commit()
            except Exception:
                log.exception("Failed to process file: %s", path)
                try:
                    mark_source_file_status(
                        session,
                        file_id,
                        ingested_status="failed",
                        parse_error="Unhandled exception during backfill processing",
                    )
                    session.commit()
                except Exception:
                    log.exception("Could not update source file status for: %s", path)

    log.info("Backfill complete.")


if __name__ == "__main__":
    main()
