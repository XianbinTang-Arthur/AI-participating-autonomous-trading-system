from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SNAPSHOT_REF_KEYS = (
    "market_snapshot_ref",
    "feature_snapshot_ref",
    "portfolio_snapshot_ref",
    "health_snapshot_ref",
)

LIFECYCLE_MARKET_CONTEXT_REF_KEYS = (
    "pre_event_orderbook_snapshot_ref",
    "post_event_orderbook_snapshot_ref",
)


def snapshot_refs_from_obj(obj: Any) -> dict[str, str | None]:
    return {
        key: _normalize_snapshot_ref(getattr(obj, key, None))
        for key in SNAPSHOT_REF_KEYS
    }


def snapshot_refs_from_payload(payload: Mapping[str, Any] | None) -> dict[str, str | None]:
    source = payload if isinstance(payload, Mapping) else {}
    return {key: _normalize_snapshot_ref(source.get(key)) for key in SNAPSHOT_REF_KEYS}


def lifecycle_market_context_refs_from_obj(obj: Any) -> dict[str, str | None]:
    direct_refs = {
        key: _normalize_snapshot_ref(getattr(obj, key, None))
        for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS
    }
    return choose_lifecycle_market_context_refs(
        direct_refs,
        lifecycle_market_context_refs_from_payload(getattr(obj, "submission_payload", None)),
        lifecycle_market_context_refs_from_payload(getattr(obj, "raw_exchange", None)),
    )


def lifecycle_market_context_refs_from_payload(payload: Mapping[str, Any] | None) -> dict[str, str | None]:
    source = payload if isinstance(payload, Mapping) else {}
    candidates: list[Mapping[str, Any]] = [source]
    for nested_key in (
        "market_context_snapshot_refs",
        "lifecycle_market_context_refs",
        "intent",
        "order_state",
        "fill_event",
        "raw_exchange",
    ):
        nested = source.get(nested_key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    return choose_lifecycle_market_context_refs(*candidates)


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


def choose_lifecycle_market_context_refs(*candidates: Mapping[str, Any] | None) -> dict[str, str | None]:
    chosen: dict[str, str | None] = {}
    for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS:
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


def lifecycle_market_context_ref_payload(refs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = refs if isinstance(refs, Mapping) else {}
    normalized_refs = {
        key: _normalize_snapshot_ref(source.get(key))
        for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS
    }
    missing_refs = [
        key
        for key, value in normalized_refs.items()
        if value is None
    ]
    if not missing_refs:
        capture_status = "captured"
    elif len(missing_refs) == len(LIFECYCLE_MARKET_CONTEXT_REF_KEYS):
        capture_status = "missing"
    else:
        capture_status = "partial"
    return {
        **normalized_refs,
        "capture_status": capture_status,
        "missing_refs": missing_refs,
    }


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
    existing_stage_payload = lifecycle.get(stage)
    existing_stage = existing_stage_payload if isinstance(existing_stage_payload, Mapping) else {}
    existing_market_context_payload = existing_stage.get("market_context_snapshot_refs")
    market_context_refs = (
        dict(existing_market_context_payload)
        if isinstance(existing_market_context_payload, Mapping)
        else {}
    )
    for key in LIFECYCLE_MARKET_CONTEXT_REF_KEYS:
        ref_value = _normalize_snapshot_ref(refs.get(key))
        if ref_value is not None:
            market_context_refs[key] = ref_value
    lifecycle[stage] = {
        **top_level_snapshot_ref_payload(refs),
        "market_context_snapshot_refs": lifecycle_market_context_ref_payload(market_context_refs),
        "source": source,
    }
    normalized_lifecycle: dict[str, Any] = {}
    for lifecycle_stage, stage_payload in lifecycle.items():
        if not isinstance(stage_payload, Mapping):
            continue
        stage_dict = dict(stage_payload)
        raw_market_context = stage_dict.get("market_context_snapshot_refs")
        market_context_source = (
            raw_market_context
            if isinstance(raw_market_context, Mapping)
            else stage_dict
        )
        stage_dict["market_context_snapshot_refs"] = lifecycle_market_context_ref_payload(
            market_context_source
        )
        normalized_lifecycle[str(lifecycle_stage)] = stage_dict
    return {"lifecycle_snapshot_refs": normalized_lifecycle}


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
