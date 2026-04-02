from __future__ import annotations

from collections.abc import Iterable

from aats.schemas.strategy_runtime import StrategyCandidate, StrategyFamily
from aats.services.strategy_engines.base import StrategyEvaluationContext, StrategyFamilyEngine


class StrategyFamilyRegistry:
    def __init__(self) -> None:
        self._engines: dict[StrategyFamily, StrategyFamilyEngine] = {}
        self._order: list[StrategyFamily] = []

    def register(self, engine: StrategyFamilyEngine) -> None:
        family = engine.family_name
        self._engines[family] = engine
        if family not in self._order:
            self._order.append(family)

    def families(self) -> tuple[StrategyFamily, ...]:
        return tuple(self._order)

    def evaluate_all(self, context: StrategyEvaluationContext) -> dict[StrategyFamily, list[StrategyCandidate]]:
        results: dict[StrategyFamily, list[StrategyCandidate]] = {}
        for family in self._order:
            engine = self._engines[family]
            family_context = context.for_family(family)
            candidates = list(engine.evaluate(family_context) or [])
            normalized: list[StrategyCandidate] = []
            for candidate in candidates:
                normalized.append(
                    candidate if candidate.family == family else candidate.model_copy(update={"family": family})
                )
            results[family] = normalized
        return results

    @staticmethod
    def primary_candidate_map(
        results: dict[StrategyFamily, list[StrategyCandidate]],
    ) -> dict[StrategyFamily, StrategyCandidate]:
        return {
            family: candidates[0]
            for family, candidates in results.items()
            if candidates
        }

    def iter_families(self) -> Iterable[StrategyFamily]:
        return tuple(self._order)
