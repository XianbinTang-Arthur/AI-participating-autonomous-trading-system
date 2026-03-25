from __future__ import annotations

from aats.schemas.strategy_runtime import (
    AllocatorBudgetSnapshot,
    AllocatorConflictResolution,
    AllocatorNettingDecision,
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    SleeveBudgetProfile,
    StrategyExecutionBundle,
    StrategySleeveIntent,
)


class InMemoryStrategyRuntimeRepository:
    def __init__(self) -> None:
        self._budget_profiles: dict[str, SleeveBudgetProfile] = {}
        self._budget_assignments: dict[str, SleeveBudgetAssignment] = {}
        self._sleeve_intents: dict[str, StrategySleeveIntent] = {}
        self._allocations: dict[str, PortfolioAllocationDecision] = {}
        self._bundles: dict[str, StrategyExecutionBundle] = {}
        self._budget_snapshots: dict[str, AllocatorBudgetSnapshot] = {}
        self._conflict_resolutions: dict[str, AllocatorConflictResolution] = {}
        self._netting_decisions: dict[str, AllocatorNettingDecision] = {}

    def save_budget_profile(self, profile: SleeveBudgetProfile) -> SleeveBudgetProfile:
        self._budget_profiles[profile.budget_profile_id] = profile
        return profile

    def list_budget_profiles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        family: str | None = None,
    ) -> list[SleeveBudgetProfile]:
        rows = sorted(self._budget_profiles.values(), key=lambda item: item.updated_at, reverse=True)
        return [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (family is None or item.family == family)
        ]

    def save_budget_assignment(self, assignment: SleeveBudgetAssignment) -> SleeveBudgetAssignment:
        self._budget_assignments[assignment.assignment_id] = assignment
        return assignment

    def list_budget_assignments(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        strategy_sleeve_id: str | None = None,
    ) -> list[SleeveBudgetAssignment]:
        rows = sorted(self._budget_assignments.values(), key=lambda item: item.updated_at, reverse=True)
        return [
            item
            for item in rows
            if (product_type is None or item.product_type == product_type)
            and (margin_mode is None or item.margin_mode == margin_mode)
            and (symbol is None or item.symbol == symbol)
            and (strategy_sleeve_id is None or item.strategy_sleeve_id == strategy_sleeve_id)
        ]

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
        for snapshot in decision.budget_snapshots:
            self._budget_snapshots[snapshot.budget_snapshot_id] = snapshot
        for item in decision.conflict_resolutions:
            self._conflict_resolutions[item.conflict_resolution_id] = item
        for item in decision.netting_decisions:
            self._netting_decisions[item.netting_decision_id] = item
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

    def get_allocation_decision(self, allocation_id: str) -> PortfolioAllocationDecision | None:
        return self._allocations.get(allocation_id)

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

    def list_budget_snapshots(
        self,
        *,
        allocation_id: str | None = None,
        strategy_sleeve_id: str | None = None,
    ) -> list[AllocatorBudgetSnapshot]:
        rows = sorted(self._budget_snapshots.values(), key=lambda item: item.created_at, reverse=True)
        return [
            item
            for item in rows
            if (allocation_id is None or item.allocation_id == allocation_id)
            and (strategy_sleeve_id is None or item.strategy_sleeve_id == strategy_sleeve_id)
        ]

    def list_conflict_resolutions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
    ) -> list[AllocatorConflictResolution]:
        rows = sorted(self._conflict_resolutions.values(), key=lambda item: item.created_at, reverse=True)
        return [
            item
            for item in rows
            if (allocation_id is None or item.allocation_id == allocation_id)
            and (symbol is None or item.symbol == symbol)
        ]

    def list_netting_decisions(
        self,
        *,
        allocation_id: str | None = None,
        symbol: str | None = None,
    ) -> list[AllocatorNettingDecision]:
        rows = sorted(self._netting_decisions.values(), key=lambda item: item.created_at, reverse=True)
        return [
            item
            for item in rows
            if (allocation_id is None or item.allocation_id == allocation_id)
            and (symbol is None or item.symbol == symbol)
        ]
