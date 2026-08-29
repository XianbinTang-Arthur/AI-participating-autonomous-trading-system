#!/usr/bin/env python3
"""Capture public collector heartbeat mtimes before the health boundary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import write_deployment_evidence as evidence  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("spot", "derivatives"), required=True)
    args = parser.parse_args(argv)
    required_names = evidence._REQUIRED_CONTAINERS_BY_PROFILE[args.profile]
    for name in sorted(required_names):
        heartbeat_path = evidence._COLLECTOR_HEARTBEATS.get(name)
        if heartbeat_path is None:
            continue
        epoch = evidence._copy_container_file_mtime(name, heartbeat_path)
        print(f"{name}={epoch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
