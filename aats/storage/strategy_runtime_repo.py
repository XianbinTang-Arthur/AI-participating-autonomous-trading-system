from __future__ import annotations

from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyExecutionBundle,
    StrategySleeveIntent,
)


class InMemoryStrategyRuntimeRepository:
    def __init__(self) -> None:
        self._sleeve_intents: dict[str, StrategySleeveIntent] = {}
        self._allocations: dict[str, PortfolioAllocationDecision] = {}
        self._bundles: dict[str, StrategyExecutionBundle] = {}

    def save_sleeve_intent(self, intent: StrategySleeveIntent) -> StrategySleeveIntent:
        self._sleeve_intents[intent.sleeve_intent_id] = intent
        return intent

    def list_sleeve_intents(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategySleeveIntent]:
        rows = sorted(self._sleeve_intents.values(), key=lambda item: item.created_at, reverse=True)
        rows = [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (symbol is None or item.symbol == symbol)
        ]
        return rows if limit is None else rows[:limit]

    def save_allocation_decision(self, decision: PortfolioAllocationDecision) -> PortfolioAllocationDecision:
        self._allocations[decision.allocation_id] = decision
        return decision

    def latest_allocation_decision(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
    ) -> PortfolioAllocationDecision | None:
        rows = sorted(self._allocations.values(), key=lambda item: item.created_at, reverse=True)
        for item in rows:
            if product_type is not None and item.product_type != product_type:
                continue
            if margin_mode is not None and item.margin_mode != margin_mode:
                continue
            if symbol is not None and item.symbol != symbol:
                continue
            return item
        return None

    def save_execution_bundle(self, bundle: StrategyExecutionBundle) -> StrategyExecutionBundle:
        self._bundles[bundle.bundle_id] = bundle
        return bundle

    def recent_execution_bundles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategyExecutionBundle]:
        rows = sorted(self._bundles.values(), key=lambda item: item.created_at, reverse=True)
        rows = [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (symbol is None or item.selected_symbol == symbol)
        ]
        return rows if limit is None else rows[:limit]
