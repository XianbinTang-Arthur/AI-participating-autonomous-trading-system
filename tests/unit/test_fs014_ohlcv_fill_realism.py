"""FS-014 adversarial tests for the bounded OHLCV fill proxy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from aats.data_platform.replay.backtest.cost_validator import CostValidator
from aats.data_platform.replay.backtest.fill_simulator import (
    FILL_MODEL_VERSION,
    FillRequest,
    FillSimulator,
)
from aats.data_platform.replay.backtest.harness import BacktestConfig, run_backtest
from tests.unit.replay_contract_fixtures import LINEAR_SWAP_CONTRACT, SPOT_CONTRACT


_BASE_TS = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _request(order_type: str, *, target_qty: str = "1", side: str = "buy"):
    return FillRequest(
        order_id="order-4",
        side=side,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        target_qty=Decimal(target_qty),
        submitted_at_ts=1,
    )


@pytest.mark.parametrize("order_type", ["ioc", "post_only", "bounded_limit"])
def test_every_order_type_fails_closed_without_positive_volume(
    order_type: str,
) -> None:
    result = FillSimulator(
        instrument_contract=SPOT_CONTRACT,
        spot_buy_fee_asset="quote",
    ).simulate(
        _request(order_type),
        Decimal("100"),
        Decimal("0"),
    )

    assert result.fill_kind == "no_fill"
    assert result.filled_qty == 0
    assert "non-positive bar_volume" in result.notes


def test_ioc_quantity_is_capped_by_ohlcv_participation() -> None:
    result = FillSimulator(
        instrument_contract=SPOT_CONTRACT,
        max_volume_participation=Decimal("0.01"),
        spot_buy_fee_asset="quote",
    ).simulate(
        _request("ioc", target_qty="5"),
        Decimal("100"),
        Decimal("100"),
    )

    assert result.filled_qty == Decimal("1.00")
    assert result.avg_fill_price == Decimal("100.1")
    assert result.slippage_bps == 10.0
    assert result.fee_bps == 5.0
    assert "partial_fill" in result.notes


def test_bounded_limit_uses_capped_taker_fallback() -> None:
    result = FillSimulator(
        instrument_contract=SPOT_CONTRACT,
        max_volume_participation=Decimal("0.01"),
        spot_buy_fee_asset="quote",
    ).simulate(
        _request("bounded_limit", target_qty="5", side="sell"),
        Decimal("100"),
        Decimal("100"),
    )

    assert result.filled_qty == Decimal("1.00")
    assert result.avg_fill_price == Decimal("99.9")
    assert result.slippage_bps == 10.0
    assert result.fee_bps == 5.0
    assert result.fill_kind == "taker"


def test_post_only_probability_hit_still_respects_participation_cap() -> None:
    result = FillSimulator(
        instrument_contract=SPOT_CONTRACT,
        max_volume_participation=Decimal("0.01"),
        spot_buy_fee_asset="quote",
    ).simulate(
        _request("post_only", target_qty="3"),
        Decimal("100"),
        Decimal("100"),
    )

    assert result.fill_kind == "maker"
    assert result.filled_qty == Decimal("1.00")
    assert result.slippage_bps == 0.0
    assert "partial_fill" in result.notes


@pytest.mark.parametrize(
    ("config", "error"),
    [
        (
            BacktestConfig(
                instrument_contract=LINEAR_SWAP_CONTRACT,
                fill_model_version="legacy_full_fill_v1",  # type: ignore[arg-type]
            ),
            "Unsupported fill_model_version",
        ),
        (
            BacktestConfig(
                instrument_contract=LINEAR_SWAP_CONTRACT,
                max_volume_participation=Decimal("0"),
            ),
            "max_volume_participation",
        ),
        (
            BacktestConfig(
                instrument_contract=LINEAR_SWAP_CONTRACT,
                max_volume_participation=Decimal("1.01"),
            ),
            "max_volume_participation",
        ),
    ],
)
def test_unsupported_fill_contract_fails_before_loading_market_data(
    config: BacktestConfig,
    error: str,
) -> None:
    with patch(
        "aats.data_platform.replay.backtest.harness.load_gold_bars"
    ) as loader:
        with pytest.raises(ValueError, match=error):
            run_backtest(
                MagicMock(),
                config=config,
                start_ts=_BASE_TS,
                end_ts=_BASE_TS + timedelta(days=1),
            )

    loader.assert_not_called()


def test_cost_diagnostic_preserves_actual_fee_and_slippage_components() -> None:
    diagnostic = CostValidator().record(
        decision_id="d1",
        assumed_cost_bps=4.0,
        actual_cost_bps=6.0,
        actual_fee_bps=5.0,
        actual_slippage_bps=1.0,
        assumed_net_edge_bps=10.0,
    )

    assert diagnostic.actual_cost_bps == 6.0
    assert diagnostic.actual_fee_bps == 5.0
    assert diagnostic.actual_slippage_bps == 1.0
    assert diagnostic.cost_diff_bps == 2.0
    assert FILL_MODEL_VERSION == "ohlcv_participation_cap_contract_v3"
