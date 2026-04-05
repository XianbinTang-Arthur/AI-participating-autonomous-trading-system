from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.strategy_runtime import StrategyLegIntent
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.strategy_engines.smart_arbitrage.cost_model import (
    _resolve_required_hedge_margin_mode,
)
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitrageOpportunity, ArbitragePairDefinition


def build_legs(
    *,
    settings: AATSSettings,
    pair: ArbitragePairDefinition,
    opportunity: ArbitrageOpportunity,
    hedge_margin_mode: str | None = None,
    require_explicit_hedge_margin_mode: bool = True,
    account_spot_qty: Decimal,
    account_hedge_qty: Decimal,
    sleeve_spot_qty: Decimal,
    sleeve_hedge_qty: Decimal,
    spot_price: Decimal,
    hedge_price: Decimal,
) -> list[StrategyLegIntent]:
    spot_target_qty = to_decimal(opportunity.target_spot_qty)
    hedge_target_qty = to_decimal(opportunity.target_hedge_qty)
    spot_delta_qty = spot_target_qty - to_decimal(sleeve_spot_qty)
    hedge_delta_qty = hedge_target_qty - to_decimal(sleeve_hedge_qty)
    spot_account_target_qty = to_decimal(account_spot_qty) + spot_delta_qty
    hedge_account_target_qty = to_decimal(account_hedge_qty) + hedge_delta_qty
    spot_margin_mode = (
        settings.smart_arbitrage_margin_short_spot_margin_mode
        if opportunity.execution_mode == "margin_reverse_carry"
        else "cash"
    )
    spot_note = {
        "inventory_reverse_carry": "Inventory-backed arbitrage spot leg driven by sleeve inventory truth.",
        "margin_reverse_carry": "Borrow-backed arbitrage spot leg uses margin sell semantics.",
    }.get(opportunity.execution_mode, "Arbitrage spot leg driven by sleeve inventory truth.")
    hedge_note = {
        "inventory_reverse_carry": "Derivatives hedge leg offsets the inventory-backed reverse carry.",
        "margin_reverse_carry": "Derivatives hedge leg offsets the borrow-backed reverse carry.",
    }.get(opportunity.execution_mode, "Arbitrage hedge leg driven by sleeve inventory truth.")
    resolved_hedge_margin_mode = _resolve_required_hedge_margin_mode(
        hedge_margin_mode=hedge_margin_mode,
        require_explicit_hedge_margin_mode=require_explicit_hedge_margin_mode,
    )
    hedge_target_leverage = min(
        max(float(settings.smart_arbitrage_hedge_target_leverage), 1.0),
        max(float(settings.max_target_leverage), 1.0),
    )
    return [
        StrategyLegIntent(
            symbol=pair.spot_symbol,
            product_type="spot",
            side="buy" if spot_delta_qty >= 0 else "sell",
            role="primary",
            margin_mode=spot_margin_mode,  # type: ignore[arg-type]
            target_leverage=1.0,
            current_position_qty=to_decimal(account_spot_qty),
            target_position_qty=spot_account_target_qty,
            delta_position_qty=spot_delta_qty,
            reference_price=spot_price,
            execution_compatible=True,
            pair_id=pair.pair_id,
            opportunity_kind=opportunity.opportunity_kind,
            execution_mode=opportunity.execution_mode,
            state_phase=opportunity.state_phase,
            note=spot_note,
        ),
        StrategyLegIntent(
            symbol=pair.hedge_symbol,
            product_type=pair.hedge_product_type,
            side="buy" if hedge_delta_qty >= 0 else "sell",
            role="hedge",
            margin_mode=resolved_hedge_margin_mode,
            target_leverage=hedge_target_leverage,
            current_position_qty=to_decimal(account_hedge_qty),
            target_position_qty=hedge_account_target_qty,
            delta_position_qty=hedge_delta_qty,
            reference_price=hedge_price,
            execution_compatible=True,
            pair_id=pair.pair_id,
            opportunity_kind=opportunity.opportunity_kind,
            execution_mode=opportunity.execution_mode,
            state_phase=opportunity.state_phase,
            note=hedge_note,
        ),
    ]


