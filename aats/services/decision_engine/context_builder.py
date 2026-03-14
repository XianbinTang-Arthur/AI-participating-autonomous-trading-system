from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import new_id, utc_now
from aats.schemas.decision import DecisionContext
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.mode import RuntimeModeController
from aats.storage.base import EventStore, PortfolioRepository


class DecisionContextBuilder:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store: EventStore,
        portfolio_repo: PortfolioRepository,
        mode_controller: RuntimeModeController,
        health_service: SystemHealthService,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.portfolio_repo = portfolio_repo
        self.mode_controller = mode_controller
        self.health_service = health_service

    def build(self, symbol: str, timeframe: str) -> DecisionContext:
        if timeframe not in self.settings.supported_timeframes:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        market_event = self.event_store.latest(topics.MARKET_SNAPSHOTS, key=symbol)
        feature_event = self.event_store.latest(topics.FEATURE_SNAPSHOTS, key=symbol)
        portfolio_event = self.event_store.latest(topics.PORTFOLIO_SNAPSHOTS, key="portfolio")

        if market_event is None:
            raise RuntimeError("Market snapshot is required before building decision context")
        if feature_event is None:
            raise RuntimeError("Feature snapshot is required before building decision context")
        if portfolio_event is None:
            raise RuntimeError("Portfolio snapshot is required before building decision context")

        portfolio_snapshot = self.portfolio_repo.latest()
        current_position_qty = self._position_qty(portfolio_snapshot, symbol)
        health_snapshot = self.health_service.snapshot()
        return DecisionContext(
            decision_id=new_id("decision"),
            symbol=symbol,
            timeframe=timeframe,
            as_of_ts=utc_now(),
            market_snapshot_ref=market_event.event_id,
            feature_snapshot_ref=feature_event.event_id,
            portfolio_snapshot_ref=portfolio_event.event_id,
            health_snapshot_ref=f"health_{health_snapshot.created_at.isoformat()}",
            mode=self.mode_controller.mode,
            policy_flags=[],
            risk_budget_state={"max_abs_position_qty": self.settings.max_abs_position_qty},
            current_position_qty=current_position_qty,
            current_open_orders=[],
        )

    @staticmethod
    def _position_qty(snapshot: PortfolioSnapshot | None, symbol: str) -> float:
        if snapshot is None:
            return 0.0
        for position in snapshot.positions:
            if position.symbol == symbol:
                return position.position_qty
        return 0.0
