#!/usr/bin/env python3
"""OKX liquidation-orders WebSocket daemon — long-running data-lake ingest.

Runs :class:`aats.data_platform.collectors.liquidations_ws_collector.LiquidationsCollector`
forever, handling SIGTERM / SIGINT for graceful shutdown and touching
``/tmp/aats_liquidations_heartbeat`` periodically so docker-compose healthcheck
can distinguish "process alive + consuming" from "process hanging / crashed".

Usage::

    python scripts/liquidations_ws_daemon.py                 # default: SWAP
    python scripts/liquidations_ws_daemon.py --inst-type SWAP --inst-type FUTURES

The daemon owns no state beyond the in-memory flush buffer; the WS client and
DB pool handle reconnection themselves.
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
log = logging.getLogger("liquidations_ws_daemon")

_HEARTBEAT_PATH = Path("/tmp/aats_liquidations_heartbeat")
_HEARTBEAT_INTERVAL_SECONDS = 10.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OKX liquidation-orders WebSocket daemon")
    p.add_argument(
        "--inst-type",
        dest="inst_types",
        action="append",
        default=None,
        help="OKX instType to subscribe (repeatable). Default: SWAP",
    )
    p.add_argument(
        "--flush-max-rows",
        type=int,
        default=100,
        help="Flush buffer once it hits this many rows (default 100)",
    )
    p.add_argument(
        "--flush-max-seconds",
        type=float,
        default=5.0,
        help="Flush buffer at least this often (default 5.0s)",
    )
    return p.parse_args()


async def _heartbeat_loop(stop_event: asyncio.Event, collector) -> None:
    while not stop_event.is_set():
        try:
            status = collector.status()
            payload = (
                f"connected={status['connected']} "
                f"today_count={status['today_count']} "
                f"buffered={status['buffered_rows']} "
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
    from aats.data_platform.collectors.liquidations_ws_collector import (
        LiquidationsCollector,
    )

    # AATSSettings.model_validate({}) bypasses BaseSettings' env-loading path
    # entirely — only the class-level defaults (okx_public_ws_url, reconnect/
    # keepalive tunings) are used. The daemon is pure data-lake ingest, not
    # part of the 4-process runtime topology, so it deliberately sidesteps
    # managed-profile resolution, process_role validation, and the derivatives
    # cross-field checks that those paths enforce. DB connectivity goes through
    # aats.data_platform.db → ResearchPlatformSettings, which reads its own
    # RDP_DATABASE_URL / AATS_ACTIVE_PARAMETER_DB_URL env vars independently.
    settings = AATSSettings.model_validate({})
    inst_types = tuple(args.inst_types) if args.inst_types else ("SWAP",)
    collector = LiquidationsCollector(
        settings=settings,
        inst_types=inst_types,
        flush_max_rows=args.flush_max_rows,
        flush_max_seconds=args.flush_max_seconds,
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
    log.info("starting liquidations_ws_daemon (inst_types=%s)", list(inst_types))
    try:
        await collector.run_forever()
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
    log.info("liquidations_ws_daemon exited cleanly")
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
