"""Deterministic, versioned identity helpers for governed parameter values."""

from __future__ import annotations

from typing import Any, Mapping

from .typed_json_identity import typed_json_sha256


PARAMETER_VALUES_FINGERPRINT_SCHEMA = "aats.parameter_values.v1"


def parameter_values_fingerprint(values: Any) -> str:
    """Return the canonical SHA-256 identity for one parameter-values object.

    The schema discriminator prevents the digest from being reused as a hash
    for another business object.  Rejecting non-JSON and non-finite values is
    deliberate: governance evidence must not depend on Python-specific
    serialization fallbacks.
    """

    if type(values) is not dict:
        raise ValueError("parameter_values_invalid")
    try:
        return typed_json_sha256(
            {
                "schema": PARAMETER_VALUES_FINGERPRINT_SCHEMA,
                "values": values,
            }
        )
    except ValueError as exc:
        raise ValueError("parameter_values_invalid") from exc


def parameter_set_immutable_identity(
    parameter_set: Mapping[str, Any],
) -> tuple[Any, ...]:
    """Build the application-layer insert-once identity for a parameter set."""

    return (
        parameter_set.get("family"),
        parameter_set.get("symbol", "BTC-USDT-SWAP"),
        str(parameter_set.get("timeframe") or "").lower(),
        parameter_set.get("source_round_id"),
        parameter_set.get("source_phase"),
        parameter_set.get("dataset_version", "v1.0"),
        parameter_set.get("confidence"),
        parameter_values_fingerprint(parameter_set.get("values")),
    )
