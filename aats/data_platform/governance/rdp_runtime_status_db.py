"""RDP Runtime Status DB — governance.rdp_runtime_status 读写层."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def db_upsert_runtime_status(
    session: Session,
    *,
    component: str,
    status: str,
    heartbeat_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """写入或更新组件运行态心跳."""
    now = heartbeat_at or datetime.now(timezone.utc)
    session.execute(
        text("""
            INSERT INTO governance.rdp_runtime_status
                (component, status, heartbeat_at, details_json, created_at, updated_at)
            VALUES
                (:component, :status, :heartbeat_at, :details_json, :now, :now)
            ON CONFLICT (component) DO UPDATE
            SET status = EXCLUDED.status,
                heartbeat_at = EXCLUDED.heartbeat_at,
                details_json = EXCLUDED.details_json,
                updated_at = EXCLUDED.updated_at
        """),
        {
            "component": component,
            "status": status,
            "heartbeat_at": now,
            "details_json": json.dumps(details or {}, ensure_ascii=False),
            "now": now,
        },
    )


def db_get_runtime_status(
    session: Session,
    component: str,
) -> dict[str, Any] | None:
    """读取单个组件的运行态心跳."""
    row = session.execute(
        text("""
            SELECT component, status, heartbeat_at, details_json, created_at, updated_at
            FROM governance.rdp_runtime_status
            WHERE component = :component
            LIMIT 1
        """),
        {"component": component},
    ).fetchone()
    if row is None:
        return None
    try:
        details = json.loads(row.details_json) if row.details_json else {}
    except (TypeError, json.JSONDecodeError):
        details = {"raw_details_json": row.details_json}
    return {
        "component": row.component,
        "status": row.status,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "details": details,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def db_list_runtime_status(session: Session) -> list[dict[str, Any]]:
    """列出全部组件运行态心跳."""
    rows = session.execute(
        text("""
            SELECT component, status, heartbeat_at, details_json, created_at, updated_at
            FROM governance.rdp_runtime_status
            ORDER BY component ASC
        """),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row.details_json) if row.details_json else {}
        except (TypeError, json.JSONDecodeError):
            details = {"raw_details_json": row.details_json}
        result.append(
            {
                "component": row.component,
                "status": row.status,
                "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
                "details": details,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    return result
