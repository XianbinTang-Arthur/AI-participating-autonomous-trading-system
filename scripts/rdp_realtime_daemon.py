#!/usr/bin/env python3
"""Realtime data aggregation - thin compatibility shim.

HISTORY (2026-04-07)
-----------------------------------------------------------------
This script was originally a 60s-tick long-running daemon. It has
been retired and cleaned up:
  - daemon loop / cycle_count / max_iterations removed
  - internal _auto_build_gold_all / _auto_detect_gaps removed
    (now handled by daily_ingest)
  - no longer depends on aats/data_platform/jobs/scheduler.py
    (that file has been deleted)

This shim is kept only to preserve operator muscle memory and
existing systemd unit / Task Scheduler entries. All invocations
are forwarded to scripts/rdp_run_daily_ingest.py via subprocess.
-----------------------------------------------------------------

Recommended usage (call the replacement directly):
    python scripts/rdp_run_daily_ingest.py

Compatible usage (forwarded to daily_ingest):
    python scripts/rdp_realtime_daemon.py            # equivalent
    python scripts/rdp_realtime_daemon.py --once     # same
    python scripts/rdp_realtime_daemon.py --dry-run  # forwards --dry-run

See docs/operations/rdp_scheduling_strategy.md
"""

from __future__ import annotations

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


def _utf8_subprocess_env() -> dict[str, str]:
    """Return an env dict that forces UTF-8 stdout/stderr in child Python.

    Required because each subprocess starts its own Python interpreter
    and rechecks the locale; without this, Windows children fall back
    to GBK and mangle Chinese paths/log messages.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_realtime")

_SCRIPT_DIR = Path(__file__).resolve().parent

# Flags accepted by the legacy daemon but not by daily_ingest. Both
# space-separated form (`--interval 30`) and equals form (`--interval=30`)
# are filtered out before forwarding.
_LEGACY_FLAG_NAMES = frozenset({"--once", "--interval", "--max-iterations"})
_LEGACY_FLAGS_WITH_VALUE = frozenset({"--interval", "--max-iterations"})


def _filter_legacy_flags(argv: list[str]) -> list[str]:
    """Strip legacy daemon-era flags from argv before forwarding."""
    forwarded: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        # Handle both `--interval 30` and `--interval=30` forms.
        base = arg.split("=", 1)[0]
        if base in _LEGACY_FLAG_NAMES:
            log.info("Ignored legacy flag: %s", arg)
            # Only consume the next token for the space-separated form
            # of flags that take a value.
            if base in _LEGACY_FLAGS_WITH_VALUE and "=" not in arg:
                skip_next = True
            continue
        forwarded.append(arg)
    return forwarded


def main() -> int:
    log.warning("=" * 70)
    log.warning("DEPRECATION: rdp_realtime_daemon.py 已退役为薄壳。")
    log.warning("请直接调用替代品:")
    log.warning("  python scripts/rdp_run_daily_ingest.py")
    log.warning("本次调用将转发到 daily_ingest。")
    log.warning("=" * 70)

    forwarded = _filter_legacy_flags(sys.argv[1:])

    daily_ingest_path = _SCRIPT_DIR / "rdp_run_daily_ingest.py"
    if not daily_ingest_path.exists():
        log.error("daily_ingest script not found: %s", daily_ingest_path)
        return 1

    cmd = [sys.executable, str(daily_ingest_path), *forwarded]
    log.info("Forwarding to: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(_SCRIPT_DIR.parent),
            env=_utf8_subprocess_env(),
        )
        return result.returncode
    except Exception:
        log.exception("Failed to invoke daily_ingest")
        return 1


if __name__ == "__main__":
    sys.exit(main())
