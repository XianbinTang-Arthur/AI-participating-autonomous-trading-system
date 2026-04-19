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

Observability (Stage 4)
=======================

On startup the daemon:
  1. Constructs a local :class:`MetricsRegistry` and passes it into the
     collector (so ``microstructure_*`` counters get populated).
  2. Initialises :func:`configure_telemetry` to spin up the OTel
     ``PrometheusMetricReader`` on **port 9465** (9464 is used by the 4
     AATS app processes — we deliberately pick a neighbouring port so
     Prometheus can scrape this container independently).
  3. Starts :func:`start_metrics_bridge_loop` which flushes registry
     increments into OTel counters every 30 s (aligned with Prometheus
     scrape_interval).

If opentelemetry SDK is missing (e.g. thin test image) the bridge
silently degrades to a no-op; the daemon still runs.

Design alignment: docs/design/p1d_phase1a_implementation_design_2026_04_20.md
§4 (monitoring framework) + §9 W1 Day 3 + appendix A (path list) + appendix B.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
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

# ─────────────────────────────────────────────────────────────────────
# Prometheus metrics reader port.
#
# The 4 AATS application processes (gateway / market / decision / execution)
# all bind PrometheusMetricReader on :9464. This daemon runs in its own
# container, so it picks 9465 to avoid collision and gives Prometheus a
# distinct scrape target identifier. The port is configurable via the
# OTEL_EXPORTER_PROMETHEUS_PORT env var (standard OTel convention).
# ─────────────────────────────────────────────────────────────────────
_PROMETHEUS_PORT_DEFAULT = "9465"


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


def _setup_prometheus_metrics():
    """Bootstrap MetricsRegistry + OTel Meter + PrometheusMetricReader.

    Returns ``(registry, bridge_task_factory)`` where ``registry`` is the
    :class:`MetricsRegistry` to pass into the collector, and
    ``bridge_task_factory()`` is a callable returning an asyncio Task that
    flushes registry → OTel counters every 30 s. If OTel is unavailable or
    port binding fails the function logs a warning and returns the registry
    anyway — the collector still increments counters, they just won't reach
    Prometheus.
    """
    from aats.bootstrap.metrics import MetricsRegistry
    from aats.bootstrap.metrics_bridge import start_metrics_bridge_loop
    from aats.bootstrap.telemetry import TelemetryConfig, configure_telemetry

    registry = MetricsRegistry()

    # OTel exporter prometheus reads OTEL_EXPORTER_PROMETHEUS_HOST/PORT env
    # vars. Default HOST is '0.0.0.0' which makes the /metrics endpoint
    # reachable from Prometheus inside the docker network.
    os.environ.setdefault("OTEL_EXPORTER_PROMETHEUS_HOST", "0.0.0.0")
    os.environ.setdefault("OTEL_EXPORTER_PROMETHEUS_PORT", _PROMETHEUS_PORT_DEFAULT)

    telemetry_config = TelemetryConfig.from_env(
        service_name="aats-microstructure-collector",
        process_role="microstructure-collector",
    )
    try:
        ok = configure_telemetry(telemetry_config)
        if ok:
            log.info(
                "prometheus metrics endpoint initialised on port %s",
                os.environ["OTEL_EXPORTER_PROMETHEUS_PORT"],
            )
        else:
            log.warning(
                "OTel not available; microstructure metrics will not reach "
                "Prometheus (MetricsRegistry still populates locally)",
            )
    except Exception as exc:  # pragma: no cover - defensive, keep daemon alive
        log.warning("telemetry configuration failed: %s (metrics disabled)", exc)

    def bridge_task_factory():
        return asyncio.create_task(start_metrics_bridge_loop(registry))

    return registry, bridge_task_factory


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

    # Stage 4: wire metrics registry → OTel → Prometheus :9465.
    registry, bridge_task_factory = _setup_prometheus_metrics()

    collector = MicrostructureCollector(
        settings=settings,
        symbols=symbols,
        metrics_registry=registry,
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
    bridge_task = bridge_task_factory()
    log.info("starting microstructure_ws_daemon (symbols=%s)", list(symbols))
    try:
        await collector.run_forever()
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        bridge_task.cancel()
        for task in (heartbeat_task, bridge_task):
            try:
                await task
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
