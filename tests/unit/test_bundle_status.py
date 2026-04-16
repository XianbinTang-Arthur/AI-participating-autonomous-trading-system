from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from aats.schemas.execution import OrderState
from aats.services.execution_engine.bundle_status import (
    apply_strategy_bundle_status_reason_codes,
    derive_strategy_bundle_status,
)


def _order_state(*, client_order_id: str, status: str, execution_mode: str, filled_qty: str = "0") -> OrderState:
    now = datetime.now(timezone.utc)
    return OrderState(
        decision_id="decision_bundle_status_1",
        intent_id=f"intent_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        client_order_id=client_order_id,
        venue="OKX",
        exchange_order_id=None,
        status=status,
        submission_mode="guarded_simulated_submit",
        submitted_ts=now,
        last_update_ts=now,
        requested_qty=Decimal("0.01"),
        filled_qty=Decimal(filled_qty),
        remaining_qty=Decimal("0.01") - Decimal(filled_qty),
        average_fill_price=None,
        fees=Decimal("0"),
        product_type="derivatives",
        margin_mode="cross",
        position_mode="long_short_mode",
        pos_side="long",
        strategy_family="independent",
        strategy_sleeve_id="sleeve_independent_long",
        allocation_id="alloc_bundle_status_1",
        strategy_bundle_id="bundle_status_1",
        strategy_leg_role="hedge",
        strategy_execution_mode=execution_mode,
        submission_payload={},
    )


def test_derive_strategy_bundle_status_marks_all_blocked_bundle_as_blocked() -> None:
    status = derive_strategy_bundle_status(
        order_states=[
            _order_state(
                client_order_id="cl_blocked_family_mode",
                status="BLOCKED",
                execution_mode="independent_books",
            )
        ],
        previous_status="submitted",
    )

    assert status == "blocked"
    assert apply_strategy_bundle_status_reason_codes(
        reason_codes=["independent_overlay_rollout_stage_blocks_live_runtime"],
        status=status,
    ) == [
        "independent_overlay_rollout_stage_blocks_live_runtime",
        "strategy_bundle_blocked",
    ]


def test_derive_strategy_bundle_status_keeps_mixed_open_and_blocked_bundle_as_review_required() -> None:
    status = derive_strategy_bundle_status(
        order_states=[
            _order_state(
                client_order_id="cl_submitted_long",
                status="SUBMITTED",
                execution_mode="independent_long_book",
            ),
            _order_state(
                client_order_id="cl_blocked_short",
                status="BLOCKED",
                execution_mode="independent_short_book",
            ),
        ],
        previous_status="submitted",
    )

    assert status == "review_required"
