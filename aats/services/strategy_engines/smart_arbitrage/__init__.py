from __future__ import annotations

from aats.services.strategy_engines.smart_arbitrage.engine import SmartArbitrageStrategyEngine as _Engine
from aats.services.strategy_engines.smart_arbitrage.pair_registry import (
    configured_market_symbols,
    derived_derivatives_symbol as _derived_derivatives_symbol,
    derived_spot_symbol as _derived_spot_symbol,
)


class SmartArbitrageStrategyEngine:
    def __init__(self, **kwargs) -> None:
        self._impl = _Engine(**kwargs)

    def evaluate(self, engine_input):
        return self._impl.evaluate(engine_input)

__all__ = [
    "SmartArbitrageStrategyEngine",
    "_derived_derivatives_symbol",
    "_derived_spot_symbol",
    "configured_market_symbols",
]
