from __future__ import annotations

from decimal import Decimal
from typing import Any

from aats.schemas.common import utc_now


class DemoMarketDataProvider:
    """Generates deterministic synthetic market data for demo / local testing.

    Encapsulates all simulation-specific state (price deltas, tick counters)
    so that ``MarketDataGateway`` stays focused on real exchange integration.
    """

    _PRICE_DELTAS: tuple[Decimal, ...] = (
        Decimal("320.0"),
        Decimal("260.0"),
        Decimal("-380.0"),
        Decimal("-310.0"),
        Decimal("240.0"),
        Decimal("-270.0"),
    )

    def __init__(self, exchange_name: str) -> None:
        self.exchange_name = exchange_name
        self._tick_by_symbol: dict[str, int] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._hourly_open: dict[str, Decimal] = {}

    def build_payload(
        self,
        symbol: str,
        *,
        default_price: Decimal = Decimal("67250.0"),
    ) -> dict[str, Any]:
        """Build a synthetic market snapshot payload for *symbol*."""
        tick = self._tick_by_symbol.get(symbol, 0)
        previous_price = self._last_prices.get(symbol, default_price)
        delta = self._PRICE_DELTAS[tick % len(self._PRICE_DELTAS)]
        last_price = previous_price + delta
        high = max(previous_price, last_price) + Decimal("40.0")
        low = min(previous_price, last_price) - Decimal("40.0")

        # Use a rolling anchor for the 1h open so that the hourly candle
        # does not always show a directional bias from a fixed start price.
        # Reset the anchor every full cycle (6 ticks) of _PRICE_DELTAS.
        cycle_len = len(self._PRICE_DELTAS)
        if tick % cycle_len == 0:
            self._hourly_open.setdefault(symbol, previous_price)
            self._hourly_open[symbol] = previous_price
        hourly_open = self._hourly_open.get(symbol, previous_price)

        self._last_prices[symbol] = last_price
        self._tick_by_symbol[symbol] = tick + 1

        return {
            "symbol": symbol,
            "exchange": self.exchange_name,
            "snapshot_ts": utc_now(),
            "best_bid": last_price - Decimal("5.0"),
            "best_ask": last_price + Decimal("5.0"),
            "last_price": last_price,
            "bid_size": Decimal("1.25") + (Decimal(tick) * Decimal("0.05")),
            "ask_size": Decimal("1.10") + (Decimal(tick) * Decimal("0.04")),
            "volume_24h": Decimal("128500000.0") + (Decimal(tick) * Decimal("250000.0")),
            "kline_15m": {
                "open": previous_price,
                "high": high,
                "low": low,
                "close": last_price,
                "volume": Decimal("1250.0") + (Decimal(tick) * Decimal("50.0")),
            },
            "kline_1h": {
                "open": hourly_open,
                "high": max(hourly_open, high),
                "low": min(hourly_open, low),
                "close": last_price,
                "volume": Decimal("4800.0") + (Decimal(tick) * Decimal("125.0")),
            },
            "recent_trades": [
                {"price": last_price - Decimal("10.0"), "qty": Decimal("0.05"), "side": "buy"},
                {"price": last_price, "qty": Decimal("0.04"), "side": "buy" if delta >= 0 else "sell"},
                {"price": last_price + Decimal("10.0"), "qty": Decimal("0.03"), "side": "sell"},
            ],
            "orderbook_depth": {
                "bids": [
                    [last_price - Decimal("5.0"), Decimal("1.25")],
                    [last_price - Decimal("10.0"), Decimal("1.5")],
                ],
                "asks": [
                    [last_price + Decimal("5.0"), Decimal("1.10")],
                    [last_price + Decimal("10.0"), Decimal("1.35")],
                ],
            },
        }
