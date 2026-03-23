from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.system import HealthSnapshot
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.portfolio_service.position_keys import symbol_from_position_key
from aats.services.runtime_scope import latest_matching_snapshot, runtime_state_scope, scoped_portfolio_event
from aats.services.strategy_execution_health import compute_strategy_execution_health
from aats.storage.base import EventStore, ExecutionRepository, PortfolioRepository


class DecisionContextBuilder:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store: EventStore,
        portfolio_repo: PortfolioRepository,
        execution_repo: ExecutionRepository,
        mode_controller: RuntimeModeController,
        health_service: SystemHealthService,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.portfolio_repo = portfolio_repo
        self.execution_repo = execution_repo
        self.mode_controller = mode_controller
        self.health_service = health_service
        self.state_scope = runtime_state_scope(settings)

    def build_health_snapshot(self, *, decision_id: str) -> HealthSnapshot:
        snapshot = self.health_service.snapshot()
        return HealthSnapshot(
            decision_id=decision_id,
            mode=snapshot.mode,
            operating_state=snapshot.operating_state,
            status=snapshot.status,
            halted=snapshot.halted,
            blockers=list(snapshot.blockers),
            components=list(snapshot.components),
        )

    def build(
        self,
        symbol: str,
        timeframe: str,
        *,
        decision_id: str,
        health_snapshot_ref: str,
    ) -> DecisionContext:
        if timeframe not in self.settings.supported_timeframes:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        market_event = self.event_store.latest(topics.MARKET_SNAPSHOTS, key=symbol)
        feature_event = self.event_store.latest(topics.FEATURE_SNAPSHOTS, key=symbol)
        portfolio_event = scoped_portfolio_event(
            self.event_store.by_topic(topics.PORTFOLIO_SNAPSHOTS),
            self.state_scope,
        )
        portfolio_snapshot = latest_matching_snapshot(self.portfolio_repo.history(), self.state_scope)

        if market_event is None:
            raise RuntimeError("Market snapshot is required before building decision context")
        if feature_event is None:
            raise RuntimeError("Feature snapshot is required before building decision context")
        if portfolio_event is None and portfolio_snapshot is None:
            raise RuntimeError("Portfolio snapshot is required before building decision context")

        current_position_qty = self._position_qty(portfolio_snapshot, symbol, self.settings.trading_product_type)
        current_exposure_side = self._exposure_side(current_position_qty)
        open_orders = [
            order.client_order_id
            for order in self.execution_repo.order_states_for_scope(
                scope=self.state_scope,
                open_only=True,
            )
            if order.symbol == symbol
        ]
        strategy_health = compute_strategy_execution_health(
            settings=self.settings,
            symbol=symbol,
            fills=self.execution_repo.fills_for_scope(scope=self.state_scope),
            snapshots=self.portfolio_repo.history(),
            current_position_qty=current_position_qty,
        )
        guardrails = strategy_health.active_guardrails(
            settings=self.settings,
            as_of=utc_now(),
            current_position_qty=current_position_qty,
        )
        return DecisionContext(
            decision_id=decision_id,
            symbol=symbol,
            timeframe=timeframe,
            as_of_ts=utc_now(),
            market_snapshot_ref=market_event.event_id,
            feature_snapshot_ref=feature_event.event_id,
            portfolio_snapshot_ref=(
                portfolio_event.event_id
                if portfolio_event is not None
                else f"portfolio_snapshot:{portfolio_snapshot.created_at.isoformat()}"
            ),
            health_snapshot_ref=health_snapshot_ref,
            mode=self.mode_controller.mode,
            policy_flags=[],
            risk_budget_state={
                "max_abs_position_qty": to_decimal(self.settings.max_abs_position_qty),
                "max_target_leverage": to_decimal(self.settings.max_target_leverage),
            },
            current_position_qty=current_position_qty,
            current_open_orders=open_orders,
            product_type=self.settings.trading_product_type,
            current_exposure_side=current_exposure_side,
            current_target_leverage=self._current_target_leverage(portfolio_snapshot, symbol),
            current_position_opened_at=strategy_health.current_position_opened_at,
            last_position_closed_at=strategy_health.last_position_closed_at,
            latest_fill_timestamp=strategy_health.latest_fill_timestamp,
            recent_closed_trade_count=strategy_health.recent_closed_trade_count,
            recent_win_rate=strategy_health.recent_win_rate,
            recent_fee_drag_ratio=strategy_health.recent_fee_drag_ratio,
            recent_churn_ratio=strategy_health.recent_churn_ratio,
            recent_low_edge_trade_streak=strategy_health.recent_low_edge_trade_streak,
            recent_low_edge_trade_at=strategy_health.recent_low_edge_trade_at,
            strategy_guardrail_flags=list(guardrails["flags"]),
            strategy_cooldowns=dict(guardrails["cooldowns"]),
        )

    @staticmethod
    def _position_qty(
        snapshot: PortfolioSnapshot | None,
        symbol: str,
        product_type: str = "spot",
    ) -> Decimal:
        if snapshot is None:
            return Decimal("0")
        quantity = sum(
            (
                position.position_qty
                for position in snapshot.positions
                if position.symbol == symbol
            ),
            start=Decimal("0"),
        )
        if abs(quantity) > EPSILON_DECIMAL_12:
            return quantity
        if product_type == "spot" and "-" in symbol:
            base_currency, _quote_currency = symbol.split("-", 1)
            return snapshot.balances.get(base_currency, Decimal("0"))
        return Decimal("0")

    @staticmethod
    def _current_target_leverage(snapshot: PortfolioSnapshot | None, symbol: str) -> float:
        if snapshot is None:
            return 1.0
        direct = snapshot.leverage_profile.get(symbol)
        if direct is not None:
            return float(direct)
        leverages = [
            float(snapshot.leverage_profile.get(position.position_key or position.symbol, position.target_leverage))
            for position in snapshot.positions
            if position.symbol == symbol
        ]
        if leverages:
            return max(leverages)
        for key, value in snapshot.leverage_profile.items():
            if symbol_from_position_key(key) == symbol:
                leverages.append(float(value))
        return max(leverages) if leverages else 1.0

    @staticmethod
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > EPSILON_DECIMAL_12:
            return "long"
        if quantity < -EPSILON_DECIMAL_12:
            return "short"
        return "flat"
