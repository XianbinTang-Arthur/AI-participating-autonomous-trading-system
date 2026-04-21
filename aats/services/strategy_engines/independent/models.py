from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from aats.schemas.decision import HedgeOverlayDecision
from aats.schemas.strategy_runtime import StrategyLegIntent

from .versioning import (
    INDEPENDENT_SCORE_STABILITY_SEMANTICS_VERSION,
    INDEPENDENT_STATE_MACHINE_VERSION,
)

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
    expected_lifecycle_cost_bps: float | None = None
    expected_lifecycle_net_edge_bps: float | None = None
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
    """Independent 策略家族的分数稳定性指标。

    字段语义（Task 142 显性化）：
    - ``upward_excursion_bps``：窗口内 mean_score → max_score 的**向上**偏移（bps）。
      新代码一律使用此字段来表达"向上偏移幅度"。
    - ``downward_drawdown_bps``：窗口内 max_score → min_score 的**向下**回撤（bps）。
      新代码一律使用此字段来表达"向下回撤幅度"。
    - ``max_drawdown_bps``：**【已弃用兼容字段】** 字面上"最大回撤"，但实际语义等于
      ``upward_excursion_bps``（向上偏移）。名字的历史遗留误导下游，新代码不得使用。
      保留目的：兼容反序列化老 replay 快照（semantics_version < 2），
      ``__post_init__`` 会自动镜像到 ``upward_excursion_bps`` 并把
      ``max_drawdown_bps_compat_source`` 设为 ``"upward_excursion_bps"``，
      下游可据此识别这个值来自 legacy 兼容路径。
    - ``max_drawdown_bps_compat_source``：兼容来源标记。非 None 表示本指标对象至少有一
      个值来自 legacy 兼容路径（可能是入参用了旧字段被映射为新字段，也可能反向）。
      下游可以 ``metrics.is_legacy_drawdown_compat`` 做显式 switch，对新字段做优先读。
    - ``semantics_version``：语义版本号。当前 = 2（引入 upward/downward split）。
      将来 >= 3 的版本会彻底移除 ``max_drawdown_bps``。
    """

    support_count: int
    min_score: float
    mean_score: float
    stable: bool
    source: Literal["recent_target_history", "current_signal_confirmation"]
    max_drawdown_bps: float | None = None
    max_score: float | None = None
    score_slope: float | None = None
    score_volatility_bps: float | None = None
    upward_excursion_bps: float | None = None
    downward_drawdown_bps: float | None = None
    max_drawdown_bps_compat_source: Literal["upward_excursion_bps"] | None = None
    semantics_version: int = INDEPENDENT_SCORE_STABILITY_SEMANTICS_VERSION

    def __post_init__(self) -> None:
        upward_excursion = (
            None
            if self.upward_excursion_bps is None
            else float(self.upward_excursion_bps)
        )
        compat_drawdown = (
            None
            if self.max_drawdown_bps is None
            else float(self.max_drawdown_bps)
        )
        if upward_excursion is None and compat_drawdown is not None:
            object.__setattr__(self, "upward_excursion_bps", compat_drawdown)
            upward_excursion = compat_drawdown
        if compat_drawdown is None and upward_excursion is not None:
            object.__setattr__(self, "max_drawdown_bps", upward_excursion)
            compat_drawdown = upward_excursion
        if compat_drawdown is not None and self.max_drawdown_bps_compat_source is None:
            object.__setattr__(self, "max_drawdown_bps_compat_source", "upward_excursion_bps")
        if self.semantics_version < INDEPENDENT_SCORE_STABILITY_SEMANTICS_VERSION:
            object.__setattr__(self, "semantics_version", INDEPENDENT_SCORE_STABILITY_SEMANTICS_VERSION)

    @property
    def is_legacy_drawdown_compat(self) -> bool:
        """当本指标对象经过 legacy ``max_drawdown_bps`` 兼容路径（含新→旧自动回填）
        即为 True。Task 142：下游显式 switch 用。"""
        return self.max_drawdown_bps_compat_source is not None


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
    state_version: int = INDEPENDENT_STATE_MACHINE_VERSION
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
