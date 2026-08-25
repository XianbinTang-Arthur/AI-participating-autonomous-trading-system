from __future__ import annotations

from aats.schemas.common import EventEnvelope
from aats.storage.scope_metadata import envelope_scope_metadata


def _envelope(*, topic: str, key: str, payload: dict[str, object]) -> EventEnvelope:
    return EventEnvelope(
        event_type="TestEvent",
        source_component="test",
        topic=topic,
        key=key,
        payload=payload,
    )


def test_risk_decision_uses_symbol_envelope_key_for_scope_metadata() -> None:
    envelope = _envelope(
        topic="risk.decisions",
        key="BTC-USDT-SWAP",
        payload={"decision_id": "decision-1", "approved": True},
    )

    scope = envelope_scope_metadata(envelope)

    assert scope["symbol"] == "BTC-USDT-SWAP"
    assert scope["product_type"] == "derivatives"


def test_non_risk_event_does_not_treat_arbitrary_key_as_symbol() -> None:
    envelope = _envelope(
        topic="system.operator_actions",
        key="operator-action-1",
        payload={"action": "inspect"},
    )

    scope = envelope_scope_metadata(envelope)

    assert scope["symbol"] is None
    assert scope["product_type"] is None
