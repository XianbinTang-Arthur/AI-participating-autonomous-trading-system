#!/usr/bin/env python3
"""Write a fresh, read-only heartbeat packet for derivatives public collectors."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.governance._atomic_io import immutable_json_write  # noqa: E402
from scripts.write_deployment_evidence import (  # noqa: E402
    _COLLECTOR_HEARTBEATS,
    _IMAGE_RE,
    _collector_heartbeat_fact,
    _copy_container_file_mtime,
    _run_command,
)


_COLLECTORS = ("aats-liquidations-daemon", "aats-microstructure-collector")
_CONTAINER_INSPECT_TEMPLATE = "\n".join(
    (
        "{{json .State.Status}}",
        '{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}',
        "{{json .Image}}",
    )
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _collector_container_fact(name: str) -> dict[str, str]:
    if name not in _COLLECTORS:
        raise ValueError("invalid_collector_container_name")
    raw = _run_command(
        ("docker", "inspect", "--format", _CONTAINER_INSPECT_TEMPLATE, name),
        None,
    )
    try:
        status, health, image_id = (
            json.loads(line) for line in raw.splitlines()
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid_collector_container_facts:{name}") from exc
    if not all(isinstance(value, str) for value in (status, health, image_id)):
        raise RuntimeError(f"invalid_collector_container_facts:{name}")
    if status != "running" or health != "healthy":
        raise RuntimeError(f"required_container_not_healthy:{name}:{status}:{health}")
    if _IMAGE_RE.fullmatch(image_id) is None:
        raise RuntimeError(f"invalid_container_image_id:{name}")
    return {"name": name, "status": status, "health": health, "image_id": image_id}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    containers: list[dict[str, str]] = []
    freshness: list[dict[str, object]] = []
    for name in _COLLECTORS:
        containers.append(_collector_container_fact(name))
        heartbeat_epoch = _copy_container_file_mtime(
            name,
            _COLLECTOR_HEARTBEATS[name],
        )
        freshness.append(
            _collector_heartbeat_fact(
                name,
                heartbeat_epoch=heartbeat_epoch,
                now=_utc_now(),
                observation_phase="current",
                observation_method="docker_archive_mtime",
            )
        )
    generated_at = _utc_now()
    payload = {
        "format_version": 1,
        "generated_at": generated_at.isoformat(),
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
