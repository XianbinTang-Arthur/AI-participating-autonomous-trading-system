#!/usr/bin/env python3
"""OKX microstructure WebSocket daemon — long-running data-lake ingest.

Runs :class:`aats.data_platform.collectors.microstructure_ws_collector.MicrostructureCollector`
forever, handling SIGTERM / SIGINT for graceful shutdown and touching
``/tmp/aats_microstructure_heartbeat`` periodically so docker-compose
healthcheck can distinguish "process alive + consuming" from "process
hanging / crashed".

Phase 1A scope: BTC-USDT-SWAP single-symbol, six OKX public channels
(trades-all / bbo-tbt / books5 / open-interest / funding-rate / mark-price).

Usage::

    python scripts/microstructure_ws_daemon.py                       # default
    python scripts/microstructure_ws_daemon.py --symbol BTC-USDT-SWAP

The daemon owns no state beyond the in-memory flush buffers; the WS client
and DB pool handle reconnection themselves. The collector creates exactly
one ``meta.ingest_runs`` row per process lifetime (rolling workflow).

Design alignment: docs/design/p1d_phase1a_implementation_design_2026_04_20.md
§9 W1 Day 3 + appendix A (path list) + appendix B (daemon template anchor).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("microstructure_ws_daemon")

_HEARTBEAT_PATH = Path("/tmp/aats_microstructure_heartbeat")
_HEARTBEAT_INTERVAL_SECONDS = 10.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OKX microstructure WebSocket daemon")
    p.add_argument(
        "--symbol",
        dest="symbols",
        action="append",
        default=None,
        help="OKX instId to subscribe (repeatable). Default: BTC-USDT-SWAP",
    )
    # Buffer tuning knobs — defaults align with collector module constants
    # (§6.6 of the implementation design). Exposed here so operators can
    # adjust flush cadence without editing Python.
    p.add_argument("--flush-trades-max-rows", type=int, default=500)
    p.add_argument("--flush-trades-max-seconds", type=float, default=3.0)
    p.add_argument("--flush-bbo-max-rows", type=int, default=100)
    p.add_argument("--flush-bbo-max-seconds", type=float, default=5.0)
    p.add_argument("--flush-books5-max-rows", type=int, default=200)
    p.add_argument("--flush-books5-max-seconds", type=float, default=2.0)
    p.add_argument("--flush-oif-max-rows", type=int, default=100)
    p.add_argument("--flush-oif-max-seconds", type=float, default=3.0)
    p.add_argument(
        "--bbo-min-interval-seconds",
        type=float,
        default=1.0,
        help="Client-side BBO sample throttle (default 1.0s per symbol, per §appendix E #5)",
    )
    p.add_argument(
        "--books5-min-interval-seconds",
        type=float,
        default=0.5,
        help="Client-side books5 sample throttle (default 0.5s per symbol)",
    )
    return p.parse_args()


async def _heartbeat_loop(stop_event: asyncio.Event, collector) -> None:
    while not stop_event.is_set():
        try:
            status = collector.status()
            buffered = status.get("buffered", {})
            written = status.get("written_counts", {})
            payload = (
                f"connected={status['connected']} "
                f"ingest_run_id={status.get('ingest_run_id')} "
                f"symbols={','.join(status.get('symbols', []))} "
                f"buffered_trades={buffered.get('trades', 0)} "
                f"buffered_bbo={buffered.get('bbo', 0)} "
                f"buffered_books5={buffered.get('books5', 0)} "
                f"buffered_oif={buffered.get('oi_funding_mark', 0)} "
                f"written_trades={written.get('bronze.market_trades', 0)} "
                f"written_bbo={written.get('bronze.market_orderbook_bbo', 0)} "
                f"written_books5={written.get('bronze.market_orderbook_books5', 0)} "
                f"written_oif={written.get('staging.market_oi_funding_ticks', 0)} "
                f"last_error={status['last_error']}"
            )
            _HEARTBEAT_PATH.write_text(payload, encoding="utf-8")
        except OSError:
            # Heartbeat file is advisory; a write failure should not kill the
            # ingest loop. Next tick will try again.
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue


async def amain(args: argparse.Namespace) -> int:
    # Deferred imports so --help works even when aats config is not ready.
    from aats.bootstrap.settings import AATSSettings
    from aats.data_platform.collectors.microstructure_ws_collector import (
        MicrostructureCollector,
    )

    # AATSSettings.model_validate({}) bypasses BaseSettings' env-loading path
    # entirely — only the class-level defaults (okx_public_ws_url, reconnect/
    # keepalive tunings) are used. The daemon is pure data-lake ingest, not
    # part of the 4-process runtime topology, so it deliberately sidesteps
    # managed-profile resolution, process_role validation, and the derivatives
    # cross-field checks those paths enforce. DB connectivity goes through
    # aats.data_platform.db → ResearchPlatformSettings, which reads its own
    # RDP_DATABASE_URL / AATS_ACTIVE_PARAMETER_DB_URL env vars independently.
    settings = AATSSettings.model_validate({})
    symbols = tuple(args.symbols) if args.symbols else ("BTC-USDT-SWAP",)
    collector = MicrostructureCollector(
        settings=settings,
        symbols=symbols,
        flush_trades_max_rows=args.flush_trades_max_rows,
        flush_trades_max_seconds=args.flush_trades_max_seconds,
        flush_bbo_max_rows=args.flush_bbo_max_rows,
        flush_bbo_max_seconds=args.flush_bbo_max_seconds,
        flush_books5_max_rows=args.flush_books5_max_rows,
        flush_books5_max_seconds=args.flush_books5_max_seconds,
        flush_oif_max_rows=args.flush_oif_max_rows,
        flush_oif_max_seconds=args.flush_oif_max_seconds,
        bbo_min_interval_seconds=args.bbo_min_interval_seconds,
        books5_min_interval_seconds=args.books5_min_interval_seconds,
    )

    stop_event = collector.client.stop_event
    loop = asyncio.get_running_loop()

    def _request_stop(signum: int) -> None:
        log.info("received signal %s, requesting stop", signum)
        stop_event.set()

    # add_signal_handler is Linux-only. The daemon runs in Docker, so that's
    # the only path that matters. On Windows dev Ctrl-C still works via the
    # default SIGINT → KeyboardInterrupt path caught in main().
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop, int(sig))

    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event, collector))
    log.info("starting microstructure_ws_daemon (symbols=%s)", list(symbols))
    try:
        await collector.run_forever()
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
    log.info("microstructure_ws_daemon exited cleanly")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.info("interrupted by KeyboardInterrupt")
        return 0


if __name__ == "__main__":
    sys.exit(main())
