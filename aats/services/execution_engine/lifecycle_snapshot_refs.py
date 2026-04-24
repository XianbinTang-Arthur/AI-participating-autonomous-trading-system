from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SNAPSHOT_REF_KEYS = (
    "market_snapshot_ref",
    "feature_snapshot_ref",
    "portfolio_snapshot_ref",
    "health_snapshot_ref",
)


def snapshot_refs_from_obj(obj: Any) -> dict[str, str | None]:
    return {
        key: _normalize_snapshot_ref(getattr(obj, key, None))
        for key in SNAPSHOT_REF_KEYS
    }


def snapshot_refs_from_payload(payload: Mapping[str, Any] | None) -> dict[str, str | None]:
    source = payload if isinstance(payload, Mapping) else {}
    return {key: _normalize_snapshot_ref(source.get(key)) for key in SNAPSHOT_REF_KEYS}


def choose_snapshot_refs(*candidates: Mapping[str, Any] | None) -> dict[str, str | None]:
    chosen: dict[str, str | None] = {}
    for key in SNAPSHOT_REF_KEYS:
        chosen[key] = next(
            (
                _normalize_snapshot_ref(candidate.get(key))
                for candidate in candidates
                if isinstance(candidate, Mapping) and _normalize_snapshot_ref(candidate.get(key)) is not None
            ),
            None,
        )
    return chosen


def top_level_snapshot_ref_payload(refs: Mapping[str, Any]) -> dict[str, str | None]:
    return {key: _normalize_snapshot_ref(refs.get(key)) for key in SNAPSHOT_REF_KEYS}


def lifecycle_snapshot_ref_payload(
    *,
    existing_raw_payload: Mapping[str, Any] | None = None,
    stage: str,
    refs: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    existing = existing_raw_payload if isinstance(existing_raw_payload, Mapping) else {}
    existing_lifecycle = existing.get("lifecycle_snapshot_refs")
    lifecycle = dict(existing_lifecycle) if isinstance(existing_lifecycle, Mapping) else {}
    lifecycle[stage] = {
        **top_level_snapshot_ref_payload(refs),
        "source": source,
    }
    return {"lifecycle_snapshot_refs": lifecycle}


def order_state_lifecycle_stage(status: str | None, *, exchange_order_id: str | None = None) -> str:
    normalized_status = (status or "").upper()
    if exchange_order_id or normalized_status in {
        "SUBMITTED",
        "ACKED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "FAILED",
        "EXPIRED",
    }:
        return "ack"
    return "submit"


def _normalize_snapshot_ref(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
