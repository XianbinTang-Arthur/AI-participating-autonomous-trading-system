"""RDP daemon health helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aats.data_platform.governance._time_util import parse_iso_datetime_utc

log = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PATH = Path("/tmp/rdp_daemon_heartbeat.json")
DEFAULT_COMPONENT = "rdp-daemon"
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS = 45
_UNHEALTHY_STATES = {"error", "stopped"}


def _parse_heartbeat_ts(value: str | None) -> datetime | None:
    """Heartbeat timestamp parse; illegal → None + WARN log.

    A corrupt heartbeat_at should fail the freshness check ("heartbeat_at
    missing or invalid") rather than crash the probe, so this wrapper swallows
    :class:`ValueError` after logging.
    """
    try:
        return parse_iso_datetime_utc(value, context="rdp_daemon_heartbeat_at")
    except ValueError as exc:
        log.warning("rdp_daemon_health: illegal heartbeat_at: %s", exc)
        return None


def _load_local_heartbeat(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _heartbeat_ok(
    *,
    payload: dict[str, Any] | None,
    max_age_seconds: int,
) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "heartbeat payload missing"

    heartbeat_at = _parse_heartbeat_ts(str(payload.get("heartbeat_at") or ""))
    if heartbeat_at is None:
        return False, "heartbeat_at missing or invalid"

    age_seconds = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
    if age_seconds >= max_age_seconds:
        return False, f"heartbeat stale ({age_seconds:.1f}s >= {max_age_seconds}s)"

    status = str(payload.get("status") or "unknown")
    if status in _UNHEALTHY_STATES:
        return False, f"status={status}"

    return True, f"status={status}, age_seconds={age_seconds:.1f}"


def check_daemon_health(
    *,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    component: str = DEFAULT_COMPONENT,
    max_age_seconds: int = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate both local and DB-backed daemon heartbeat freshness."""
    result: dict[str, Any] = {
        "healthy": False,
        "component": component,
        "checks": [],
        "errors": [],
    }

    local_heartbeat = _load_local_heartbeat(heartbeat_path)
    local_ok, local_detail = _heartbeat_ok(
        payload=local_heartbeat,
        max_age_seconds=max_age_seconds,
    )
    result["local_heartbeat"] = local_heartbeat
    result["checks"].append(
        {
            "name": "local_heartbeat",
            "status": "ok" if local_ok else "blocked",
            "detail": local_detail,
        }
    )
    if not local_ok:
        result["errors"].append(f"local heartbeat unhealthy: {local_detail}")

    try:
        from aats.data_platform.db import get_session
        from aats.data_platform.governance.rdp_runtime_status_db import (
            db_get_runtime_status,
        )

        with get_session() as session:
            runtime_status = db_get_runtime_status(session, component)
    except Exception as exc:
        runtime_status = None
        result["errors"].append(f"governance runtime status unavailable: {exc}")

    result["runtime_status"] = runtime_status
    runtime_ok, runtime_detail = _heartbeat_ok(
        payload=runtime_status,
        max_age_seconds=max_age_seconds,
    )
    result["checks"].append(
        {
            "name": "governance_runtime_status",
            "status": "ok" if runtime_ok else "blocked",
            "detail": runtime_detail,
        }
    )
    if not runtime_ok:
        result["errors"].append(f"governance runtime status unhealthy: {runtime_detail}")

    result["healthy"] = not result["errors"]
    return result


def daemon_health_exit_code(
    *,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    component: str = DEFAULT_COMPONENT,
    max_age_seconds: int = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
) -> int:
    """Return Docker healthcheck friendly exit code."""
    return 0 if check_daemon_health(
        heartbeat_path=heartbeat_path,
        component=component,
        max_age_seconds=max_age_seconds,
    ).get("healthy") else 1
