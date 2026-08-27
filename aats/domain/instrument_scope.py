"""Strict RDP instrument-scope classification from the explicit support set."""

from __future__ import annotations

from typing import Literal


SUPPORTED_SYMBOLS_SPOT = ("BTC-USDT", "ETH-USDT")
SUPPORTED_SYMBOLS_SWAP = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
SUPPORTED_SYMBOLS = SUPPORTED_SYMBOLS_SPOT + SUPPORTED_SYMBOLS_SWAP

INSTRUMENT_SCOPE_UNSUPPORTED_REASON = (
    "instrument_scope_unsupported_or_unproven"
)

InstrumentScope = Literal["spot", "swap", "unsupported"]

_SUPPORTED_SPOT = frozenset(SUPPORTED_SYMBOLS_SPOT)
_SUPPORTED_SWAP = frozenset(SUPPORTED_SYMBOLS_SWAP)


def classify_instrument_scope(symbol: str | None) -> InstrumentScope:
    """Classify only explicitly supported RDP instruments.

    Symbol shape and suffixes are not evidence of support. Unknown, empty and
    future instruments remain ``unsupported`` until the allowlist is reviewed.
    """

    normalized = str(symbol or "").strip().upper()
    if normalized in _SUPPORTED_SPOT:
        return "spot"
    if normalized in _SUPPORTED_SWAP:
        return "swap"
    return "unsupported"


__all__ = [
    "INSTRUMENT_SCOPE_UNSUPPORTED_REASON",
    "InstrumentScope",
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_SYMBOLS_SPOT",
    "SUPPORTED_SYMBOLS_SWAP",
    "classify_instrument_scope",
]
