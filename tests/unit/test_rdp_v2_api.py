from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from aats.api.auth import OperatorPrincipal, require_read_access, require_write_access
from aats.api.rdp_v2 import rdp_v2_router


class _Session:
    pass


@contextmanager
def _session():
    yield _Session()


def _principal() -> OperatorPrincipal:
    return OperatorPrincipal(
        identity="alice",
        role="operator",
        auth_enabled=True,
        auth_source="session",
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(rdp_v2_router, prefix="/rdp")
    app.dependency_overrides[require_read_access] = _principal
    app.dependency_overrides[require_write_access] = _principal
    return app


def _run(status: str = "queued") -> dict[str, object]:
    return {
        "run_id": "run_123",
        "workflow": "research_cycle",
        "status": status,
        "trigger_kind": "manual",
        "requested_by": "alice",
        "completed_steps": 0,
        "total_steps": 0,
    }


def _detail_patches(status: str = "queued"):
    return (
        patch("aats.api.rdp_v2.governance_session", _session),
        patch("aats.data_platform.governance.rdp_runs_db.db_get_run", return_value=_run(status)),
        patch("aats.data_platform.governance.rdp_runs_db.db_get_run_attempts", return_value=[]),
        patch("aats.data_platform.governance.rdp_runs_db.db_get_run_steps", return_value=[]),
        patch("aats.data_platform.governance.rdp_runs_db.db_get_run_events", return_value=[]),
    )


def test_create_run_requires_idempotency_key() -> None:
    response = TestClient(_app()).post(
        "/rdp/v2/runs",
        json={"workflow": "research_cycle"},
    )
    assert response.status_code == 422


def test_create_run_returns_logical_run_and_binds_authenticated_actor() -> None:
    captured: dict[str, object] = {}

    def _create(_session, **kwargs):
        captured.update(kwargs)
        return "task_123", None

    patches = _detail_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
        "aats.data_platform.operations.workflow_dispatcher.describe_manual_trigger_availability",
        return_value={"enabled": True},
    ), patch(
        "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
        side_effect=_create,
    ), patch(
        "aats.data_platform.governance.rdp_task_db.db_get_latest_task_for_workflow",
        return_value={"task_id": "task_123", "run_id": "run_123"},
    ):
        response = TestClient(_app()).post(
            "/rdp/v2/runs",
            headers={"Idempotency-Key": "ui-request-123"},
            json={"workflow": "research_cycle", "payload": {"source": "test"}},
        )

    assert response.status_code == 202
    assert response.json()["run"]["run_id"] == "run_123"
    assert captured["requested_by"] == "alice"
    assert captured["idempotency_key"] == "ui-request-123"


def test_create_run_replays_same_idempotency_key_without_duplicate() -> None:
    patches = _detail_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
        "aats.data_platform.operations.workflow_dispatcher.describe_manual_trigger_availability",
        return_value={"enabled": True},
    ), patch(
        "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
        return_value=(
            None,
            {
                "task_id": "task_123",
                "run_id": "run_123",
                "status": "pending",
                "idempotent_replay": True,
            },
        ),
    ):
        response = TestClient(_app()).post(
            "/rdp/v2/runs",
            headers={"Idempotency-Key": "ui-request-123"},
            json={"workflow": "research_cycle"},
        )

    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is True


def test_create_run_rejects_idempotency_key_reused_for_other_workflow() -> None:
    patches = _detail_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
        "aats.data_platform.operations.workflow_dispatcher.describe_manual_trigger_availability",
        return_value={"enabled": True},
    ), patch(
        "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
        return_value=(
            None,
            {
                "task_id": "task_123",
                "run_id": "run_123",
                "status": "pending",
                "idempotent_replay": True,
            },
        ),
    ):
        response = TestClient(_app()).post(
            "/rdp/v2/runs",
            headers={"Idempotency-Key": "ui-request-123"},
            json={"workflow": "governance_cycle"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_key_payload_mismatch"


def test_create_run_reports_workflow_busy_as_conflict() -> None:
    with patch("aats.api.rdp_v2.governance_session", _session), patch(
        "aats.data_platform.operations.workflow_dispatcher.describe_manual_trigger_availability",
        return_value={"enabled": True},
    ), patch(
        "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
        return_value=(
            None,
            {"task_id": "task_busy", "run_id": "run_busy", "status": "running"},
        ),
    ):
        response = TestClient(_app()).post(
            "/rdp/v2/runs",
            headers={"Idempotency-Key": "ui-request-456"},
            json={"workflow": "research_cycle"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workflow_busy"


def test_run_detail_maps_governance_db_failure_to_retryable_503() -> None:
    with patch("aats.api.rdp_v2.governance_session", _session), patch(
        "aats.data_platform.governance.rdp_runs_db.db_get_run",
        side_effect=OperationalError("SELECT 1", {}, RuntimeError("db down")),
    ):
        response = TestClient(_app()).get("/rdp/v2/runs/run_123")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "governance_db_unavailable",
        "retryable": True,
    }
