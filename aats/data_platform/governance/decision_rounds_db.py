"""Decision round snapshot DB — governance.decision_round_snapshots 读写层."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from ._db_util import ADVISORY_LOCK_KEYS, parse_dt
from ._exceptions import DBConflictError
from .typed_json_identity import canonical_typed_json_bytes, typed_json_sha256

log = logging.getLogger(__name__)

_DECISION_ROUND_PUBLICATION_LOCK_KEY = ADVISORY_LOCK_KEYS[
    "decision_round_publication"
]
_DECISION_ROUND_JSON_IDENTITY_SCHEMA = (
    "aats.decision_round_snapshot.typed_json.v1"
)


def db_acquire_decision_round_publication_lock(
    session: Session,
    *,
    round_id: str,
) -> None:
    """Serialize one Phase 6 publication for the lifetime of its transaction."""

    if not isinstance(round_id, str) or not round_id.strip():
        raise ValueError("decision_round_publication_round_id_invalid")
    session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            ":namespace, hashtext(:round_id))"
        ),
        {
            "namespace": _DECISION_ROUND_PUBLICATION_LOCK_KEY,
            "round_id": round_id,
        },
    )


def db_upsert_decision_round_snapshot(
    session: Session,
    *,
    round_id: str,
    started_at: str | None = None,
    finished_at: str | None = None,
    evidence_bundle_summary: dict[str, Any] | None = None,
    parameter_upgrade_candidates: list[dict[str, Any]] | None = None,
    family_timeframe_decisions: list[dict[str, Any]] | None = None,
    promotion_readiness_assessment: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    conclusion_markdown: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    canonical_json_payload = {
        "schema": _DECISION_ROUND_JSON_IDENTITY_SCHEMA,
        "evidence_bundle_summary": evidence_bundle_summary,
        "parameter_upgrade_candidates": parameter_upgrade_candidates or [],
        "family_timeframe_decisions": family_timeframe_decisions or [],
        "promotion_readiness_assessment": promotion_readiness_assessment,
        "manifest": manifest,
    }
    typed_json_identity_sha256 = typed_json_sha256(canonical_json_payload)
    result = session.execute(
        text(
            """
            INSERT INTO governance.decision_round_snapshots
                (round_id, started_at, finished_at,
                 evidence_summary_json,
                 parameter_upgrade_candidates_json,
                 family_timeframe_decisions_json,
                 promotion_readiness_json,
                 manifest_json,
                 conclusion_markdown,
                 typed_json_identity_sha256,
                 created_at,
                 updated_at)
            VALUES
                (:round_id, :started_at, :finished_at,
                 :evidence_summary_json,
                 :parameter_upgrade_candidates_json,
                 :family_timeframe_decisions_json,
                 :promotion_readiness_json,
                 :manifest_json,
                 :conclusion_markdown,
                 :typed_json_identity_sha256,
                 :now,
                 :now)
            ON CONFLICT (round_id) DO UPDATE SET
                typed_json_identity_sha256 = COALESCE(
                    governance.decision_round_snapshots.typed_json_identity_sha256,
                    EXCLUDED.typed_json_identity_sha256
                )
            WHERE governance.decision_round_snapshots.started_at IS NOT DISTINCT FROM EXCLUDED.started_at
              AND governance.decision_round_snapshots.finished_at IS NOT DISTINCT FROM EXCLUDED.finished_at
              AND governance.decision_round_snapshots.evidence_summary_json::text IS NOT DISTINCT FROM EXCLUDED.evidence_summary_json::text
              AND governance.decision_round_snapshots.parameter_upgrade_candidates_json::text IS NOT DISTINCT FROM EXCLUDED.parameter_upgrade_candidates_json::text
              AND governance.decision_round_snapshots.family_timeframe_decisions_json::text IS NOT DISTINCT FROM EXCLUDED.family_timeframe_decisions_json::text
              AND governance.decision_round_snapshots.promotion_readiness_json::text IS NOT DISTINCT FROM EXCLUDED.promotion_readiness_json::text
              AND governance.decision_round_snapshots.manifest_json::text IS NOT DISTINCT FROM EXCLUDED.manifest_json::text
              AND governance.decision_round_snapshots.conclusion_markdown IS NOT DISTINCT FROM EXCLUDED.conclusion_markdown
              AND (
                    governance.decision_round_snapshots.typed_json_identity_sha256 IS NULL
                    OR governance.decision_round_snapshots.typed_json_identity_sha256
                       = EXCLUDED.typed_json_identity_sha256
              )
            RETURNING round_id
            """
        ),
        {
            "round_id": round_id,
            "started_at": parse_dt(started_at),
            "finished_at": parse_dt(finished_at),
            "evidence_summary_json": _to_json(evidence_bundle_summary),
            "parameter_upgrade_candidates_json": _to_json(parameter_upgrade_candidates or []),
            "family_timeframe_decisions_json": _to_json(family_timeframe_decisions or []),
            "promotion_readiness_json": _to_json(promotion_readiness_assessment),
            "manifest_json": _to_json(manifest),
            "conclusion_markdown": conclusion_markdown,
            "typed_json_identity_sha256": typed_json_identity_sha256,
            "now": now,
        },
    )
    if result.fetchone() is None:
        raise DBConflictError("decision_round_snapshot_immutable_identity_conflict")
    log.info("DB insert/verify decision_round_snapshot: %s", round_id)


def db_load_latest_decision_round_snapshot(session: Session) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT round_id,
                   started_at,
                   finished_at,
                   evidence_summary_json,
                   parameter_upgrade_candidates_json,
                   family_timeframe_decisions_json,
                   promotion_readiness_json,
                   manifest_json,
                   conclusion_markdown
            FROM governance.decision_round_snapshots
            ORDER BY COALESCE(finished_at, started_at, updated_at, created_at) DESC, round_id DESC
            LIMIT 1
            """
        ),
    ).fetchone()
    if row is None:
        return None
    return _decision_round_snapshot_from_row(row)


