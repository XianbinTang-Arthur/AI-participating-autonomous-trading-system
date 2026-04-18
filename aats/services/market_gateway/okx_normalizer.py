from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.bootstrap.logging import get_logger
from aats.schemas.market import MarketSnapshot
from aats.services.portfolio_service.decimals import to_decimal

_logger = get_logger("aats.okx_normalizer")

# (symbol, channel) key for tracking per-instrument candle timestamps.
_CandleKey = tuple[str, str]


def _parse_ms_timestamp(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)


@dataclass(slots=True)
class CandleGap:
    symbol: str
    channel: str
    last_ts: datetime
    new_ts: datetime
    expected_interval_seconds: int


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
    raw_messages: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=50))


class OKXMarketSnapshotNormalizer:
    _CANDLE_INTERVALS: dict[str, int] = {
        "candle1m": 60,
        "candle3m": 180,
        "candle5m": 300,
        "candle15m": 900,
        "candle30m": 1800,
        "candle1H": 3600,
        "candle2H": 7200,
        "candle4H": 14400,
        "candle6H": 21600,
        "candle12H": 43200,
        "candle1D": 86400,
        "candle1W": 604800,
    }

    def __init__(self, exchange_name: str = "OKX") -> None:
        self.exchange_name = exchange_name
        self._last_candle_ts: dict[_CandleKey, datetime] = {}
        self._detected_gaps: list[CandleGap] = []

    def drain_detected_gaps(self) -> list[CandleGap]:
        gaps = self._detected_gaps
        self._detected_gaps = []
        return gaps

    # P1-7：OKX 合约符号格式校验。OKX 的 instId 必须是 3 段连字符分隔：
    #   - 现货: BASE-QUOTE         (e.g. BTC-USDT)
    #   - 合约: BASE-QUOTE-SWAP    (e.g. BTC-USDT-SWAP)
    #   - 交割: BASE-QUOTE-YYMMDD  (e.g. BTC-USDT-251226)
    # 旧代码仅做 .upper()，不校验格式，"BTC-USDT" 当作现货被接受但订阅的是合约
    # 会静默失败。这里加严格校验，非法符号立即抛 ValueError，上游立即可见。
    _VALID_OKX_CONTRACT_SUFFIXES: frozenset[str] = frozenset({"SWAP"})

    @classmethod
    def okx_inst_id(cls, symbol: str) -> str:
        return cls._validate_inst_id(symbol.upper())

    @classmethod
    def internal_symbol(cls, inst_id: str) -> str:
        return cls._validate_inst_id(inst_id.upper())

    @classmethod
    def _validate_inst_id(cls, inst_id: str) -> str:
        parts = inst_id.split("-")
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(f"invalid_okx_inst_id: {inst_id!r} — expected BASE-QUOTE[-SUFFIX]")
        if len(parts) == 3:
            suffix = parts[2]
            # 允许 SWAP 永续、YYMMDD 交割合约（6 位数字）
            if suffix not in cls._VALID_OKX_CONTRACT_SUFFIXES and not (len(suffix) == 6 and suffix.isdigit()):
                raise ValueError(
                    f"invalid_okx_inst_id: {inst_id!r} — unsupported contract suffix {suffix!r}"
                )
        if not parts[0] or not parts[1]:
            raise ValueError(f"invalid_okx_inst_id: {inst_id!r} — empty base/quote segment")
        return inst_id

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
            self._check_candle_gap(symbol=symbol, channel=channel, new_ts=candle.snapshot_ts)
            if channel == "candle15m":
                state.candle_15m = candle
            else:
                state.candle_1h = candle
        else:
            return []

        snapshot = self._build_snapshot(state)
        return [snapshot] if snapshot is not None else []

    def build_snapshot_from_rest_payloads(
        self,
        *,
        symbol: str,
        ticker_payload: dict[str, Any],
        candle_15m_payload: list[str],
        candle_1h_payload: list[str],
    ) -> MarketSnapshot:
        ticker = self._parse_ticker(symbol=symbol, payload=ticker_payload)
        sanity_reason = self._sanity_check_ticker(ticker)
        if sanity_reason is not None:
            raise ValueError(
                f"rest_snapshot_ticker_sanity_failed: symbol={symbol} reason={sanity_reason}"
            )
        state = OKXInstrumentMarketState(
            symbol=symbol,
            ticker=ticker,
            candle_15m=self._parse_candle(channel="candle15m", payload=candle_15m_payload),
            candle_1h=self._parse_candle(channel="candle1H", payload=candle_1h_payload),
        )
        snapshot = self._build_snapshot(state)
        if snapshot is None:
            raise ValueError(
                f"rest_snapshot_build_failed: symbol={symbol} "
                f"ticker={'present' if state.ticker else 'missing'} "
                f"candle_15m={'present' if state.candle_15m else 'missing'} "
                f"candle_1h={'present' if state.candle_1h else 'missing'}"
            )
        return snapshot

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
        if len(payload) < 6:
            raise ValueError(
                f"okx_candle_payload_too_short: channel={channel} len={len(payload)}, expected>=6"
            )
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

    def _check_candle_gap(self, *, symbol: str, channel: str, new_ts: datetime) -> None:
        key = (symbol, channel)
        last_ts = self._last_candle_ts.get(key)
        if last_ts is not None and new_ts == last_ts:
            return
        # R6-M1：乱序 / 重放场景 new_ts < last_ts 时原先无条件覆盖
        # _last_candle_ts，把跟踪状态回退到更旧 ts；且 gap_seconds < 0
        # 不会触发 detected_gaps，行情时序异常静默丢失。这里明确拒绝回退：
        # 不更新 _last_candle_ts，但落 warning 让下游 ops 可见。
        if last_ts is not None and new_ts < last_ts:
            _logger.warning(
                "okx_candle_ts_regression symbol=%s channel=%s "
                "last_ts=%s new_ts=%s regression_seconds=%s",
                symbol, channel, last_ts.isoformat(), new_ts.isoformat(),
                (last_ts - new_ts).total_seconds(),
            )
            return
        self._last_candle_ts[key] = new_ts
        if last_ts is None:
            return
        expected_interval = self._CANDLE_INTERVALS.get(channel)
        if expected_interval is None:
            return
        gap_seconds = (new_ts - last_ts).total_seconds()
        if gap_seconds > expected_interval * 1.5:
            self._detected_gaps.append(CandleGap(
                symbol=symbol,
                channel=channel,
                last_ts=last_ts,
                new_ts=new_ts,
                expected_interval_seconds=expected_interval,
            ))

    def _build_snapshot(self, state: OKXInstrumentMarketState) -> MarketSnapshot | None:
        if state.ticker is None or state.candle_15m is None or state.candle_1h is None:
            return None

        ticker = state.ticker
        sanity_reason = self._sanity_check_ticker(ticker)
        if sanity_reason is not None:
            return None

        snapshot_ts = max(
            ticker.snapshot_ts,
            state.candle_15m.snapshot_ts,
            state.candle_1h.snapshot_ts,
        )
        # Build a minimal orderbook from ticker top-of-book so that
        # downstream LiquidityAnalyzer has at least one level of depth
        # rather than operating on an empty dict.
        orderbook_depth: dict[str, list[list[Decimal]]] = {
            "bids": [[ticker.best_bid, ticker.bid_size]],
            "asks": [[ticker.best_ask, ticker.ask_size]],
        }
        return MarketSnapshot(
            symbol=state.symbol,
            exchange=self.exchange_name,
            snapshot_ts=snapshot_ts,
            best_bid=ticker.best_bid,
            best_ask=ticker.best_ask,
            last_price=ticker.last_price,
            bid_size=ticker.bid_size,
            ask_size=ticker.ask_size,
            volume_24h=ticker.volume_24h,
            kline_15m=state.candle_15m.to_market_kline(),
            kline_1h=state.candle_1h.to_market_kline(),
            recent_trades=[],
            orderbook_depth=orderbook_depth,
        )

    @staticmethod
    def _sanity_check_ticker(ticker: OKXTickerState) -> str | None:
        """Return ``None`` if the ticker is valid, or a reason string if invalid."""
        if ticker.last_price <= 0:
            reason = f"last_price_non_positive:{ticker.last_price}"
            _logger.warning("okx_ticker_sanity_fail: %s symbol=%s", reason, ticker.symbol)
            return reason
        if ticker.best_bid <= 0 or ticker.best_ask <= 0:
            reason = f"bid_ask_non_positive:bid={ticker.best_bid},ask={ticker.best_ask}"
            _logger.warning("okx_ticker_sanity_fail: %s symbol=%s", reason, ticker.symbol)
            return reason
        if ticker.best_bid > ticker.best_ask:
            reason = f"crossed_spread:bid={ticker.best_bid}>ask={ticker.best_ask}"
            _logger.warning("okx_ticker_sanity_fail: %s symbol=%s", reason, ticker.symbol)
            return reason
        return None
