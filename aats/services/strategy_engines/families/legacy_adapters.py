from __future__ import annotations

from collections.abc import Callable

from aats.schemas.strategy_runtime import StrategyCandidate, StrategyFamily
from aats.services.strategy_engines.base import StrategyEvaluationContext, StrategyEngineInput


class DirectionalFamilyAdapter:
    family_name: StrategyFamily = "directional"

    def __init__(self, *, candidate_loader: Callable[[object], StrategyCandidate]) -> None:
        self._candidate_loader = candidate_loader

    def evaluate(self, context: StrategyEvaluationContext) -> list[StrategyCandidate]:
        return [self._candidate_loader(context.directional_target)]


class ExistingCandidateFamilyAdapter:
    def __init__(
        self,
        *,
        family_name: StrategyFamily,
        evaluator: Callable[[StrategyEngineInput], StrategyCandidate],
    ) -> None:
        self.family_name = family_name
        self._evaluator = evaluator

    def evaluate(self, context: StrategyEvaluationContext) -> list[StrategyCandidate]:
        engine_input = StrategyEngineInput(
            context=context.context,
            baseline=context.baseline,
            directional_target=context.directional_target,
            latest_snapshot=context.latest_snapshot,
            latest_account_snapshot=context.latest_account_snapshot,
            latest_market_snapshot=context.latest_market_snapshot,
            recent_market_snapshots=context.recent_market_snapshots,
            recent_targets_by_family=context.recent_targets_by_family,
            ai_assessment=context.ai_assessment,
            latest_snapshots_by_family=context.latest_snapshots_by_family,
            latest_account_snapshots_by_family=context.latest_account_snapshots_by_family,
            resolved_pair_definitions_by_family=context.resolved_pair_definitions_by_family,
            latest_market_snapshots_by_symbol=context.latest_market_snapshots_by_symbol,
            latest_market_snapshots_by_symbol_by_family=context.latest_market_snapshots_by_symbol_by_family,
            latest_market_snapshots_by_family=context.latest_market_snapshots_by_family,
            recent_market_snapshot_windows_by_family=context.recent_market_snapshot_windows_by_family,
            market_history_requests_by_family=context.market_history_requests_by_family,
        )
        return [self._evaluator(engine_input)]
