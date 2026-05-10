from __future__ import annotations

import pytest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import HedgeOverlayDecision
from aats.schemas.strategy_runtime import StrategyLegIntent, StrategySleeveIntent


def test_retired_overlay_modes_are_not_valid_runtime_settings() -> None:
    for mode in ("protective", "opportunistic"):
        with pytest.raises(ValueError):
            AATSSettings.model_validate({"strategy_hedge_overlay_mode": mode})


def test_retired_strategy_families_remain_readable_in_historical_intents() -> None:
    sleeve = StrategySleeveIntent.model_validate(
        {
            "decision_id": "dec_retired",
            "family": "protective",
            "strategy_sleeve_id": "protective:BTC-USDT-SWAP",
            "symbol": "BTC-USDT-SWAP",
            "product_type": "derivatives",
            "margin_mode": "cross",
            "inventory_policy": "paired_inventory",
        }
    )
    leg = StrategyLegIntent.model_validate(
        {
            "symbol": "BTC-USDT-SWAP",
            "product_type": "derivatives",
            "side": "sell",
            "family": "opportunistic",
            "overlay_mode": "opportunistic",
        }
    )

    assert sleeve.family == "protective"
    assert leg.family == "opportunistic"
    assert leg.overlay_mode == "opportunistic"


def test_retired_overlay_decisions_remain_readable_for_historical_payloads() -> None:
    decision = HedgeOverlayDecision.model_validate(
        {
            "configured_mode": "protective",
            "effective_mode": "protective",
            "overlay_source": "protective",
        }
    )

    assert decision.configured_mode == "protective"
    assert decision.effective_mode == "protective"
