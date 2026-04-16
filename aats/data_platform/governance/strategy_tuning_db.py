"""DB helpers for automated strategy tuning proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import json_dumps, parse_dt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _with_payload(payload: Any, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        result.update(payload)
    result.update({key: value for key, value in fields.items() if value is not None})
    return result


def db_upsert_strategy_tuning_proposal(session: Session, proposal: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO governance.strategy_tuning_proposals
                (proposal_id, review_id, last_review_id, combo_key, family, timeframe,
                 parameter, current_value, proposed_value, delta, confidence, status,
                 review_required, dominant_blocker, dominant_blocker_ratio, rationale,
                 review_notes, reviewed_at, reviewed_by, superseded_at,
                 superseded_by_review_id, created_at, last_seen_at, payload, updated_at)
            VALUES
                (:proposal_id, :review_id, :last_review_id, :combo_key, :family, :timeframe,
                 :parameter, CAST(:current_value AS jsonb), CAST(:proposed_value AS jsonb),
                 :delta, :confidence, :status, :review_required, :dominant_blocker,
                 :dominant_blocker_ratio, :rationale, :review_notes, :reviewed_at,
                 :reviewed_by, :superseded_at, :superseded_by_review_id, :created_at,
                 :last_seen_at, CAST(:payload AS jsonb), :updated_at)
            ON CONFLICT (proposal_id) DO UPDATE SET
                review_id = EXCLUDED.review_id,
                last_review_id = EXCLUDED.last_review_id,
                combo_key = EXCLUDED.combo_key,
                family = EXCLUDED.family,
                timeframe = EXCLUDED.timeframe,
                parameter = EXCLUDED.parameter,
                current_value = EXCLUDED.current_value,
                proposed_value = EXCLUDED.proposed_value,
                delta = EXCLUDED.delta,
                confidence = EXCLUDED.confidence,
                status = EXCLUDED.status,
                review_required = EXCLUDED.review_required,
                dominant_blocker = EXCLUDED.dominant_blocker,
                dominant_blocker_ratio = EXCLUDED.dominant_blocker_ratio,
                rationale = EXCLUDED.rationale,
                review_notes = EXCLUDED.review_notes,
                reviewed_at = EXCLUDED.reviewed_at,
                reviewed_by = EXCLUDED.reviewed_by,
                superseded_at = EXCLUDED.superseded_at,
                superseded_by_review_id = EXCLUDED.superseded_by_review_id,
                last_seen_at = EXCLUDED.last_seen_at,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "proposal_id": proposal.get("proposal_id"),
            "review_id": proposal.get("review_id"),
            "last_review_id": proposal.get("last_review_id"),
            "combo_key": proposal.get("combo_key"),
            "family": proposal.get("family"),
            "timeframe": str(proposal.get("timeframe") or "").lower(),
            "parameter": proposal.get("parameter"),
            "current_value": json_dumps(proposal.get("current_value")),
            "proposed_value": json_dumps(proposal.get("proposed_value")),
            "delta": proposal.get("delta"),
            "confidence": proposal.get("confidence", "low"),
            "status": proposal.get("status", "pending_review"),
            "review_required": bool(proposal.get("review_required", True)),
            "dominant_blocker": proposal.get("dominant_blocker"),
            "dominant_blocker_ratio": proposal.get("dominant_blocker_ratio"),
            "rationale": proposal.get("rationale"),
            "review_notes": proposal.get("review_notes"),
            "reviewed_at": parse_dt(proposal.get("reviewed_at")),
            "reviewed_by": proposal.get("reviewed_by"),
            "superseded_at": parse_dt(proposal.get("superseded_at")),
            "superseded_by_review_id": proposal.get("superseded_by_review_id"),
            "created_at": parse_dt(proposal.get("created_at")) or _utcnow(),
            "last_seen_at": parse_dt(proposal.get("last_seen_at")) or _utcnow(),
            "payload": json_dumps(proposal),
            "updated_at": _utcnow(),
        },
    )


def db_load_strategy_tuning_registry(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT payload, created_at
            FROM governance.strategy_tuning_proposals
            ORDER BY created_at ASC
            """
        ),
    ).fetchall()
    return {
        "generated_at": _utcnow().isoformat(),
        "version": len(rows),
        "proposals": [
            _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)
            for row in rows
        ],
    }


def db_find_strategy_tuning_proposal(
    session: Session,
    proposal_id: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload, created_at
            FROM governance.strategy_tuning_proposals
            WHERE proposal_id = :proposal_id
            """
        ),
        {"proposal_id": proposal_id},
    ).fetchone()
    if row is None:
        return None
    return _with_payload(row.payload, created_at=row.created_at.isoformat() if row.created_at else None)


def db_load_strategy_tuning_overrides(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT combo_key, parameter, proposed_value
            FROM governance.strategy_tuning_proposals
            WHERE status = 'approved'
            ORDER BY COALESCE(reviewed_at, created_at) ASC
            """
        ),
    ).fetchall()
    combo_overrides: dict[str, dict[str, Any]] = {}
    for row in rows:
        combo_overrides.setdefault(str(row.combo_key), {})[str(row.parameter)] = row.proposed_value
    return {
        "generated_at": _utcnow().isoformat(),
        "combo_overrides": combo_overrides,
    }

