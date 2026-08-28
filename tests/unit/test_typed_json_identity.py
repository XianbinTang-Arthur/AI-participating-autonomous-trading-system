from __future__ import annotations

import math

import pytest

from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)
from aats.data_platform.governance.typed_json_identity import typed_json_sha256


def test_typed_json_identity_preserves_numeric_type_and_key_order_semantics() -> None:
    assert typed_json_sha256({"value": 1}) != typed_json_sha256({"value": 1.0})
    assert typed_json_sha256({"a": 1, "b": [2.0]}) == typed_json_sha256(
        {"b": [2.0], "a": 1}
    )
    assert typed_json_sha256({"zero": -0.0}) == typed_json_sha256({"zero": 0.0})


@pytest.mark.parametrize(
    "invalid",
    [
        {"value": math.nan},
        {"value": math.inf},
        {1: "non-string key"},
        {"value": (1, 2)},
    ],
)
def test_typed_json_identity_rejects_non_strict_json(invalid: object) -> None:
    with pytest.raises(ValueError, match="typed_json_identity_invalid"):
        typed_json_sha256(invalid)


def test_parameter_values_fingerprint_remains_compatible_with_shared_encoder() -> None:
    values = {"entry_threshold": 1.0, "signed_zero": -0.0}
    assert parameter_values_fingerprint(values) == typed_json_sha256(
        {"schema": "aats.parameter_values.v1", "values": values}
    )
