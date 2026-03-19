from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.schemas.market import MarketSnapshot
from aats.services.portfolio_service.decimals import to_decimal


def _parse_ms_timestamp(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)


@dataclass(slots=True)
class OKXTickerState:
    symbol: str
    snapshot_ts: datetime
    best_bid: Decimal
    best_ask: Decimal
    last_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    volume_24h: Decimal


@dataclass(slots=True)
class OKXCandleState:
    channel: str
    snapshot_ts: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    confirm: bool

    def to_market_kline(self) -> dict[str, Decimal]:
        return {
            "open": self.open_price,
            "high": self.high_price,
            "low": self.low_price,
            "close": self.close_price,
            "volume": self.volume,
        }


@dataclass(slots=True)
class OKXInstrumentMarketState:
    symbol: str
    ticker: OKXTickerState | None = None
    candle_15m: OKXCandleState | None = None
    candle_1h: OKXCandleState | None = None
    raw_messages: list[dict[str, Any]] = field(default_factory=list)


class OKXMarketSnapshotNormalizer:
    def __init__(self, exchange_name: str = "OKX") -> None:
        self.exchange_name = exchange_name

    @staticmethod
    def okx_inst_id(symbol: str) -> str:
        return symbol.upper()

    @staticmethod
    def internal_symbol(inst_id: str) -> str:
        return inst_id.upper()

    def apply_message(
        self,
        *,
        message: dict[str, Any],
        states: dict[str, OKXInstrumentMarketState],
    ) -> list[MarketSnapshot]:
        arg = message.get("arg")
        if not isinstance(arg, dict):
            return []

        channel = str(arg.get("channel", ""))
        inst_id = str(arg.get("instId", ""))
        if not channel or not inst_id:
            return []

        symbol = self.internal_symbol(inst_id)
        state = states.setdefault(symbol, OKXInstrumentMarketState(symbol=symbol))
        state.raw_messages.append(message)
        data = message.get("data")
        if not isinstance(data, list) or not data:
            return []

        if channel == "tickers":
            state.ticker = self._parse_ticker(symbol=symbol, payload=data[0])
        elif channel in {"candle15m", "candle1H"}:
            candle = self._parse_candle(channel=channel, payload=data[0])
            if channel == "candle15m":
                state.candle_15m = candle
            else:
                state.candle_1h = candle
        else:
            return []

        snapshot = self._build_snapshot(state)
        return [snapshot] if snapshot is not None else []

    def _parse_ticker(self, *, symbol: str, payload: dict[str, Any]) -> OKXTickerState:
        return OKXTickerState(
            symbol=symbol,
            snapshot_ts=_parse_ms_timestamp(str(payload["ts"])),
            best_bid=to_decimal(payload["bidPx"]),
            best_ask=to_decimal(payload["askPx"]),
            last_price=to_decimal(payload["last"]),
            bid_size=to_decimal(payload.get("bidSz", 0)),
            ask_size=to_decimal(payload.get("askSz", 0)),
            volume_24h=to_decimal(payload.get("vol24h", 0)),
        )

    def _parse_candle(self, *, channel: str, payload: list[str]) -> OKXCandleState:
        return OKXCandleState(
            channel=channel,
            snapshot_ts=_parse_ms_timestamp(str(payload[0])),
            open_price=to_decimal(payload[1]),
            high_price=to_decimal(payload[2]),
            low_price=to_decimal(payload[3]),
            close_price=to_decimal(payload[4]),
            volume=to_decimal(payload[5]),
            confirm=str(payload[8]) == "1" if len(payload) > 8 else False,
        )

    def _build_snapshot(self, state: OKXInstrumentMarketState) -> MarketSnapshot | None:
        if state.ticker is None or state.candle_15m is None or state.candle_1h is None:
            return None

        snapshot_ts = max(
            state.ticker.snapshot_ts,
            state.candle_15m.snapshot_ts,
            state.candle_1h.snapshot_ts,
        )
        return MarketSnapshot(
            symbol=state.symbol,
            exchange=self.exchange_name,
            snapshot_ts=snapshot_ts,
            best_bid=state.ticker.best_bid,
            best_ask=state.ticker.best_ask,
            last_price=state.ticker.last_price,
            bid_size=state.ticker.bid_size,
            ask_size=state.ticker.ask_size,
            volume_24h=state.ticker.volume_24h,
            kline_15m=state.candle_15m.to_market_kline(),
            kline_1h=state.candle_1h.to_market_kline(),
            recent_trades=[],
            orderbook_depth={},
        )
