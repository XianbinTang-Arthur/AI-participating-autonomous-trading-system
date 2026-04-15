from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.rdp_routes import rdp_router


def _build_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            operator_auth_enabled=False,
            operator_control_plane_execution_ledger_enabled=False,
            operator_unsafe_write_without_auth=True,
            storage_mode="memory",
        ),
        environment_capabilities=SimpleNamespace(local_only=True),
    )


def test_create_release_api_rejects_prod_skip_gate() -> None:
    app = FastAPI()
    app.include_router(rdp_router)
    app.state.runtime = _build_runtime()

    with patch.dict(
        os.environ,
        {"RDP_ENV": "prod", "RDP_PRODUCTION_APPLY_ENABLED": "true"},
        clear=False,
    ):
        client = TestClient(app)
        response = client.post(
            "/rdp/releases/create",
            json={
                "recommendation_id": "rec_prod_api",
                "actor": "operator",
                "skip_gate": True,
                "skip_apply": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "requires gate pass" in payload["message"]
