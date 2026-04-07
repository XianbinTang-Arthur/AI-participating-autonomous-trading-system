#!/usr/bin/env python3
"""Research Data Platform - thin compatibility shim.

HISTORY (2026-04-07)
-----------------------------------------------------------------
This script was originally a unified launcher that ran the
historical and realtime daemons concurrently in two threads in
the same process. The daemon mode has been retired and cleaned up:
  - multi-threaded daemon orchestration removed
  - importlib-based dynamic daemon loading removed
  - signal handling / Ctrl+C graceful shutdown removed

This shim is kept only to preserve operator muscle memory. All
invocations are forwarded via subprocess to:
  1. scripts/rdp_run_daily_ingest.py        (replaces realtime daemon)
  2. scripts/rdp_historical_daemon.py --once (scans incoming/ once)
-----------------------------------------------------------------

Recommended usage (call replacements directly):
    python scripts/rdp_run_daily_ingest.py
    python scripts/rdp_historical_daemon.py --once   # after dropping ZIPs

Or via cron / Task Scheduler:
    0 4 * * * python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance

Compatible usage (this shim runs both replacements in sequence):
    python scripts/rdp_start.py                  # daily_ingest + historical --once
    python scripts/rdp_start.py --realtime-only  # only daily_ingest
    python scripts/rdp_start.py --historical-only # only historical --once

See docs/operations/rdp_scheduling_strategy.md
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows so Chinese log messages render
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_start")

_SCRIPT_DIR = Path(__file__).resolve().parent


def _utf8_subprocess_env() -> dict[str, str]:
    """Return an env dict that forces UTF-8 stdout/stderr in child Python.

    Required because each subprocess starts its own Python interpreter
    and rechecks the locale; without this, Windows children fall back
    to GBK and mangle Chinese paths/log messages.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_subprocess(cmd: list[str], label: str) -> int:
    log.info("[%s] running: %s", label, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(_SCRIPT_DIR.parent),
            env=_utf8_subprocess_env(),
        )
        return result.returncode
    except Exception as exc:
        log.exception("[%s] FAILED: %s", label, exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RDP shim - forwards to daily_ingest + historical --once",
    )
    parser.add_argument(
        "--historical-only", action="store_true",
        help="only run historical_daemon --once (scans incoming/)",
    )
    parser.add_argument(
        "--realtime-only", action="store_true",
        help="only run daily_ingest (replaces realtime daemon)",
    )
    # legacy flags - accepted but ignored, prevents old callers from failing
    parser.add_argument("--historical-interval", type=int, default=None,
                        help="(legacy, ignored)")
    parser.add_argument("--realtime-interval", type=int, default=None,
                        help="(legacy, ignored)")
    args = parser.parse_args()

    log.warning("=" * 60)
    log.warning("  RDP shim - forwards to replacement scripts")
    log.warning("=" * 60)
    log.warning("  DEPRECATION: rdp_start.py 已退役为薄壳。")
    log.warning("  推荐改为直接调用替代品或 cron:")
    log.warning("    python scripts/rdp_run_daily_ingest.py")
    log.warning("    0 4 * * * python scripts/rdp_run_scheduled_workflow.py \\")
    log.warning("              --workflow data_maintenance")
    log.warning("=" * 60)

    run_realtime = not args.historical_only
    run_historical = not args.realtime_only

    overall_rc = 0

    if run_realtime:
        rc = _run_subprocess(
            [sys.executable, str(_SCRIPT_DIR / "rdp_run_daily_ingest.py")],
            "daily_ingest (replaces realtime daemon)",
        )
        # first-error-wins: keep the first nonzero exit code so operators
        # see a meaningful value instead of a bitwise-OR collision.
        if rc != 0 and overall_rc == 0:
            overall_rc = rc

    if run_historical:
        rc = _run_subprocess(
            [sys.executable, str(_SCRIPT_DIR / "rdp_historical_daemon.py"), "--once"],
            "historical_daemon --once",
        )
        if rc != 0 and overall_rc == 0:
            overall_rc = rc

    log.warning("=" * 60)
    log.warning("  RDP shim done | exit=%d", overall_rc)
    log.warning("=" * 60)
    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
