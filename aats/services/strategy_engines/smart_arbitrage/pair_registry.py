from __future__ import annotations

import re
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitragePairDefinition


def derived_spot_symbol(symbol: str | None) -> str | None:
    normalized = str(symbol or "").upper()
    if not normalized:
        return None
    if normalized.endswith("-SWAP"):
        return normalized[:-5]
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return normalized[: -(len(tail) + 1)]
    return normalized


def derived_derivatives_symbol(symbol: str | None) -> str | None:
    normalized = str(symbol or "").upper()
    if not normalized:
        return None
    if normalized.endswith("-SWAP"):
        return normalized
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return normalized
    return f"{normalized}-SWAP"


def configured_market_symbols(settings: AATSSettings, primary_symbol: str) -> set[str]:
    symbols = {
        symbol
        for symbol in (
            primary_symbol,
            derived_spot_symbol(primary_symbol),
            derived_derivatives_symbol(primary_symbol),
        )
        if str(symbol or "").strip()
    }
    for pair in load_pair_definitions(settings=settings, primary_symbol=primary_symbol):
        symbols.add(pair.spot_symbol)
        symbols.add(pair.hedge_symbol)
    return {str(symbol).upper() for symbol in symbols if str(symbol).strip()}


def load_pair_definitions(*, settings: AATSSettings, primary_symbol: str) -> list[ArbitragePairDefinition]:
    pairs: list[ArbitragePairDefinition] = []
    default_pair = _default_pair(primary_symbol=primary_symbol)
    if default_pair is not None:
        pairs.append(default_pair)
    for index, payload in enumerate(settings.smart_arbitrage_pair_definitions, start=1):
        normalized = _pair_from_payload(payload=payload, settings=settings, index=index)
        if normalized is not None:
            pairs.append(normalized)
    deduped: list[ArbitragePairDefinition] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for pair in pairs:
        key = (pair.pair_id, pair.spot_symbol, pair.hedge_symbol)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(pair)
    return deduped


def _default_pair(*, primary_symbol: str) -> ArbitragePairDefinition | None:
    spot_symbol = derived_spot_symbol(primary_symbol)
    hedge_symbol = derived_derivatives_symbol(primary_symbol)
    if not spot_symbol or not hedge_symbol:
        return None
    return ArbitragePairDefinition(
        pair_id=_default_pair_id(spot_symbol=spot_symbol, hedge_symbol=hedge_symbol),
        spot_symbol=str(spot_symbol).upper(),
        hedge_symbol=str(hedge_symbol).upper(),
        settle_currency=_settle_currency(str(hedge_symbol).upper()),
        metadata={"source": "derived_primary_symbol"},
    )


def _pair_from_payload(*, payload: Any, settings: AATSSettings, index: int) -> ArbitragePairDefinition | None:
    if not isinstance(payload, dict):
        return None
    spot_symbol = str(payload.get("spot_symbol") or payload.get("primary_symbol") or "").strip().upper()
    hedge_symbol = str(payload.get("hedge_symbol") or payload.get("derivatives_symbol") or "").strip().upper()
    if not spot_symbol or not hedge_symbol:
        return None
    raw_modes = payload.get("execution_modes")
    execution_modes = (
        tuple(str(item).strip() for item in raw_modes if str(item).strip())
        if isinstance(raw_modes, (list, tuple))
        else ()
    )
    normalized_modes = tuple(
        mode
        for mode in execution_modes
        if mode in {"spot_carry", "inventory_reverse_carry", "margin_reverse_carry", "inter_derivatives_spread"}
    )
    return ArbitragePairDefinition(
        pair_id=str(payload.get("pair_id") or _default_pair_id(spot_symbol=spot_symbol, hedge_symbol=hedge_symbol)),
        spot_symbol=spot_symbol,
        hedge_symbol=hedge_symbol,
        settle_currency=str(payload.get("settle_currency") or _settle_currency(hedge_symbol) or "").upper() or None,
        execution_modes=normalized_modes or ("spot_carry", "inventory_reverse_carry"),
        metadata={
            "source": "pair_registry",
            "index": index,
            "priority_rank": payload.get("priority_rank", index),
            **{
                str(key): value
                for key, value in payload.items()
                if key not in {"pair_id", "spot_symbol", "primary_symbol", "hedge_symbol", "derivatives_symbol"}
            },
        },
    )


def _default_pair_id(*, spot_symbol: str, hedge_symbol: str) -> str:
    text = f"{spot_symbol}__{hedge_symbol}".lower()
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_") or "smart_arbitrage_pair"


def _settle_currency(symbol: str) -> str | None:
    normalized = str(symbol or "").upper()
    if "-USDT" in normalized:
        return "USDT"
    if "-USD" in normalized:
        return "USD"
    return None
