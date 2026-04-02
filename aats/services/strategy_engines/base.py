from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.strategy_runtime import StrategyCandidate, StrategyFamily
from aats.services.strategy_engines.overlay_parent_exposure import OverlayParentExposureLifecycle

StrategyMarketHistorySamplingSource = Literal["event_store_recent", "not_required"]
StrategyLatestMarketSnapshotSource = Literal[
    "gateway_or_event_store_latest",
    "market_gateway_latest",
    "event_store_latest",
    "not_required",
]
StrategyLatestPortfolioSnapshotSource = Literal["runtime_scope_latest", "not_required"]
StrategyLatestAccountSnapshotSource = Literal["account_service_latest", "not_required"]


@dataclass(frozen=True, slots=True)
class StrategyTargetHistory:
    created_at: object
    target: PositionTarget


@dataclass(frozen=True, slots=True)
class StrategyMarketHistoryRequest:
    family: StrategyFamily
    symbols: tuple[str, ...] = field(default_factory=tuple)
    topic: str | None = None
    sampling_source: StrategyMarketHistorySamplingSource = "not_required"
    lookback_snapshots: int = 1
    latest_snapshot_symbols: tuple[str, ...] = field(default_factory=tuple)
    latest_snapshot_symbol: str | None = None
    latest_snapshot_topic: str | None = None
    latest_snapshot_source: StrategyLatestMarketSnapshotSource = "not_required"
    latest_portfolio_snapshot_source: StrategyLatestPortfolioSnapshotSource = "not_required"
    latest_account_snapshot_source: StrategyLatestAccountSnapshotSource = "not_required"


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
    latest_snapshots_by_family: dict[StrategyFamily, PortfolioSnapshot | None] = field(default_factory=dict)
    latest_account_snapshots_by_family: dict[StrategyFamily, ExchangeAccountSnapshot | None] = field(default_factory=dict)
    resolved_pair_definitions_by_family: dict[StrategyFamily, tuple[object, ...]] = field(default_factory=dict)
    latest_market_snapshots_by_symbol: dict[str, MarketSnapshot] = field(default_factory=dict)
    latest_market_snapshots_by_symbol_by_family: dict[StrategyFamily, dict[str, MarketSnapshot]] = field(default_factory=dict)
    latest_market_snapshots_by_family: dict[StrategyFamily, MarketSnapshot | None] = field(default_factory=dict)
    overlay_parent_exposures_by_family: dict[StrategyFamily, OverlayParentExposureLifecycle] = field(default_factory=dict)
    recent_market_snapshot_windows_by_family: dict[StrategyFamily, int] = field(default_factory=dict)
    market_history_requests_by_family: dict[StrategyFamily, StrategyMarketHistoryRequest] = field(default_factory=dict)


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
    latest_snapshots_by_family: dict[StrategyFamily, PortfolioSnapshot | None] = field(default_factory=dict)
    latest_account_snapshots_by_family: dict[StrategyFamily, ExchangeAccountSnapshot | None] = field(default_factory=dict)
    resolved_pair_definitions_by_family: dict[StrategyFamily, tuple[object, ...]] = field(default_factory=dict)
    latest_market_snapshots_by_symbol: dict[str, MarketSnapshot] = field(default_factory=dict)
    latest_market_snapshots_by_symbol_by_family: dict[StrategyFamily, dict[str, MarketSnapshot]] = field(default_factory=dict)
    latest_market_snapshots_by_family: dict[StrategyFamily, MarketSnapshot | None] = field(default_factory=dict)
    overlay_parent_exposure: OverlayParentExposureLifecycle | None = None
    overlay_parent_exposures_by_family: dict[StrategyFamily, OverlayParentExposureLifecycle] = field(default_factory=dict)
    recent_market_snapshot_windows_by_family: dict[StrategyFamily, int] = field(default_factory=dict)
    market_history_requests_by_family: dict[StrategyFamily, StrategyMarketHistoryRequest] = field(default_factory=dict)

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
            latest_snapshots_by_family=engine_input.latest_snapshots_by_family,
            latest_account_snapshots_by_family=engine_input.latest_account_snapshots_by_family,
            resolved_pair_definitions_by_family=engine_input.resolved_pair_definitions_by_family,
            latest_market_snapshots_by_symbol=engine_input.latest_market_snapshots_by_symbol,
            latest_market_snapshots_by_symbol_by_family=engine_input.latest_market_snapshots_by_symbol_by_family,
            latest_market_snapshots_by_family=engine_input.latest_market_snapshots_by_family,
            overlay_parent_exposures_by_family=engine_input.overlay_parent_exposures_by_family,
            recent_market_snapshots=engine_input.recent_market_snapshots,
            recent_targets_by_family=engine_input.recent_targets_by_family,
            ai_assessment=engine_input.ai_assessment,
            family_runtime_controls=family_runtime_controls,
            recent_market_snapshot_windows_by_family=engine_input.recent_market_snapshot_windows_by_family,
            market_history_requests_by_family=engine_input.market_history_requests_by_family,
        )

    def for_family(self, family: StrategyFamily) -> "StrategyEvaluationContext":
        request = self.market_history_requests_by_family.get(family)
        limit = max(int(self.recent_market_snapshot_windows_by_family.get(family, 1)), 1)
        requested_symbols = set(request.symbols) if request is not None else set()
        sampling_source = "event_store_recent" if request is None else request.sampling_source
        latest_snapshot = self.latest_snapshots_by_family.get(family)
        if latest_snapshot is None and family not in self.latest_snapshots_by_family:
            latest_snapshot = self.latest_snapshot
        latest_account_snapshot = self.latest_account_snapshots_by_family.get(family)
        if latest_account_snapshot is None and family not in self.latest_account_snapshots_by_family:
            latest_account_snapshot = self.latest_account_snapshot
        resolved_pair_definitions = self.resolved_pair_definitions_by_family
        latest_market_snapshots_by_symbol = self.latest_market_snapshots_by_symbol_by_family.get(family)
        if latest_market_snapshots_by_symbol is None and family not in self.latest_market_snapshots_by_symbol_by_family:
            latest_market_snapshots_by_symbol = (
                {}
                if self.latest_market_snapshot is None
                else {self.latest_market_snapshot.symbol: self.latest_market_snapshot}
            )
        latest_market_snapshot = self.latest_market_snapshots_by_family.get(family)
        if latest_market_snapshot is None and family not in self.latest_market_snapshots_by_family:
            latest_market_snapshot = (
                self.latest_market_snapshot
                if self.latest_market_snapshot is not None
                and (
                    not requested_symbols
                    or self.latest_market_snapshot.symbol in requested_symbols
                )
                else None
            )
        return StrategyEvaluationContext(
            context=self.context,
            baseline=self.baseline,
            directional_target=self.directional_target,
            latest_snapshot=latest_snapshot,
            latest_account_snapshot=latest_account_snapshot,
            latest_market_snapshot=latest_market_snapshot,
            latest_snapshots_by_family=self.latest_snapshots_by_family,
            latest_account_snapshots_by_family=self.latest_account_snapshots_by_family,
            resolved_pair_definitions_by_family=resolved_pair_definitions,
            latest_market_snapshots_by_symbol=latest_market_snapshots_by_symbol,
            latest_market_snapshots_by_symbol_by_family=self.latest_market_snapshots_by_symbol_by_family,
            latest_market_snapshots_by_family=self.latest_market_snapshots_by_family,
            overlay_parent_exposure=self.overlay_parent_exposures_by_family.get(family),
            overlay_parent_exposures_by_family=self.overlay_parent_exposures_by_family,
            recent_market_snapshots=(
                {}
                if sampling_source != "event_store_recent"
                else {
                    symbol: list(rows[-limit:])
                    for symbol, rows in self.recent_market_snapshots.items()
                    if not requested_symbols or symbol in requested_symbols
                }
            ),
            recent_targets_by_family=self.recent_targets_by_family,
            ai_assessment=self.ai_assessment,
            family_runtime_controls=self.family_runtime_controls,
            recent_market_snapshot_windows_by_family=self.recent_market_snapshot_windows_by_family,
            market_history_requests_by_family=self.market_history_requests_by_family,
        )


class StrategyFamilyEngine(Protocol):
    family_name: StrategyFamily

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> list[StrategyCandidate]:
        ...
