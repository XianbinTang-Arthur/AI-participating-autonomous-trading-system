"""Data-gap persistence with explicit recoverability semantics."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text


_CLASSIFICATIONS = {
    "deterministic_rebuild",
    "official_backfill",
    "third_party_candidate",
    "prospective_only",
    "cannot_recover",
}
_STATUSES = {
    "OPEN",
    "CLASSIFIED",
    "BACKFILLED",
    "REBUILT",
    "AWAITING_LIVE_COLLECTION",
    "CANNOT_RECOVER",
    "THIRD_PARTY_ONLY",
}
_TERMINAL_STATUS_BY_CLASSIFICATION = {
    "deterministic_rebuild": "REBUILT",
    "official_backfill": "BACKFILLED",
    "third_party_candidate": "THIRD_PARTY_ONLY",
    "prospective_only": "AWAITING_LIVE_COLLECTION",
    "cannot_recover": "CANNOT_RECOVER",
}
_RESOLVED_STATUSES = {
    "BACKFILLED",
    "REBUILT",
    "CANNOT_RECOVER",
    "THIRD_PARTY_ONLY",
}


@dataclass(frozen=True)
class DataGap:
    dataset_name: str
    symbol: str
    channel: str
    gap_start: datetime
    gap_end: datetime
    classification: str
    status: str
    reason_code: str
    source_id: str | None = None
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.classification not in _CLASSIFICATIONS:
            raise ValueError("data_gap_classification_invalid")
        if self.status not in _STATUSES:
            raise ValueError("data_gap_status_invalid")
        expected_terminal = _TERMINAL_STATUS_BY_CLASSIFICATION[self.classification]
        if self.status not in {"OPEN", "CLASSIFIED", expected_terminal}:
            raise ValueError("data_gap_status_incompatible_with_classification")
        if self.status in _RESOLVED_STATUSES:
            raise ValueError("data_gap_resolved_status_requires_transition")
        if self.gap_start.tzinfo is None or self.gap_start.utcoffset() is None:
            raise ValueError("data_gap_start_must_be_timezone_aware")
        if self.gap_end.tzinfo is None or self.gap_end.utcoffset() is None:
            raise ValueError("data_gap_end_must_be_timezone_aware")
        if self.gap_end <= self.gap_start:
            raise ValueError("data_gap_end_must_follow_start")
        if not all(
            str(value).strip()
            for value in (
                self.dataset_name,
                self.symbol,
                self.channel,
                self.reason_code,
            )
        ):
            raise ValueError("data_gap_identity_must_be_nonempty")
        if self.source_id is not None:
            try:
                uuid.UUID(self.source_id)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("data_gap_source_id_invalid") from exc


def prospective_drop_gap(
    *,
    dataset_name: str,
    symbol: str,
    channel: str,
    event_ts: datetime,
    reason_code: str,
    details: dict[str, Any] | None = None,
) -> DataGap:
    """Build a fail-closed gap for live public data that cannot be replayed.

    Callers may provide exact ``gap_start`` / ``gap_end`` ISO timestamps in
    ``details``.  When row bounds are unavailable, the record deliberately
    marks a one-millisecond evidence point; it does not pretend the unknown
    loss interval was only one millisecond.
    """

    evidence = dict(details or {})
    start = _parse_evidence_time(evidence.get("gap_start")) or event_ts
    end = _parse_evidence_time(evidence.get("gap_end"))
    if end is None or end <= start:
        end = start + timedelta(milliseconds=1)
        evidence["range_precision"] = "event_marker_only"
    else:
        evidence["range_precision"] = "source_row_bounds"
    evidence.setdefault("recoverability", "prospective_collection_only")
    return DataGap(
        dataset_name=dataset_name,
        symbol=symbol,
        channel=channel,
        gap_start=start.astimezone(timezone.utc),
        gap_end=end.astimezone(timezone.utc),
        classification="prospective_only",
        status="AWAITING_LIVE_COLLECTION",
        reason_code=reason_code,
        evidence=evidence,
    )


def official_backfill_gap(
    *,
    dataset_name: str,
    symbol: str,
    channel: str,
    gap_start: datetime,
    gap_end: datetime,
    reason_code: str,
    source_id: str | None,
    evidence: dict[str, Any] | None = None,
) -> DataGap:
    """Describe a missing official-source interval without inventing rows."""

    return DataGap(
        source_id=source_id,
        dataset_name=dataset_name,
        symbol=symbol,
        channel=channel,
        gap_start=gap_start.astimezone(timezone.utc),
        gap_end=gap_end.astimezone(timezone.utc),
        classification="official_backfill",
        status="OPEN",
        reason_code=reason_code,
        evidence={
            "recoverability": "retry_or_supply_official_source",
            **(evidence or {}),
        },
    )


def record_data_gaps(session, gaps: Iterable[DataGap]) -> int:
    inserted_or_confirmed = 0
    for gap in gaps:
        payload = {
            **asdict(gap),
            "evidence": json.dumps(gap.evidence or {}, sort_keys=True),
        }
        gap_id = session.execute(
            text(
                """
                INSERT INTO meta.data_gap_records (
                    source_id, dataset_name, symbol, channel, gap_start, gap_end,
                    classification, status, reason_code, evidence
                ) VALUES (
                    CAST(:source_id AS UUID), :dataset_name, :symbol, :channel,
                    :gap_start, :gap_end, :classification, :status, :reason_code,
                    CAST(:evidence AS jsonb)
                )
                ON CONFLICT ON CONSTRAINT uq_data_gap_scope_reason DO UPDATE SET
                    reason_code = EXCLUDED.reason_code
                WHERE meta.data_gap_records.source_id IS NOT DISTINCT FROM EXCLUDED.source_id
                  AND meta.data_gap_records.classification = EXCLUDED.classification
                  AND meta.data_gap_records.status = EXCLUDED.status
                  AND meta.data_gap_records.evidence = EXCLUDED.evidence
                RETURNING gap_id
                """
            ),
            payload,
        ).scalar_one_or_none()
        if gap_id is None:
            raise RuntimeError("data_gap_immutable_evidence_conflict")
        inserted_or_confirmed += 1
    return inserted_or_confirmed


_ALLOWED_TRANSITIONS = {
    "OPEN": {"CLASSIFIED"},
    "CLASSIFIED": {
        "BACKFILLED",
        "REBUILT",
        "AWAITING_LIVE_COLLECTION",
        "CANNOT_RECOVER",
        "THIRD_PARTY_ONLY",
    },
}


def transition_data_gap(
    session,
    *,
    gap_id: str,
    expected_status: str,
    new_status: str,
    resolution_evidence: dict[str, Any],
) -> None:
    """Advance one gap without permitting terminal evidence rewrites."""

    if new_status not in _ALLOWED_TRANSITIONS.get(expected_status, set()):
        raise ValueError("data_gap_transition_not_allowed")
    if not resolution_evidence:
        raise ValueError("data_gap_resolution_evidence_required")
    required_classification = next(
        (
            classification
            for classification, terminal_status in _TERMINAL_STATUS_BY_CLASSIFICATION.items()
            if terminal_status == new_status
        ),
        None,
    )
    result = session.execute(
        text(
            """
            UPDATE meta.data_gap_records SET
                status = :new_status,
                evidence = evidence || CAST(:resolution AS jsonb),
                resolved_at = CASE
                    WHEN :new_status IN (
                        'BACKFILLED','REBUILT','CANNOT_RECOVER','THIRD_PARTY_ONLY'
                    ) THEN NOW() ELSE NULL END,
                updated_at = NOW()
            WHERE gap_id = CAST(:gap_id AS UUID) AND status = :expected_status
              AND (
                    CAST(:required_classification AS TEXT) IS NULL
                    OR classification = CAST(:required_classification AS TEXT)
                  )
            """
        ),
        {
            "gap_id": gap_id,
            "expected_status": expected_status,
            "new_status": new_status,
            "required_classification": required_classification,
            "resolution": json.dumps(resolution_evidence, sort_keys=True),
        },
    )
    if result.rowcount != 1:
        raise RuntimeError("data_gap_transition_conflict")


def _parse_evidence_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = [
    "DataGap",
    "official_backfill_gap",
    "prospective_drop_gap",
    "record_data_gaps",
    "transition_data_gap",
]
