from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.strategy_runtime import StrategyCandidate, StrategyFamily


@dataclass(frozen=True, slots=True)
class StrategyTargetHistory:
    created_at: object
    target: PositionTarget


@dataclass(frozen=True, slots=True)
class StrategyEngineInput:
    context: DecisionContext
    baseline: BaselineAssessment
    directional_target: PositionTarget
    latest_snapshot: PortfolioSnapshot | None
    latest_account_snapshot: ExchangeAccountSnapshot | None
    latest_market_snapshot: MarketSnapshot | None
    recent_market_snapshots: dict[str, list[MarketSnapshot]]
    recent_targets_by_family: dict[str, list[StrategyTargetHistory]]
    ai_assessment: AIMarketAssessment | None = None


@dataclass(frozen=True, slots=True)
class StrategyFamilyRuntimeControl:
    enabled: bool = False
    shadow_mode_enabled: bool = False
    live_execution_enabled: bool = False


@dataclass(frozen=True, slots=True)
class StrategyEvaluationContext:
    context: DecisionContext
    baseline: BaselineAssessment
    directional_target: PositionTarget
    latest_snapshot: PortfolioSnapshot | None
    latest_account_snapshot: ExchangeAccountSnapshot | None
    latest_market_snapshot: MarketSnapshot | None
    recent_market_snapshots: dict[str, list[MarketSnapshot]]
    recent_targets_by_family: dict[str, list[StrategyTargetHistory]]
    ai_assessment: AIMarketAssessment | None
    family_runtime_controls: dict[StrategyFamily, StrategyFamilyRuntimeControl]

    @classmethod
    def from_engine_input(
        cls,
        engine_input: StrategyEngineInput,
        *,
        family_runtime_controls: dict[StrategyFamily, StrategyFamilyRuntimeControl],
    ) -> "StrategyEvaluationContext":
        return cls(
            context=engine_input.context,
            baseline=engine_input.baseline,
            directional_target=engine_input.directional_target,
            latest_snapshot=engine_input.latest_snapshot,
            latest_account_snapshot=engine_input.latest_account_snapshot,
            latest_market_snapshot=engine_input.latest_market_snapshot,
            recent_market_snapshots=engine_input.recent_market_snapshots,
            recent_targets_by_family=engine_input.recent_targets_by_family,
            ai_assessment=engine_input.ai_assessment,
            family_runtime_controls=family_runtime_controls,
        )


class StrategyFamilyEngine(Protocol):
    family_name: StrategyFamily

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> list[StrategyCandidate]:
        ...
