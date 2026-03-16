from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from aats.schemas.common import utc_now
from aats.schemas.portfolio import PortfolioSnapshot, Position
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
        price_provider: Callable[[str], float],
        decision_id: str | None = None,
        source_intent_id: str | None = None,
        source_fill_id: str | None = None,
    ) -> PortfolioSnapshot:
        positions: list[Position] = []
        gross_exposure = 0.0
        net_exposure = 0.0
        unrealized_pnl = 0.0
        spot_marked_value = 0.0
        derivatives_unrealized_pnl = 0.0

        for symbol, record in state.positions.items():
            mark_price = price_provider(symbol)
            position_notional = record.quantity * mark_price
            position_unrealized = self.pnl_calculator.unrealized_pnl(
                position_qty=record.quantity,
                avg_entry_price=record.avg_entry_price,
                mark_price=mark_price,
            )
            positions.append(
                Position(
                    symbol=symbol,
                    position_qty=record.quantity,
                    position_notional=position_notional,
                    avg_entry_price=record.avg_entry_price,
                    unrealized_pnl=position_unrealized,
                    product_type=record.product_type,  # type: ignore[arg-type]
                    exposure_side=("long" if record.quantity > 1e-12 else "short" if record.quantity < -1e-12 else "flat"),
                    target_leverage=record.target_leverage,
                    margin_mode=record.margin_mode,  # type: ignore[arg-type]
                    margin_allocated=(
                        abs(position_notional) / max(record.target_leverage, 1.0)
                        if record.product_type == "derivatives"
                        else 0.0
                    ),
                )
            )
            gross_exposure += abs(position_notional)
            net_exposure += position_notional
            unrealized_pnl += position_unrealized
            if record.product_type == "derivatives":
                derivatives_unrealized_pnl += position_unrealized
            else:
                spot_marked_value += position_notional

        balances = dict(state.balances)
        total_equity = balances.get("USDT", 0.0) + spot_marked_value + derivatives_unrealized_pnl
        return PortfolioSnapshot(
            decision_id=decision_id,
            source_intent_id=source_intent_id,
            source_fill_id=source_fill_id,
            snapshot_ts=utc_now(),
            balances=balances,
            positions=positions,
            cost_basis={symbol: record.avg_entry_price for symbol, record in state.positions.items()},
            realized_pnl=state.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_equity=total_equity,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            risk_budget_usage={"gross_exposure": gross_exposure},
            product_type=(
                "derivatives"
                if any(record.product_type == "derivatives" for record in state.positions.values())
                else state.default_product_type
            ),
            margin_mode=next(iter({record.margin_mode for record in state.positions.values()}), state.default_margin_mode),
            margin_usage=sum(
                abs(record.quantity * price_provider(symbol)) / max(record.target_leverage, 1.0)
                for symbol, record in state.positions.items()
                if record.product_type == "derivatives"
            ),
            leverage_profile={
                symbol: record.target_leverage for symbol, record in state.positions.items()
            },
        )