def db_load_decision_round_snapshot(
    session: Session,
    *,
    round_id: str,
) -> dict[str, Any] | None:
    """Load one exact Phase 6 decision-round snapshot.

    This reader deliberately has no latest-round fallback.  Callers use it for
    recommendation evidence, where substituting a newer round would break the
    source identity and could authorize a legacy recommendation incorrectly.
    """

    row = session.execute(
        text(
            """
            SELECT round_id,
                   started_at,
                   finished_at,
                   evidence_summary_json,
                   parameter_upgrade_candidates_json,
                   family_timeframe_decisions_json,
                   promotion_readiness_json,
                   manifest_json,
                   conclusion_markdown
            FROM governance.decision_round_snapshots
            WHERE round_id = :round_id
            LIMIT 1
            """
        ),
        {"round_id": round_id},
    ).fetchone()
    if row is None:
        return None
    return _decision_round_snapshot_from_row(row)


def db_load_decision_round_snapshots(
    session: Session,
    *,
    round_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Load an exact set of decision-round snapshots in one bounded query."""

    unique_round_ids = tuple(sorted(set(round_ids)))
    if not unique_round_ids:
        return {}
    statement = text(
        """
        SELECT round_id,
               started_at,
               finished_at,
               evidence_summary_json,
               parameter_upgrade_candidates_json,
               family_timeframe_decisions_json,
               promotion_readiness_json,
               manifest_json,
               conclusion_markdown
        FROM governance.decision_round_snapshots
        WHERE round_id IN :round_ids
        """
    ).bindparams(bindparam("round_ids", expanding=True))
    rows = session.execute(
        statement,
        {"round_ids": unique_round_ids},
    ).fetchall()
    return {
        row.round_id: _decision_round_snapshot_from_row(row)
        for row in rows
    }


def _decision_round_snapshot_from_row(row: Any) -> dict[str, Any]:
    return {
        "round_id": row.round_id,
        "started_at": _db_timestamp_iso(row.started_at),
        "finished_at": _db_timestamp_iso(row.finished_at),
        "evidence_bundle_summary": _from_json(row.evidence_summary_json, {}),
        "parameter_upgrade_candidates": _from_json(row.parameter_upgrade_candidates_json, []),
        "family_timeframe_decisions": _from_json(row.family_timeframe_decisions_json, []),
        "promotion_readiness_assessment": _from_json(row.promotion_readiness_json, {}),
        "manifest": _from_json(row.manifest_json, {}),
        "conclusion_markdown": row.conclusion_markdown,
    }


def _db_timestamp_iso(value: datetime | None) -> str | None:
    """Project aware database timestamps as canonical UTC ISO strings.

    A naive driver value is preserved as naive text so downstream promotion
    qualification rejects it instead of silently assuming a timezone.
    """

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _to_json(payload: Any) -> str | None:
    if payload is None:
        return None
    return canonical_typed_json_bytes(payload).decode("utf-8")


def _from_json(payload: str | None, default: Any) -> Any:
    if not payload:
        return default
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return default
