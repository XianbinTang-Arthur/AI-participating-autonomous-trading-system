from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from aats.schemas.common import utc_now
from aats.schemas.portfolio import PortfolioSnapshot, PortfolioSnapshotOrigin, Position
from aats.services.portfolio_service.decimals import is_effectively_zero, quantize_decimal, to_decimal
from aats.services.portfolio_service.position_keys import exposure_side_from_quantity
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator

if TYPE_CHECKING:
    from aats.services.portfolio_service.positions import PortfolioState


class PortfolioSnapshotBuilder:
    def __init__(self, *, pnl_calculator: PortfolioPnLCalculator) -> None:
        self.pnl_calculator = pnl_calculator

    def build(
        self,
        *,
        state: "PortfolioState",
        price_provider: Callable[[str], Decimal],
        decision_id: str | None = None,
        source_intent_id: str | None = None,
        source_fill_id: str | None = None,
        snapshot_origin: PortfolioSnapshotOrigin = "fill_derived",
    ) -> PortfolioSnapshot:
        positions: list[Position] = []
        gross_exposure = Decimal("0")
        net_exposure = Decimal("0")
        unrealized_pnl = Decimal("0")
        margin_usage = Decimal("0")
        spot_marked_value = Decimal("0")
        derivatives_unrealized_pnl = Decimal("0")
        other_asset_equity = Decimal("0")
        tracked_spot_base_currencies: set[str] = set()

        for position_key, record in state.positions.items():
            mark_price = to_decimal(price_provider(record.symbol))
            position_qty = to_decimal(record.quantity)
            avg_entry_price = to_decimal(record.avg_entry_price)
            position_notional = position_qty * mark_price
            position_unrealized = self.pnl_calculator.unrealized_pnl(
                position_qty=position_qty,
                avg_entry_price=avg_entry_price,
                mark_price=mark_price,
            )
            estimated_margin_allocated = (
                abs(position_notional) / to_decimal(max(record.target_leverage, 1.0))
                if record.product_type == "derivatives"
                else Decimal("0")
            )
            margin_source = "exchange" if str(record.margin_source or "").lower() == "exchange" else "estimated"
            resolved_margin_allocated = (
                to_decimal(record.margin_allocated)
                if margin_source == "exchange"
                else estimated_margin_allocated
            )
            resolved_maintenance_margin = (
                to_decimal(record.maintenance_margin)
                if margin_source == "exchange"
                else Decimal("0")
            )
            resolved_margin_ratio = (
                to_decimal(record.margin_ratio)
                if margin_source == "exchange" and record.margin_ratio is not None
                else None
            )
            resolved_liquidation_price = (
                to_decimal(record.liquidation_price)
                if margin_source == "exchange" and record.liquidation_price is not None
                else None
            )
            positions.append(
                Position(
                    symbol=record.symbol,
                    position_key=position_key,
                    position_qty=quantize_decimal(position_qty),
                    position_notional=quantize_decimal(position_notional),
                    avg_entry_price=quantize_decimal(avg_entry_price),
                    unrealized_pnl=quantize_decimal(position_unrealized),
                    product_type=record.product_type,  # type: ignore[arg-type]
                    exposure_side=record.exposure_side or exposure_side_from_quantity(record.quantity),
                    target_leverage=record.target_leverage,
                    margin_mode=record.margin_mode,  # type: ignore[arg-type]
                    position_mode=record.position_mode,  # type: ignore[arg-type]
                    pos_side=record.pos_side,  # type: ignore[arg-type]
                    instrument_family=record.instrument_family,
                    settle_currency=record.settle_currency,
                    margin_allocated=quantize_decimal(resolved_margin_allocated),
                    maintenance_margin=quantize_decimal(resolved_maintenance_margin),
                    margin_ratio=None if resolved_margin_ratio is None else quantize_decimal(resolved_margin_ratio),
                    liquidation_price=(
                        None
                        if resolved_liquidation_price is None
                        else quantize_decimal(resolved_liquidation_price)
                    ),
                    margin_source=margin_source,  # type: ignore[arg-type]
                )
            )
            gross_exposure += abs(position_notional)
            net_exposure += position_notional
            unrealized_pnl += position_unrealized
            if record.product_type == "derivatives":
                derivatives_unrealized_pnl += position_unrealized
                margin_usage += resolved_margin_allocated
            else:
                spot_marked_value += position_notional
                base_currency, _quote_currency = self._symbol_currencies(record.symbol)
                if base_currency is not None:
                    tracked_spot_base_currencies.add(base_currency)

        balances = dict(state.balances)
        balances = {currency: to_decimal(balance) for currency, balance in balances.items()}
        cash_equity = balances.get("USDT", Decimal("0"))
        for currency, balance in balances.items():
            if currency == "USDT" or currency in tracked_spot_base_currencies or is_effectively_zero(balance):
                continue
            implied_symbol = f"{currency}-USDT"
            implied_price = to_decimal(price_provider(implied_symbol))
            if implied_price > 0:
                other_asset_equity += balance * implied_price
        collateral_value = cash_equity + spot_marked_value + other_asset_equity
        total_equity = collateral_value + derivatives_unrealized_pnl
        return PortfolioSnapshot(
            decision_id=decision_id,
            source_intent_id=source_intent_id,
            source_fill_id=source_fill_id,
            snapshot_origin=snapshot_origin,
            snapshot_ts=utc_now(),
            balances={currency: quantize_decimal(balance) for currency, balance in balances.items()},
            positions=positions,
            cost_basis={
                position_key: quantize_decimal(record.avg_entry_price)
                for position_key, record in state.positions.items()
            },
            realized_pnl=quantize_decimal(state.realized_pnl),
            unrealized_pnl=quantize_decimal(unrealized_pnl),
            total_equity=quantize_decimal(total_equity),
            gross_exposure=quantize_decimal(gross_exposure),
            net_exposure=quantize_decimal(net_exposure),
            risk_budget_usage={
                "gross_exposure": quantize_decimal(gross_exposure),
                "margin_usage": quantize_decimal(margin_usage),
            },
            product_type=(
                "derivatives"
                if any(record.product_type == "derivatives" for record in state.positions.values())
                else state.default_product_type
            ),
            margin_mode=state.default_margin_mode,  # type: ignore[arg-type]
            margin_usage=quantize_decimal(margin_usage),
            leverage_profile={
                position_key: record.target_leverage for position_key, record in state.positions.items()
            },
            cash_equity=quantize_decimal(cash_equity),
            spot_asset_equity=quantize_decimal(spot_marked_value),
            off_position_asset_equity=quantize_decimal(other_asset_equity),
            derivatives_unrealized_pnl=quantize_decimal(derivatives_unrealized_pnl),
            collateral_value=quantize_decimal(collateral_value),
        )

    @staticmethod
    def _symbol_currencies(symbol: str) -> tuple[str | None, str | None]:
        if "-" not in symbol:
            return symbol or None, None
        parts = symbol.split("-")
        if len(parts) >= 2:
            return parts[0] or None, parts[1] or None
        return None, None
