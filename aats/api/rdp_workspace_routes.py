"""Versioned RDP workspace API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import OperationalError

from aats.api.auth import require_read_access
from aats.api.rdp_data_governance import build_data_governance_snapshot
from aats.api.rdp_workspace import build_rdp_workspace
from aats.data_platform.config import get_settings as get_rdp_settings


rdp_workspace_router = APIRouter(prefix="/v3", tags=["RDP-v3"])


@rdp_workspace_router.get(
    "/workspace",
    dependencies=[Depends(require_read_access)],
)
async def get_rdp_workspace(
    request: Request,
    run_limit: int = Query(default=20, ge=5, le=50),
) -> dict[str, Any]:
    """Return one coherent, versioned snapshot for the RDP operator UI."""
    try:
        return build_rdp_workspace(request, run_limit=run_limit)
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "governance_db_unavailable", "retryable": True},
        ) from exc


@rdp_workspace_router.get(
    "/data-governance",
    dependencies=[Depends(require_read_access)],
)
async def get_rdp_data_governance() -> dict[str, Any]:
    """Return the bounded data-governance portion without raw-table scans."""
    try:
        root = Path(get_rdp_settings().project_root).resolve()
        return build_data_governance_snapshot(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "data_governance_unavailable", "retryable": True},
        ) from exc


__all__ = ["rdp_workspace_router"]
