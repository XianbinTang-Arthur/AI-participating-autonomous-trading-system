#!/usr/bin/env python3
"""Write a fresh, read-only heartbeat packet for derivatives public collectors."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance._atomic_io import immutable_json_write  # noqa: E402
from scripts.write_deployment_evidence import (  # noqa: E402
    _collector_heartbeat_fact,
    _container_fact,
    _run_command,
)


_COLLECTORS = ("aats-liquidations-daemon", "aats-microstructure-collector")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    now = datetime.now(UTC)
    containers = [_container_fact(name, _run_command) for name in _COLLECTORS]
    freshness = [
        _collector_heartbeat_fact(name, run=_run_command, now=now)
        for name in _COLLECTORS
    ]
    payload = {
        "format_version": 1,
        "generated_at": now.isoformat(),
        "profile": "derivatives",
        "collector_containers": containers,
        "collector_freshness": freshness,
        "production_ready": False,
        "trading_ready": False,
        "authorization_boundary": (
            "collector heartbeat evidence only; no research or trading authorization"
        ),
    }
    digest = immutable_json_write(payload, args.output)
    print(
        json.dumps(
            {"output": args.output.as_posix(), "sha256": digest, "fresh": True},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
