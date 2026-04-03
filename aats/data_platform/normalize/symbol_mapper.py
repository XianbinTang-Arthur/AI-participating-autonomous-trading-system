"""Canonical symbol mapping.

Both file sources and API sources must use the same canonical symbols.
"""

from __future__ import annotations

# Phase 1 frozen symbol whitelist.
# This covers only the 4 instruments in scope for Phase 1.
# It is NOT a general-purpose symbol registry — future phases should
# replace this with a database-driven lookup or config-file mapping.
_CANONICAL_MAP: dict[str, str] = {
    # Spot
    "BTC-USDT": "BTC-USDT",
    "ETH-USDT": "ETH-USDT",
    # Swap
    "BTC-USDT-SWAP": "BTC-USDT-SWAP",
    "ETH-USDT-SWAP": "ETH-USDT-SWAP",
}


def to_canonical_symbol(raw_symbol: str) -> str:
    """Map a raw symbol string to the canonical form.

    Raises ValueError if the symbol is not recognised.
    """
    key = raw_symbol.strip().upper()
    canonical = _CANONICAL_MAP.get(key)
    if canonical is None:
        raise ValueError(f"Unknown symbol: {raw_symbol!r}")
    return canonical


def is_swap(symbol: str) -> bool:
    return symbol.strip().upper().endswith("-SWAP")


def instrument_type(symbol: str) -> str:
    return "swap" if is_swap(symbol) else "spot"
