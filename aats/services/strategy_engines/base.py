from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot


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
    latest_market_snapshot: MarketSnapshot | None
    recent_market_snapshots: dict[str, list[MarketSnapshot]]
    recent_targets_by_family: dict[str, list[StrategyTargetHistory]]
