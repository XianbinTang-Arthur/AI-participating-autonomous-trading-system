from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aats.schemas.exchange import ExchangeAccountSnapshot, InstrumentMetadata
from aats.schemas.execution import OrderIntent
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter


def _instrument() -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id="BTC-USDT-SWAP",
        symbol="BTC-USDT-SWAP",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        min_size=Decimal("0.01"),
        contract_value=Decimal("0.01"),
        instrument_type="SWAP",
        instrument_family="BTC-USDT",
        settle_currency="USDT",
        state="live",
    )


def _snapshot() -> ExchangeAccountSnapshot:
    return ExchangeAccountSnapshot(
        account_source="test",
        fetched_at=datetime(2026, 4, 26, tzinfo=UTC),
        position_mode="long_short_mode",
    )


def _intent(*, position_intent: str) -> OrderIntent:
    return OrderIntent(
        intent_id=f"intent_{position_intent}",
        decision_id="decision_scale_in_semantics",
        symbol="BTC-USDT-SWAP",
        side="buy",
        quantity=Decimal("0.01"),
        execution_style="taker",
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        idempotency_key=f"idem_{position_intent}",
        product_type="derivatives",
        margin_mode="cross",
        td_mode="cross",
        target_leverage=3.0,
        exposure_side="long",
        position_mode="long_short_mode",
        pos_side="long",
        leg_action="open",
        position_intent=position_intent,  # type: ignore[arg-type]
    )


def test_okx_hedge_semantic_validation_accepts_scale_in_open_leg() -> None:
    adapter = OKXExecutionAdapter.__new__(OKXExecutionAdapter)

    error = adapter._derivatives_submission_semantic_error(
        intent=_intent(position_intent="scale_in_long"),
        instrument=_instrument(),
        snapshot=_snapshot(),
        payload={"sz": "1"},
    )

    assert error is None


def test_okx_hedge_semantic_validation_still_rejects_wrong_side_scale_in() -> None:
    adapter = OKXExecutionAdapter.__new__(OKXExecutionAdapter)

    error = adapter._derivatives_submission_semantic_error(
        intent=_intent(position_intent="scale_in_short"),
        instrument=_instrument(),
        snapshot=_snapshot(),
        payload={"sz": "1"},
    )

    assert error == "okx_leg_action_mismatch_with_position_intent"
