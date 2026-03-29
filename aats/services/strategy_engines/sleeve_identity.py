from __future__ import annotations

import hashlib

from aats.schemas.common import utc_now
from aats.schemas.strategy_runtime import StrategyFamily, StrategyInventoryPolicy, StrategySleeveRecord
from aats.schemas.system import MarginModelType, ProductType


def normalized_symbol_scope(*symbols: object) -> tuple[str, ...]:
    return tuple(sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()}))


def build_strategy_sleeve_id(
    *,
    family: StrategyFamily,
    primary_symbol: str,
    product_scope: ProductType,
    margin_scope: MarginModelType,
    symbol_scope: tuple[str, ...],
) -> str:
    normalized_scope = normalized_symbol_scope(*symbol_scope)
    seed = "|".join(
        (
            str(family),
            str(primary_symbol).upper(),
            str(product_scope),
            str(margin_scope),
            ",".join(normalized_scope),
        )
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    family_prefix = str(family).replace("_", "-")
    return f"sleeve-{family_prefix}-{digest}"


def inventory_policy_for_family(family: StrategyFamily) -> StrategyInventoryPolicy:
    if family == "smart_arbitrage":
        return "paired_inventory"
    if family == "directional":
        return "account_net_inventory"
    return "inventory_accumulation"


def build_strategy_sleeve_record(
    *,
    sleeve_id: str,
    family: StrategyFamily,
    primary_symbol: str,
    product_scope: ProductType,
    margin_scope: MarginModelType,
    symbol_scope: tuple[str, ...],
) -> StrategySleeveRecord:
    now = utc_now()
    normalized_symbols = normalized_symbol_scope(*symbol_scope)
    return StrategySleeveRecord(
        sleeve_id=sleeve_id,
        family=family,
        name=f"{family}:{str(primary_symbol).upper()}",
        product_scope=product_scope,
        margin_scope=margin_scope,
        symbol_scope=normalized_symbols,
        automatic_enabled=True,
        inventory_policy=inventory_policy_for_family(family),
        status="active",
        metadata={
            "primary_symbol": str(primary_symbol).upper(),
            "symbol_scope": list(normalized_symbols),
        },
        created_at=now,
        updated_at=now,
    )
