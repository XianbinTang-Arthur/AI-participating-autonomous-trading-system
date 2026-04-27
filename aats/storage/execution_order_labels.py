from __future__ import annotations

from hashlib import blake2b


EXECUTION_ORDER_LABEL_MAX_LENGTH = 32

_KNOWN_STORAGE_LABELS = {
    "semantic_duplicate_snapshot_blocked": "semantic_dup_snapshot_blocked",
    "risk_increase_convergence_blocked": "risk_convergence_blocked",
}


def execution_order_storage_label(value: object, *, fallback: str = "aats") -> str:
    """Return a stable label that fits execution_orders varchar(32) columns."""
    raw = str(value or "").strip() or fallback
    label = _KNOWN_STORAGE_LABELS.get(raw, raw)
    if len(label) <= EXECUTION_ORDER_LABEL_MAX_LENGTH:
        return label

    digest = blake2b(raw.encode("utf-8"), digest_size=4).hexdigest()
    prefix_length = EXECUTION_ORDER_LABEL_MAX_LENGTH - len(digest) - 1
    return f"{label[:prefix_length].rstrip('_')}#{digest}"
