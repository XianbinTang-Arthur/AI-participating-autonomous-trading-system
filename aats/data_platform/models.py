"""Shared data models and table-name resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_SYMBOLS_SPOT = ("BTC-USDT", "ETH-USDT")
SUPPORTED_SYMBOLS_SWAP = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
SUPPORTED_SYMBOLS = SUPPORTED_SYMBOLS_SPOT + SUPPORTED_SYMBOLS_SWAP
SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "1H")
FUNDING_SYMBOLS = SUPPORTED_SYMBOLS_SWAP


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Table name helpers
# ---------------------------------------------------------------------------

def instrument_type_for_symbol(symbol: str) -> str:
    """Return 'spot' or 'swap' based on symbol suffix."""
    return "swap" if symbol.upper().endswith("-SWAP") else "spot"


def candle_table_name(layer: str, symbol: str, timeframe: str) -> str:
    """Resolve the fully-qualified candle table name.

    >>> candle_table_name("silver", "BTC-USDT-SWAP", "15m")
    'silver.market_swap_candles_15m'
    """
    inst = instrument_type_for_symbol(symbol)
    tf = timeframe.lower()
    return f"{layer}.market_{inst}_candles_{tf}"


def funding_table_name(layer: str) -> str:
    return f"{layer}.market_swap_funding"


def replay_bar_table_name(symbol: str, timeframe: str) -> str:
    inst = instrument_type_for_symbol(symbol)
    tf = timeframe.lower()
    return f"gold.market_{inst}_replay_bars_{tf}"


# ---------------------------------------------------------------------------
# Row data-classes (thin transfer objects)
# ---------------------------------------------------------------------------

@dataclass
class CandleRow:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol: Decimal | None = None
    vol_ccy: Decimal | None = None
    vol_quote: Decimal | None = None
    confirm: bool = True
    raw_symbol: str | None = None
    raw_ts: str | None = None


@dataclass
class FundingRow:
    symbol: str
    ts: datetime
    funding_rate: Decimal
    inst_type: str | None = None
    formula_type: str | None = None
    method: str | None = None
    realized_rate: Decimal | None = None
    raw_symbol: str | None = None
    raw_ts: str | None = None


@dataclass
class ReplayBarRow:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    quote_volume: Decimal | None = None
    is_closed: bool = True
    aligned_funding_rate: Decimal | None = None
    funding_source_ts: datetime | None = None
