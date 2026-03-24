from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.strategy_runtime import StrategyCandidate, StrategyLegIntent
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import StrategyEngineInput


def _derived_spot_symbol(symbol: str) -> str | None:
    normalized = str(symbol or "").upper()
    if not normalized:
        return None
    if normalized.endswith("-SWAP"):
        return normalized[:-5]
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return normalized[: -(len(tail) + 1)]
    return normalized


def _derived_derivatives_symbol(symbol: str) -> str | None:
    normalized = str(symbol or "").upper()
    if not normalized:
        return None
    if normalized.endswith("-SWAP"):
        return normalized
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return normalized
    return f"{normalized}-SWAP"


class SmartArbitrageStrategyEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        market_snapshot_loader,
        account_snapshot_loader=None,
    ) -> None:
        self.settings = settings
        self.market_snapshot_loader = market_snapshot_loader
        self.account_snapshot_loader = account_snapshot_loader

    def evaluate(self, engine_input: StrategyEngineInput) -> StrategyCandidate:
        spot_symbol = (
            self.settings.smart_arbitrage_companion_spot_symbol
            or _derived_spot_symbol(engine_input.context.symbol)
        )
        derivatives_symbol = (
            self.settings.smart_arbitrage_companion_derivatives_symbol
            or _derived_derivatives_symbol(engine_input.context.symbol)
        )
        if not self.settings.smart_arbitrage_enabled:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="disabled",
                enabled=False,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Smart arbitrage is disabled.",
                recommended_symbol=spot_symbol or engine_input.context.symbol,
                reason_codes=["smart_arbitrage_disabled"],
            )
        if engine_input.context.product_type != "derivatives":
            return StrategyCandidate(
                family="smart_arbitrage",
                state="incompatible",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Smart arbitrage auto execution currently runs on derivatives runtime only.",
                recommended_symbol=spot_symbol or engine_input.context.symbol,
                reason_codes=["smart_arbitrage_derivatives_runtime_required"],
            )
        if not spot_symbol or not derivatives_symbol:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Spot and derivatives companion symbols are not configured.",
                reason_codes=["smart_arbitrage_symbol_pair_missing"],
            )
        spot_snapshot = self.market_snapshot_loader(spot_symbol)
        derivatives_snapshot = self.market_snapshot_loader(derivatives_symbol)
        if spot_snapshot is None or derivatives_snapshot is None:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Paired market snapshots are incomplete.",
                recommended_symbol=spot_symbol,
                reason_codes=["smart_arbitrage_market_pair_incomplete"],
                metrics={
                    "spot_symbol": spot_symbol,
                    "derivatives_symbol": derivatives_symbol,
                },
            )

        spot_price = to_decimal(spot_snapshot.last_price)
        derivatives_price = to_decimal(derivatives_snapshot.last_price)
        if abs(spot_price) <= EPSILON_DECIMAL_12:
            basis_bps = Decimal("0")
        else:
            basis_bps = ((derivatives_price - spot_price) / spot_price) * Decimal("10000")
        entry_threshold = Decimal(str(max(self.settings.smart_arbitrage_basis_entry_bps, 0.0)))
        exit_threshold = Decimal(str(max(self.settings.smart_arbitrage_basis_exit_bps, 0.0)))
        estimated_cost_bps = Decimal(str(max(self.settings.smart_arbitrage_estimated_cost_bps, 0.0)))
        net_basis_bps = basis_bps.copy_abs() - estimated_cost_bps
        account_snapshot = engine_input.latest_account_snapshot or self._latest_account_snapshot()
        current_spot_qty = self._current_spot_quantity(
            snapshot=account_snapshot,
            engine_input=engine_input,
            spot_symbol=spot_symbol,
        )
        current_derivatives_qty = self._current_derivatives_quantity(
            snapshot=account_snapshot,
            engine_input=engine_input,
            derivatives_symbol=derivatives_symbol,
        )
        current_short_qty = abs(min(current_derivatives_qty, Decimal("0")))
        current_long_qty = max(current_derivatives_qty, Decimal("0"))
        paired_qty = min(current_spot_qty, current_short_qty)
        current_pair_active = current_spot_qty > EPSILON_DECIMAL_12 or current_short_qty > EPSILON_DECIMAL_12
        target_pair_qty = self._entry_pair_qty(spot_price=spot_price)
        directional_target_qty = to_decimal(engine_input.directional_target.target_position_qty)
        protective_directional_exit = (
            not current_pair_active
            and abs(current_derivatives_qty) > EPSILON_DECIMAL_12
            and abs(directional_target_qty) + EPSILON_DECIMAL_12 < abs(current_derivatives_qty)
        )

        if protective_directional_exit:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="advisory_only",
                enabled=True,
                selectable=True,
                execution_compatible=False,
                route_action="advisory_only",
                headline="当前更像是单腿仓位的保护性退出，智能套利不会接管本轮去风险动作。",
                recommended_symbol=derivatives_symbol,
                score=float(max(net_basis_bps, Decimal("0")) / max(entry_threshold or Decimal("1"), Decimal("1"))),
                confidence=min(0.95, 0.45 + (min(abs(float(basis_bps)), 120.0) / 200.0)),
                urgency="medium",
                reason_codes=[
                    "smart_arbitrage_protective_directional_exit_retained",
                    "smart_arbitrage_existing_unpaired_exposure",
                ],
                metrics={
                    "spot_symbol": spot_symbol,
                    "derivatives_symbol": derivatives_symbol,
                    "spot_price": spot_price,
                    "derivatives_price": derivatives_price,
                    "basis_bps": basis_bps,
                    "net_basis_bps": net_basis_bps,
                    "current_spot_qty": current_spot_qty,
                    "current_derivatives_qty": current_derivatives_qty,
                    "directional_target_qty": directional_target_qty,
                },
            )

        if basis_bps < Decimal("0") and not current_pair_active and current_long_qty <= EPSILON_DECIMAL_12:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="advisory_only",
                enabled=True,
                selectable=True,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Negative basis is detected, but cash spot shorting is not supported for auto execution.",
                recommended_symbol=spot_symbol,
                score=float(max(net_basis_bps, Decimal("0")) / max(entry_threshold, Decimal("1"))),
                confidence=min(0.95, 0.45 + (min(abs(float(basis_bps)), 120.0) / 200.0)),
                urgency="medium",
                reason_codes=[
                    "smart_arbitrage_negative_basis",
                    "smart_arbitrage_spot_short_not_supported",
                ],
                metrics={
                    "spot_symbol": spot_symbol,
                    "derivatives_symbol": derivatives_symbol,
                    "spot_price": spot_price,
                    "derivatives_price": derivatives_price,
                    "basis_bps": basis_bps,
                    "net_basis_bps": net_basis_bps,
                    "current_spot_qty": current_spot_qty,
                    "current_derivatives_qty": current_derivatives_qty,
                },
            )

        positive_basis_active = basis_bps >= entry_threshold
        unwind_required = current_pair_active and (
            basis_bps <= exit_threshold or current_long_qty > EPSILON_DECIMAL_12
        )
        recovery_mode = current_pair_active and not unwind_required and (
            abs(current_spot_qty - current_short_qty) > EPSILON_DECIMAL_12
        )
        if unwind_required:
            desired_pair_qty = Decimal("0")
            headline = "Basis has normalized or the hedge posture is inconsistent; unwind the pair."
            reason_codes = ["smart_arbitrage_exit_ready"]
            urgency = "high" if current_pair_active else "medium"
        elif positive_basis_active or recovery_mode:
            if current_pair_active:
                desired_pair_qty = max(current_spot_qty, current_short_qty)
            else:
                desired_pair_qty = target_pair_qty
            headline = "Positive basis pair is ready."
            reason_codes = ["smart_arbitrage_positive_basis"]
            urgency = "medium"
            if recovery_mode:
                reason_codes.append("smart_arbitrage_partial_fill_recovery")
                headline = "Positive basis pair is imbalanced; recover the missing leg."
                urgency = "high"
        else:
            if current_pair_active:
                desired_pair_qty = max(current_spot_qty, current_short_qty)
                headline = "Basis remains above the exit threshold; keep the pair open."
                reason_codes = ["smart_arbitrage_pair_active_waiting_exit"]
                urgency = "low"
            else:
                return StrategyCandidate(
                    family="smart_arbitrage",
                    state="inactive",
                    enabled=True,
                    selectable=False,
                    execution_compatible=True,
                    route_action="hold_current",
                    headline="Basis is below the configured entry threshold.",
                    recommended_symbol=spot_symbol,
                    reason_codes=["smart_arbitrage_basis_below_entry_threshold"],
                    metrics={
                        "spot_symbol": spot_symbol,
                        "derivatives_symbol": derivatives_symbol,
                        "spot_price": spot_price,
                        "derivatives_price": derivatives_price,
                        "basis_bps": basis_bps,
                        "net_basis_bps": net_basis_bps,
                    },
                )

        spot_target_qty = desired_pair_qty
        derivatives_target_qty = -desired_pair_qty
        spot_delta_qty = spot_target_qty - current_spot_qty
        derivatives_delta_qty = derivatives_target_qty - current_derivatives_qty
        route_action = (
            "override_target"
            if abs(spot_delta_qty) > EPSILON_DECIMAL_12 or abs(derivatives_delta_qty) > EPSILON_DECIMAL_12
            else "hold_current"
        )
        state = "ready"
        confidence = min(0.96, 0.50 + (min(abs(float(basis_bps)), 120.0) / 180.0))
        score = float(max(net_basis_bps, Decimal("0")) / max(entry_threshold or Decimal("1"), Decimal("1")))
        legs = [
            StrategyLegIntent(
                symbol=spot_symbol,
                product_type="spot",
                side="buy" if spot_delta_qty >= 0 else "sell",
                role="primary",
                margin_mode="cash",
                target_leverage=1.0,
                current_position_qty=current_spot_qty,
                target_position_qty=spot_target_qty,
                delta_position_qty=spot_delta_qty,
                reference_price=spot_price,
                execution_compatible=True,
                note="Spot cash inventory leg.",
            ),
            StrategyLegIntent(
                symbol=derivatives_symbol,
                product_type="derivatives",
                side="buy" if derivatives_delta_qty >= 0 else "sell",
                role="hedge",
                margin_mode=self.settings.margin_mode,
                target_leverage=self.settings.default_target_leverage,
                current_position_qty=current_derivatives_qty,
                target_position_qty=derivatives_target_qty,
                delta_position_qty=derivatives_delta_qty,
                reference_price=derivatives_price,
                execution_compatible=True,
                note="Derivatives hedge leg.",
            ),
        ]
        return StrategyCandidate(
            family="smart_arbitrage",
            state=state,
            enabled=True,
            selectable=True,
            execution_compatible=True,
            route_action=route_action,
            headline=headline,
            recommended_symbol=derivatives_symbol,
            target_position_qty=derivatives_target_qty,
            delta_position_qty=derivatives_delta_qty,
            score=score,
            confidence=confidence,
            urgency=urgency,
            reason_codes=reason_codes,
            metrics={
                "spot_symbol": spot_symbol,
                "derivatives_symbol": derivatives_symbol,
                "spot_price": spot_price,
                "derivatives_price": derivatives_price,
                "basis_bps": basis_bps,
                "net_basis_bps": net_basis_bps,
                "estimated_cost_bps": estimated_cost_bps,
                "target_pair_qty": desired_pair_qty,
                "current_spot_qty": current_spot_qty,
                "current_derivatives_qty": current_derivatives_qty,
                "paired_qty": paired_qty,
                "route_action": route_action,
            },
            legs=legs,
        )

    def _latest_account_snapshot(self) -> ExchangeAccountSnapshot | None:
        if self.account_snapshot_loader is None:
            return None
        snapshot = self.account_snapshot_loader()
        return snapshot if isinstance(snapshot, ExchangeAccountSnapshot) or snapshot is None else None

    def _current_spot_quantity(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        engine_input: StrategyEngineInput,
        spot_symbol: str,
    ) -> Decimal:
        if snapshot is not None:
            base_currency = spot_symbol.split("-", 1)[0]
            for balance in snapshot.balances:
                if balance.currency.upper() == base_currency.upper():
                    return to_decimal(balance.total)
        if engine_input.latest_snapshot is not None:
            base_currency = spot_symbol.split("-", 1)[0]
            if base_currency in engine_input.latest_snapshot.balances:
                return to_decimal(engine_input.latest_snapshot.balances[base_currency])
            for position in engine_input.latest_snapshot.positions:
                if position.symbol == spot_symbol:
                    return to_decimal(position.position_qty)
        return Decimal("0")

    def _current_derivatives_quantity(
        self,
        *,
        snapshot: ExchangeAccountSnapshot | None,
        engine_input: StrategyEngineInput,
        derivatives_symbol: str,
    ) -> Decimal:
        if snapshot is not None:
            quantity = Decimal("0")
            for position in snapshot.positions:
                if position.symbol != derivatives_symbol:
                    continue
                signed_qty = to_decimal(position.quantity)
                if str(position.side or "net").lower() == "short" and signed_qty > 0:
                    signed_qty = -signed_qty
                quantity += signed_qty
            if abs(quantity) > EPSILON_DECIMAL_12:
                return quantity
        if derivatives_symbol == engine_input.context.symbol:
            return to_decimal(engine_input.context.current_position_qty)
        if engine_input.latest_snapshot is not None:
            quantity = sum(
                (
                    to_decimal(position.position_qty)
                    for position in engine_input.latest_snapshot.positions
                    if position.symbol == derivatives_symbol
                ),
                start=Decimal("0"),
            )
            if abs(quantity) > EPSILON_DECIMAL_12:
                return quantity
        return Decimal("0")

    def _entry_pair_qty(self, *, spot_price: Decimal) -> Decimal:
        if spot_price <= EPSILON_DECIMAL_12:
            return Decimal("0")
        quote_budget = to_decimal(self.settings.smart_arbitrage_quote_budget_per_trade)
        notional_cap = to_decimal(self.settings.smart_arbitrage_max_pair_notional)
        effective_notional = min(
            value for value in (quote_budget, notional_cap) if value > EPSILON_DECIMAL_12
        ) if max(quote_budget, notional_cap) > EPSILON_DECIMAL_12 else Decimal("0")
        if effective_notional <= EPSILON_DECIMAL_12:
            return Decimal("0")
        return effective_notional / spot_price
