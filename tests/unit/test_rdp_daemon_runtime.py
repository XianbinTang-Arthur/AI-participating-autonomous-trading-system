from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.operations.rdp_daemon_health import check_daemon_health


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@contextmanager
def _fake_session():
    yield object()


def test_check_daemon_health_requires_fresh_local_and_db_runtime_status(
    tmp_path: Path,
) -> None:
    heartbeat_path = tmp_path / "rdp_daemon_heartbeat.json"
    heartbeat_at = datetime.now(timezone.utc).isoformat()
    _write_json(
        heartbeat_path,
        {
            "component": "rdp-daemon",
            "status": "healthy",
            "heartbeat_at": heartbeat_at,
        },
    )

    runtime_status = {
        "component": "rdp-daemon",
        "status": "healthy",
        "heartbeat_at": heartbeat_at,
        "details": {},
    }

    with (
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.governance.rdp_runtime_status_db.db_get_runtime_status",
            return_value=runtime_status,
        ),
    ):
        result = check_daemon_health(heartbeat_path=heartbeat_path)

    assert result["healthy"] is True
    assert result["errors"] == []


def test_check_daemon_health_blocks_when_db_runtime_status_missing(
    tmp_path: Path,
) -> None:
    heartbeat_path = tmp_path / "rdp_daemon_heartbeat.json"
    _write_json(
        heartbeat_path,
        {
            "component": "rdp-daemon",
            "status": "healthy",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    with (
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.governance.rdp_runtime_status_db.db_get_runtime_status",
            return_value=None,
        ),
    ):
        result = check_daemon_health(heartbeat_path=heartbeat_path)

    assert result["healthy"] is False
    assert any("governance runtime status unhealthy" in item for item in result["errors"])


def test_execute_workflow_handles_large_output_without_pipe_deadlock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = importlib.import_module("scripts.rdp_task_daemon")

    workflow_script = tmp_path / "scripts" / "rdp_run_scheduled_workflow.py"
    workflow_script.parent.mkdir(parents=True, exist_ok=True)
    workflow_script.write_text(
        "for _ in range(4096):\n"
        "    print('x' * 256)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(daemon, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setitem(daemon.WORKFLOW_TIMEOUTS, "spam", 5)
    monkeypatch.setattr(daemon, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

    exit_code, log_tail, error_message = daemon.execute_workflow("spam")

    assert exit_code == 0
    assert error_message == ""
    assert "x" in log_tail
