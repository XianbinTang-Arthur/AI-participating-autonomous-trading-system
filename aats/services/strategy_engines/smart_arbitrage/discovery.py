from __future__ import annotations

from decimal import Decimal

from aats.schemas.market import MarketSnapshot
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitragePairDefinition


def load_market_pair(
    *,
    pair: ArbitragePairDefinition,
    market_snapshot_loader,
) -> tuple[MarketSnapshot | None, MarketSnapshot | None]:
    return market_snapshot_loader(pair.spot_symbol), market_snapshot_loader(pair.hedge_symbol)


def basis_bps(*, spot_snapshot: MarketSnapshot, hedge_snapshot: MarketSnapshot) -> Decimal:
    spot_price = to_decimal(spot_snapshot.last_price)
    hedge_price = to_decimal(hedge_snapshot.last_price)
    if abs(spot_price) <= EPSILON_DECIMAL_12:
        return Decimal("0")
    return ((hedge_price - spot_price) / spot_price) * Decimal("10000")
