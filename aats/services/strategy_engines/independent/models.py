from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from aats.schemas.decision import HedgeOverlayDecision
from aats.schemas.strategy_runtime import StrategyLegIntent

if TYPE_CHECKING:
    from aats.services.strategy_engines.families.independent_models import IndependentBookRuntimeState
    from .adaptive import IndependentAdaptiveSnapshot
    from .health import IndependentFamilyHealthSnapshot, IndependentLegHealthSnapshot
    from .replay import IndependentReplayDecisionSnapshot
    from .state_machine import IndependentStateSnapshot

IndependentLeg = Literal["long", "short"]
IndependentExecutionHealthState = Literal["ok", "degraded", "blocked"]
IndependentBookAction = Literal[
    "inactive",
    "open",
    "hold",
    "scale_in",
    "de_risk",
    "close_failed_thesis",
    "close_stale_thesis",
    "blocked",
]


@dataclass(frozen=True, slots=True)
class IndependentBookExpectancy:
    leg: IndependentLeg
    expected_signal_edge_bps: float
    expected_slippage_bps: float
    expected_cost_bps: float
    expected_net_edge_bps: float
    expected_alpha_bps: float | None = None
    planned_delta_qty: Decimal | None = None
    projected_notional: Decimal | None = None
    reference_price: Decimal | None = None
    quoted_depth_notional: Decimal | None = None
    depth_consumption_ratio: float | None = None
    size_impact_bps: float = 0.0
    cost_confidence: float | None = None


IndependentBookExpectancyResolver = Callable[..., "IndependentBookExpectancy | None"]
IndependentBookScorer = Callable[..., float]


@dataclass(frozen=True, slots=True)
class ScoreStabilityMetrics:
    support_count: int
    min_score: float
    mean_score: float
    max_drawdown_bps: float
    stable: bool
    source: Literal["recent_target_history", "current_signal_confirmation"]
    max_score: float | None = None
    score_slope: float | None = None
    score_volatility_bps: float | None = None
    upward_excursion_bps: float | None = None
    downward_drawdown_bps: float | None = None


@dataclass(frozen=True, slots=True)
class IndependentExecutionPolicy:
    edge_strength: Literal["weak", "medium", "strong"]
    urgency: Literal["low", "medium", "high"]
    execution_style_preference: str | None
    order_type_preference: Literal["market", "limit"] | None
    time_in_force_preference: str | None
    limit_offset_bps_preference: Decimal | None
    max_acceptable_cost_bps: float | None
    policy_reason: str
    mode: str | None = None
    price_style: str | None = None
    passive_first: bool = False
    bounded_limit_ioc: bool = False
    bounded_taker: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IndependentEligibilityOutcome:
    eligible: bool
    hard_block_reasons: tuple[str, ...]
    soft_block_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    size_multiplier_cap: Decimal = Decimal("1")
    effective_safe_net_edge_bps: float | None = None
    effective_max_cost_bps: float | None = None


@dataclass(frozen=True, slots=True)
class IndependentSizingOutcome:
    target_qty: Decimal
    base_target_qty: Decimal
    size_multiplier: Decimal = Decimal("1")
    capital_multiplier: Decimal = Decimal("1")
    scale_in_allowed: bool = False
    sizing_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndependentBookDecision:
    leg: IndependentLeg
    expectancy: IndependentBookExpectancy | None
    score: float
    current_qty: Decimal
    target_qty: Decimal
    state: str
    reason_codes: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    min_hold_remaining_seconds: float
    rebalance_cooldown_remaining_seconds: float
    book_action: IndependentBookAction = "inactive"
    close_reason: str | None = None
    thesis_age_seconds: float | None = None
    weak_edge_report_only: bool = False
    liquidity_quality_score: float | None = None
    score_stability_metrics: ScoreStabilityMetrics | None = None
    execution_health_state: IndependentExecutionHealthState | None = None
    execution_policy: IndependentExecutionPolicy | None = None
    policy_reason: str | None = None
    score_raw: float | None = None
    score_adjusted: float | None = None
    eligibility: IndependentEligibilityOutcome | None = None
    sizing: IndependentSizingOutcome | None = None
    book_state: str | None = None
    guard_state: str | None = None
    holding_phase: str | None = None
    health_state: str | None = None
    prior_book_state: str | None = None
    prior_guard_state: str | None = None
    current_scale_in_count: int = 0
    current_de_risk_count: int = 0
    last_transition_reason: str | None = None
    last_transition_at: datetime | None = None
    suspended_until: datetime | None = None
    cooldown_until: datetime | None = None
    state_version: int = 1
    threshold_snapshot: "IndependentAdaptiveSnapshot | None" = None
    state_snapshot: "IndependentStateSnapshot | None" = None
    health_snapshot: "IndependentLegHealthSnapshot | None" = None
    replay_snapshot: "IndependentReplayDecisionSnapshot | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(str(item) for item in self.reason_codes))
        object.__setattr__(self, "blocked_reasons", tuple(str(item) for item in self.blocked_reasons))


IndependentBookEvaluation = IndependentBookDecision


@dataclass(frozen=True, slots=True)
class IndependentFamilyEvaluation:
    final_target_qty: Decimal
    legs: tuple[StrategyLegIntent, ...]
    overlay_decision: HedgeOverlayDecision
    long_book: IndependentBookDecision
    short_book: IndependentBookDecision
    book_runtime_states: tuple["IndependentBookRuntimeState", ...] = ()
    family_health: "IndependentFamilyHealthSnapshot | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "book_runtime_states", tuple(self.book_runtime_states))
