from __future__ import annotations

import re
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitragePairDefinition

_DEFAULT_EXECUTION_MODES = (
    "spot_carry",
    "inventory_reverse_carry",
    "margin_reverse_carry",
)
_VALID_EXECUTION_MODES = {
    "spot_carry",
    "inventory_reverse_carry",
    "margin_reverse_carry",
    "inter_derivatives_spread",
}


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
    configured_pairs: list[ArbitragePairDefinition] = []
    for index, payload in enumerate(settings.smart_arbitrage_pair_definitions, start=1):
        normalized = _pair_from_payload(payload=payload, settings=settings, index=index)
        if normalized is not None:
            configured_pairs.append(normalized)
    configured_pairs = _resolve_pair_id_conflicts(configured_pairs)
    pairs: list[ArbitragePairDefinition] = []
    configured_scopes = {_pair_scope_key(pair) for pair in configured_pairs}
    default_pair = _default_pair(primary_symbol=primary_symbol)
    if default_pair is not None and _pair_scope_key(default_pair) not in configured_scopes:
        pairs.append(default_pair)
    pairs.extend(configured_pairs)
    deduped: list[ArbitragePairDefinition] = []
    seen_keys: dict[tuple[str, str], int] = {}
    for pair in pairs:
        key = _pair_scope_key(pair)
        if key in seen_keys:
            existing_index = seen_keys[key]
            existing = deduped[existing_index]
            deduped[existing_index] = _append_pair_metadata(
                existing,
                warning_codes=["smart_arbitrage_duplicate_pair_scope_ignored"],
                ignored_duplicate_scope_pairs=[
                    {
                        "pair_id": pair.pair_id,
                        "spot_symbol": pair.spot_symbol,
                        "hedge_symbol": pair.hedge_symbol,
                        "requested_pair_id": pair.metadata.get("requested_pair_id", pair.pair_id),
                    }
                ],
            )
            continue
        seen_keys[key] = len(deduped)
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
        execution_modes=_DEFAULT_EXECUTION_MODES,
        metadata={"source": "derived_primary_symbol"},
    )


def _pair_from_payload(*, payload: Any, settings: AATSSettings, index: int) -> ArbitragePairDefinition | None:
    if not isinstance(payload, dict):
        return None
    spot_symbol = str(payload.get("spot_symbol") or payload.get("primary_symbol") or "").strip().upper()
    hedge_symbol = str(payload.get("hedge_symbol") or payload.get("derivatives_symbol") or "").strip().upper()
    if not spot_symbol or not hedge_symbol:
        return None
    requested_pair_id = str(payload.get("pair_id") or _default_pair_id(spot_symbol=spot_symbol, hedge_symbol=hedge_symbol))
    raw_modes = payload.get("execution_modes")
    explicit_modes_configured = "execution_modes" in payload
    parsed_modes = (
        tuple(str(item).strip() for item in raw_modes if str(item).strip())
        if isinstance(raw_modes, (list, tuple))
        else ()
    )
    normalized_modes = tuple(
        mode for mode in parsed_modes if mode in _VALID_EXECUTION_MODES
    )
    invalid_modes = tuple(mode for mode in parsed_modes if mode not in _VALID_EXECUTION_MODES)
    configuration_warning_codes: list[str] = []
    configuration_error_codes: list[str] = []
    if explicit_modes_configured:
        if invalid_modes:
            configuration_warning_codes.append("smart_arbitrage_pair_execution_modes_partial_invalid")
        if not normalized_modes:
            configuration_error_codes.append("smart_arbitrage_pair_execution_modes_invalid")
    effective_execution_modes = normalized_modes if explicit_modes_configured else _DEFAULT_EXECUTION_MODES
    return ArbitragePairDefinition(
        pair_id=requested_pair_id,
        spot_symbol=spot_symbol,
        hedge_symbol=hedge_symbol,
        settle_currency=str(payload.get("settle_currency") or _settle_currency(hedge_symbol) or "").upper() or None,
        execution_modes=effective_execution_modes,
        metadata={
            "source": "pair_registry",
            "index": index,
            "priority_rank": payload.get("priority_rank", index),
            "requested_pair_id": requested_pair_id,
            "requested_execution_modes": list(parsed_modes) if explicit_modes_configured else [],
            "invalid_execution_modes": list(invalid_modes),
            "configuration_warning_codes": configuration_warning_codes,
            "configuration_error_codes": configuration_error_codes,
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


def _pair_scope_key(pair: ArbitragePairDefinition) -> tuple[str, str]:
    return (str(pair.spot_symbol).upper(), str(pair.hedge_symbol).upper())


def _resolve_pair_id_conflicts(pairs: list[ArbitragePairDefinition]) -> list[ArbitragePairDefinition]:
    resolved: list[ArbitragePairDefinition] = []
    seen_pair_ids: dict[str, tuple[str, str]] = {}
    pair_id_conflict_counts: dict[str, int] = {}
    for pair in pairs:
        pair_id = str(pair.pair_id)
        scope = _pair_scope_key(pair)
        prior_scope = seen_pair_ids.get(pair_id)
        if prior_scope is None or prior_scope == scope:
            seen_pair_ids.setdefault(pair_id, scope)
            resolved.append(pair)
            continue
        pair_id_conflict_counts[pair_id] = pair_id_conflict_counts.get(pair_id, 1) + 1
        effective_pair_id = f"{pair_id}__scope_conflict_{pair_id_conflict_counts[pair_id]}"
        resolved.append(
            pair.model_copy(
                update={
                    "pair_id": effective_pair_id,
                    "metadata": {
                        **pair.metadata,
                        "effective_pair_id": effective_pair_id,
                        "configuration_warning_codes": list(
                            dict.fromkeys(
                                [
                                    *pair.metadata.get("configuration_warning_codes", []),
                                    "smart_arbitrage_pair_id_conflict_renamed",
                                ]
                            )
                        ),
                        "pair_id_conflict_with_scope": {
                            "pair_id": pair_id,
                            "spot_symbol": prior_scope[0],
                            "hedge_symbol": prior_scope[1],
                        },
                    },
                }
            )
        )
    return resolved


def _append_pair_metadata(
    pair: ArbitragePairDefinition,
    *,
    warning_codes: list[str] | None = None,
    error_codes: list[str] | None = None,
    ignored_duplicate_scope_pairs: list[dict[str, Any]] | None = None,
) -> ArbitragePairDefinition:
    metadata = dict(pair.metadata or {})
    if warning_codes:
        metadata["configuration_warning_codes"] = list(
            dict.fromkeys([*metadata.get("configuration_warning_codes", []), *warning_codes])
        )
    if error_codes:
        metadata["configuration_error_codes"] = list(
            dict.fromkeys([*metadata.get("configuration_error_codes", []), *error_codes])
        )
    if ignored_duplicate_scope_pairs:
        metadata["ignored_duplicate_scope_pairs"] = [
            *metadata.get("ignored_duplicate_scope_pairs", []),
            *ignored_duplicate_scope_pairs,
        ]
    return pair.model_copy(update={"metadata": metadata})


def _settle_currency(symbol: str) -> str | None:
    normalized = str(symbol or "").upper()
    if "-USDT" in normalized:
        return "USDT"
    if "-USD" in normalized:
        return "USD"
    return None
