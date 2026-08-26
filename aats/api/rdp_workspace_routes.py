"""Versioned RDP workspace API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import OperationalError

from aats.api.auth import require_read_access
from aats.api.rdp_workspace import build_rdp_workspace


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


__all__ = ["rdp_workspace_router"]
