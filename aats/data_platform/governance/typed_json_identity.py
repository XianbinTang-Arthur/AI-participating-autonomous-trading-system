"""Type-sensitive canonical identities for governed JSON payloads.

PostgreSQL ``jsonb`` equality deliberately treats JSON numbers ``1`` and
``1.0`` as equal.  Governance identities cannot use that equality because the
application parameter contract distinguishes integers from floats.  This
module owns the canonical encoding used before payloads cross the JSONB
boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _normalize_typed_json(value: Any) -> Any:
    """Return strict JSON with the project's numeric identity semantics.

    ``type(...)`` checks are intentional: ``bool`` must not be accepted as an
    ``int`` subclass, and application-specific numeric/string subclasses must
    not silently acquire a governance identity.  Signed zero is the only
    spelling normalized because both IEEE values represent the same parameter
    value in the existing parameter fingerprint contract.
    """

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("typed_json_identity_invalid")
        return 0.0 if value == 0.0 else value
    if type(value) is list:
        return [_normalize_typed_json(item) for item in value]
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise ValueError("typed_json_identity_invalid")
        return {
            key: _normalize_typed_json(item)
            for key, item in value.items()
        }
    raise ValueError("typed_json_identity_invalid")


def canonical_typed_json_bytes(value: Any) -> bytes:
    """Serialize strict JSON deterministically while preserving number types."""

    try:
        return json.dumps(
            _normalize_typed_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("typed_json_identity_invalid") from exc


def typed_json_sha256(value: Any) -> str:
    """Return a lowercase SHA-256 digest of canonical type-sensitive JSON."""

    return hashlib.sha256(canonical_typed_json_bytes(value)).hexdigest()
