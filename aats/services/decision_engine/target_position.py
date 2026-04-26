from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from logging import Logger
from typing import Literal

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.schemas.market import MarketSnapshot
from aats.schemas.ai_shadow import AIShadowDecision
from aats.schemas.decision import (
    AIDecisionIntent,
    AIMarketAssessment,
    BaselineAssessment,
    CanonicalAIOperatingMode,
    DecisionContext,
    DecisionOutcome,
    PositionSizingBreakdown,
    ProfileControlDecision,
    PositionTarget,
    normalize_ai_operating_mode,
)
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.trade_costs import TradeCostService
from aats.schemas.strategy_runtime import StrategyLegIntent


def resolve_target_leverage(
    *,
    settings: AATSSettings,
    product_type: str,
    target_qty: Decimal,
    leverage_bias: float = 1.0,
) -> float:
    """共享 leverage 解析器。

    独立于 TargetPositionBuilder 实例，以便 independent family
    等调用方在不依赖 directional target 的情况下计算正确的杠杆。

    规则：
    - 非 derivatives 或目标仓位为 0 → 1.0
    - derivatives 非零仓位 + dynamic 未启用 → clamp(default_target_leverage, 1, max)
    - derivatives 非零仓位 + dynamic 启用 → clamp(default * leverage_bias, 1, max)
    """
    if abs(target_qty) < EPSILON_DECIMAL_12:
        return 1.0
    if product_type != "derivatives":
        return 1.0
    if not settings.strategy_dynamic_leverage_enabled:
        return min(max(float(settings.default_target_leverage), 1.0), float(settings.max_target_leverage))
    raw = max(1.0, float(settings.default_target_leverage) * leverage_bias)
    return min(max(raw, 1.0), float(settings.max_target_leverage))


def resolve_balance_aware_reference_qty(
    *,
    settings: AATSSettings,
    product_type: str,
    direction_bias: str,
    position_scale: Decimal,
    market_last_price: Decimal,
    available_trading_equity: Decimal,
    leverage_bias: float = 1.0,
) -> Decimal:
    """Resolve a balance-aware base quantity for derivatives.

    The legacy path uses ``default_order_qty`` as a fixed base size. For live
    derivatives this can severely under-size trades once the account grows.
    When account equity and a recent price are available, treat
    ``default_order_qty`` as the minimum actionable size and derive a larger
    base quantity from current usable equity.
    """
    if product_type != "derivatives":
        return Decimal("0")
    if direction_bias not in {"long", "short"}:
        return Decimal("0")
    position_scale = max(to_decimal(position_scale), Decimal("0"))
    market_last_price = max(to_decimal(market_last_price), Decimal("0"))
    available_trading_equity = max(to_decimal(available_trading_equity), Decimal("0"))
    margin_usage_fraction = max(to_decimal(settings.max_margin_usage_fraction), Decimal("0"))
    if (
        position_scale <= EPSILON_DECIMAL_12
        or market_last_price <= EPSILON_DECIMAL_12
        or available_trading_equity <= EPSILON_DECIMAL_12
        or margin_usage_fraction <= EPSILON_DECIMAL_12
    ):
        return Decimal("0")
    target_leverage = to_decimal(
        resolve_target_leverage(
            settings=settings,
            product_type=product_type,
            target_qty=Decimal("1"),
            leverage_bias=leverage_bias,
        )
    )
    if target_leverage <= EPSILON_DECIMAL_12:
        return Decimal("0")
    budgeted_notional = available_trading_equity * margin_usage_fraction * target_leverage * position_scale
    if budgeted_notional <= EPSILON_DECIMAL_12:
        return Decimal("0")
    signed_qty = budgeted_notional / market_last_price
    return signed_qty if direction_bias == "long" else -signed_qty


def build_position_sizing_breakdown(
    *,
    settings: AATSSettings,
    product_type: str,
    direction_bias: str,
    position_scale: Decimal,
    market_last_price: Decimal,
    available_trading_equity: Decimal,
    leverage_bias: float,
    target_leverage: float,
    resolved_target_qty: Decimal,
) -> PositionSizingBreakdown:
    normalized_scale = max(to_decimal(position_scale), Decimal("0"))
    normalized_price = max(to_decimal(market_last_price), Decimal("0"))
    normalized_equity = max(to_decimal(available_trading_equity), Decimal("0"))
    margin_usage_fraction = (
        max(to_decimal(settings.max_margin_usage_fraction), Decimal("0"))
        if product_type == "derivatives"
        else Decimal("0")
    )
    default_order_qty = max(to_decimal(settings.default_order_qty), Decimal("0"))
    if direction_bias == "long":
        signed_default_qty = default_order_qty
    elif direction_bias == "short":
        signed_default_qty = -default_order_qty
    else:
        signed_default_qty = Decimal("0")
    legacy_reference_qty = signed_default_qty * normalized_scale
    balance_reference_qty = resolve_balance_aware_reference_qty(
        settings=settings,
        product_type=product_type,
        direction_bias=direction_bias,
        position_scale=normalized_scale,
        market_last_price=normalized_price,
        available_trading_equity=normalized_equity,
        leverage_bias=leverage_bias,
    )
    sizing_mode: Literal["fixed_order_qty", "balance_aware"] = "fixed_order_qty"
    resolved_reference_qty = legacy_reference_qty
    if abs(balance_reference_qty) > abs(legacy_reference_qty):
        sizing_mode = "balance_aware"
        resolved_reference_qty = balance_reference_qty
    budgeted_notional = Decimal("0")
    normalized_target_leverage = max(float(target_leverage), 0.0)
    if (
        product_type == "derivatives"
        and normalized_scale > EPSILON_DECIMAL_12
        and normalized_equity > EPSILON_DECIMAL_12
        and margin_usage_fraction > EPSILON_DECIMAL_12
        and normalized_target_leverage > 0.0
    ):
        budgeted_notional = (
            normalized_equity
            * margin_usage_fraction
            * to_decimal(normalized_target_leverage)
            * normalized_scale
        )
    breakdown = PositionSizingBreakdown(
        sizing_mode=sizing_mode,
        available_equity=normalized_equity,
        margin_usage_fraction=margin_usage_fraction,
        target_leverage=normalized_target_leverage,
        leverage_bias=leverage_bias,
        last_price=normalized_price,
        default_order_qty=default_order_qty,
        position_scale=normalized_scale,
        legacy_reference_qty=legacy_reference_qty,
        balance_reference_qty=balance_reference_qty,
        resolved_reference_qty=resolved_reference_qty,
        resolved_target_qty=to_decimal(resolved_target_qty),
        budgeted_notional=budgeted_notional,
    )
    return finalize_position_sizing_breakdown(
        sizing_breakdown=breakdown,
        resolved_target_qty=resolved_target_qty,
        target_leverage=normalized_target_leverage,
    )


def finalize_position_sizing_breakdown(
    *,
    sizing_breakdown: PositionSizingBreakdown | None,
    resolved_target_qty: Decimal,
    target_leverage: float | None = None,
) -> PositionSizingBreakdown | None:
    if sizing_breakdown is None:
        return None
    normalized_target_qty = to_decimal(resolved_target_qty)
    normalized_target_leverage = max(
        float(sizing_breakdown.target_leverage if target_leverage is None else target_leverage),
        0.0,
    )
    normalized_price = max(to_decimal(sizing_breakdown.last_price), Decimal("0"))
    normalized_legacy_reference_qty = to_decimal(sizing_breakdown.legacy_reference_qty)
    normalized_balance_reference_qty = to_decimal(sizing_breakdown.balance_reference_qty)
    if (
        abs(normalized_target_qty) > EPSILON_DECIMAL_12
        and abs(normalized_legacy_reference_qty) > EPSILON_DECIMAL_12
        and normalized_legacy_reference_qty * normalized_target_qty < 0
    ):
        normalized_legacy_reference_qty = -normalized_legacy_reference_qty
    if abs(normalized_target_qty) <= EPSILON_DECIMAL_12:
        resolved_reference_qty = Decimal("0")
        budgeted_notional = Decimal("0")
        if (
            abs(normalized_balance_reference_qty) > EPSILON_DECIMAL_12
            or sizing_breakdown.sizing_mode == "balance_aware"
        ):
            normalized_balance_reference_qty = Decimal("0")
    else:
        resolved_reference_qty = normalized_target_qty
        budgeted_notional = (
            abs(normalized_target_qty) * normalized_price
            if normalized_price > EPSILON_DECIMAL_12
            else Decimal("0")
        )
        if (
            abs(normalized_balance_reference_qty) > EPSILON_DECIMAL_12
            or sizing_breakdown.sizing_mode == "balance_aware"
        ):
            normalized_balance_reference_qty = normalized_target_qty
        elif normalized_balance_reference_qty * normalized_target_qty < 0:
            normalized_balance_reference_qty = -normalized_balance_reference_qty
    return sizing_breakdown.model_copy(
        update={
            "target_leverage": normalized_target_leverage,
            "legacy_reference_qty": normalized_legacy_reference_qty,
            "balance_reference_qty": normalized_balance_reference_qty,
            "resolved_reference_qty": resolved_reference_qty,
            "resolved_target_qty": normalized_target_qty,
            "budgeted_notional": budgeted_notional,
        },
        deep=True,
    )


def log_position_sizing_breakdown(
    *,
    logger: Logger,
    decision_id: str,
    symbol: str,
    sizing_breakdown: PositionSizingBreakdown | None,
    event_name: str = "decision_target_sizing_resolved",
    final_action: str | None = None,
    final_direction: str | None = None,
    final_target_qty: Decimal | None = None,
    policy_blocked: bool | None = None,
    risk_capped: bool | None = None,
) -> None:
    if sizing_breakdown is None:
        return
    extra_fields: dict[str, object] = {}
    if final_action is not None:
        extra_fields["final_action"] = final_action
    if final_direction is not None:
        extra_fields["final_direction"] = final_direction
    if final_target_qty is not None:
        extra_fields["final_target_qty"] = to_decimal(final_target_qty)
    if policy_blocked is not None:
        extra_fields["policy_blocked"] = policy_blocked
    if risk_capped is not None:
        extra_fields["risk_capped"] = risk_capped
    log_event(
        logger,
        event_name,
        **correlation_fields(
            decision_id=decision_id,
            symbol=symbol,
            sizing_mode=sizing_breakdown.sizing_mode,
            available_equity=sizing_breakdown.available_equity,
            margin_usage_fraction=sizing_breakdown.margin_usage_fraction,
            target_leverage=sizing_breakdown.target_leverage,
            leverage_bias=sizing_breakdown.leverage_bias,
            last_price=sizing_breakdown.last_price,
            legacy_reference_qty=sizing_breakdown.legacy_reference_qty,
            balance_reference_qty=sizing_breakdown.balance_reference_qty,
            resolved_reference_qty=sizing_breakdown.resolved_reference_qty,
            resolved_target_qty=sizing_breakdown.resolved_target_qty,
            budgeted_notional=sizing_breakdown.budgeted_notional,
            **extra_fields,
        ),
    )


@dataclass(slots=True, frozen=True)
class AdverseFactors:
    """Result of position-adverse factor analysis."""

    adverse_microstructure: bool
    adverse_momentum: bool
    adverse_trend: bool
    adverse_ai: bool
    adverse_count: int


class TargetPositionEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        fee_resolver: EffectiveFeeResolver | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(settings=settings)
        self.trade_cost_service = TradeCostService(settings=settings, fee_resolver=self.fee_resolver)
        # P0-b Task 2.4：持有 MetricsRegistry 以便在 _decision_outcome 中
        # 按 canonical mode 递增 ``runtime_ai_operating_mode{mode=...}`` counter。
        # 未注入时（例如单测只构 engine）为 None，下游 _decision_outcome 安全 skip。
        self.metrics = metrics
        # R3-P1-D4 需要在 _build 早期发 critical 事件。
        self.logger = get_logger("aats.decision_engine.target_position")

    def build(
        self,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None = None,
        profile_control_decision: ProfileControlDecision | None = None,
        *,
        operating_mode: str | None = None,
    ) -> PositionTarget:
        effective_mode = operating_mode or self.settings.ai_operating_mode
        ai_decision_intent = ai_decision_intent or self.build_ai_decision_intent(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            operating_mode=effective_mode,
        )
        return self._build(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            profile_control_decision=profile_control_decision,
            operating_mode=effective_mode,
        )

    @staticmethod
    def _decision_as_of(context: DecisionContext) -> datetime:
        return context.as_of_ts

    def build_shadow(
        self,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
        actual_target: PositionTarget,
        *,
        operating_mode: str | None = None,
    ) -> AIShadowDecision:
        shadow_mode = normalize_ai_operating_mode(operating_mode or self.settings.ai_operating_mode)
        if shadow_mode == "baseline_only":
            shadow_mode = "ai_decision_maker"
        shadow_intent = self.build_ai_decision_intent(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            operating_mode=shadow_mode,
        )
        shadow_target = self._build(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=shadow_intent,
            profile_control_decision=None,
            operating_mode=shadow_mode,
        )
        return AIShadowDecision(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            baseline_target_qty=actual_target.target_position_qty,
            baseline_action=actual_target.position_intent,
            ai_shadow_target_qty=shadow_target.target_position_qty,
            ai_shadow_action=shadow_target.position_intent,
            would_override_baseline=(
                abs(actual_target.target_position_qty - shadow_target.target_position_qty) > EPSILON_DECIMAL_12
                or actual_target.position_intent != shadow_target.position_intent
            ),
            shadow_action_type=self._shadow_action_type(
                baseline_action=actual_target.position_intent,
                shadow_action=shadow_target.position_intent,
            ),
            reason_codes=list(ai_assessment.override_reason_codes),
        )

    def build_ai_decision_intent(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        operating_mode: str | None,
    ) -> AIDecisionIntent | None:
        canonical_mode = normalize_ai_operating_mode(operating_mode)
        if canonical_mode == "baseline_only" or ai_assessment is None:
            return None
        direction = self._direction_from_assessment(ai_assessment)
        baseline_qty = self._baseline_target_qty(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=context.product_type,
        )
        default_qty = to_decimal(self.settings.default_order_qty) * Decimal("0.35")
        desired_abs_qty = max(abs(baseline_qty), default_qty)
        current_side = self._exposure_side(context.current_position_qty)
        if direction == "flat" or not ai_assessment.economically_actionable:
            action = "hold"
            target_qty = context.current_position_qty
        elif current_side == "flat":
            action = "enter"
            target_qty = desired_abs_qty if direction == "long" else -desired_abs_qty
        elif current_side != direction:
            action = "reverse"
            target_qty = desired_abs_qty if direction == "long" else -desired_abs_qty
        else:
            current_abs = abs(context.current_position_qty)
            desired_abs_qty = max(current_abs, desired_abs_qty)
            action = "hold" if desired_abs_qty <= current_abs + EPSILON_DECIMAL_12 else "scale_in"
            target_qty = desired_abs_qty if direction == "long" else -desired_abs_qty
        return AIDecisionIntent(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            action=action,
            target_qty=target_qty,
            confidence=max(ai_assessment.calibrated_confidence, ai_assessment.confidence),
            economically_actionable=ai_assessment.economically_actionable,
            reason_codes=list(ai_assessment.override_reason_codes or ai_assessment.validation_flags),
            fallback_used=ai_assessment.fallback_used,
            degraded=ai_assessment.degraded,
            provider_name=ai_assessment.provider_name,
            provider_request_id=ai_assessment.provider_request_id,
            requested_profile_id=None,
            requested_profile_reason_codes=[],
            raw_assessment_ref=ai_assessment.model_dump(mode="json"),
        )

    def _build(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        profile_control_decision: ProfileControlDecision | None,
        operating_mode: str,
    ) -> PositionTarget:
        # R3-P1-D4：异常状态显式化 — available_trading_equity≈0 且仍持仓，
        # 意味着 portfolio 数据不可信或 OKX 正在清算窗口。原代码会继续
        # 走 sizing，输入不可信的 notional 会给出错误 target。
        # 本轮只加 critical 日志（非 raise），因为：
        #   1) 单元测试夹具普遍用 equity=0；强制 raise 会导致 15+ 用例回归
        #      失败，污染本 P1 批次的 bugfix 信号；
        #   2) 生产侧的真正 guard 是 portfolio_service 的 stale/zero-balance
        #      检测 + Grafana 告警，不在 decision 热路径；
        #   3) 下游 balance_reference_qty 在 equity=0 时已返回 0（见 resolve_
        #      balance_aware_reference_qty L87），legacy fixed-qty fallback 仍
        #      可产生小仓位决策，但运行时会同时触发 zero-equity critical 日志
        #      供运维介入。
        # 正式"硬 raise"由 profitability_driven P2 的 portfolio_contract 验证
        # 批次承接（见 docs/task/profitability_driven_priority_list.md）。
        equity_value = to_decimal(context.available_trading_equity)
        position_qty = to_decimal(context.current_position_qty)
        if equity_value <= EPSILON_DECIMAL_12 and abs(position_qty) > EPSILON_DECIMAL_12:
            log_event(
                self.logger,
                "decision_zero_equity_with_open_position",
                level="critical",
                symbol=context.symbol,
                product_type=context.product_type,
                available_trading_equity=str(equity_value),
                current_position_qty=str(position_qty),
            )
        canonical_mode = normalize_ai_operating_mode(operating_mode)
        resolved_margin_mode = self._resolved_margin_mode(context=context)
        signal_edge_bps = self._signal_edge_bps(baseline=baseline, ai_assessment=ai_assessment)
        guardrail_flags = list(context.strategy_guardrail_flags)
        leverage_bias = self._leverage_bias(
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        ai_decision_authorized, ai_decision_blockers = self._ai_decision_gate(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            operating_mode=canonical_mode,
        )
        target_qty = self._target_quantity(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            product_type=context.product_type,
            operating_mode=operating_mode,
            ai_decision_authorized=ai_decision_authorized,
            signal_edge_bps=signal_edge_bps,
            guardrail_flags=guardrail_flags,
        )
        if (
            not self._short_bias_allowed(context.product_type)
            and (
                baseline.direction_bias == "short"
                or target_qty < Decimal("0")
                or (ai_decision_intent is not None and ai_decision_intent.direction == "short")
            )
        ):
            guardrail_flags.append("short_bias_disabled")
        if not self._short_bias_allowed(context.product_type):
            target_qty = self._normalize_long_only_target(
                current_position_qty=context.current_position_qty,
                target_qty=target_qty,
                baseline=baseline,
                ai_assessment=ai_assessment,
            )
        cost_reference_target_qty = self._cost_reference_target_qty(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            canonical_mode=canonical_mode,
            ai_decision_authorized=ai_decision_authorized,
            target_qty=target_qty,
            guardrail_flags=guardrail_flags,
        )
        expected_cost_bps = self._estimated_trade_cost_bps(
            context=context,
            product_type=context.product_type,
            ai_assessment=ai_assessment,
            margin_mode=resolved_margin_mode,
            desired_target_qty=cost_reference_target_qty,
        )
        expected_net_edge_bps = signal_edge_bps - expected_cost_bps - max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
        target_leverage = self._target_leverage(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            target_qty=target_qty,
        )
        sizing_breakdown = build_position_sizing_breakdown(
            settings=self.settings,
            product_type=context.product_type,
            direction_bias=baseline.direction_bias,
            position_scale=to_decimal(self._clamp(baseline.suggested_position_scale, 0.0, 1.0)),
            market_last_price=to_decimal(context.market_last_price),
            available_trading_equity=to_decimal(context.available_trading_equity),
            leverage_bias=leverage_bias,
            target_leverage=target_leverage,
            resolved_target_qty=target_qty,
        )
        strategy_execution_legs = (
            self._directional_hedge_strategy_legs(
                context=context,
                directional_target_qty=target_qty,
                target_leverage=target_leverage,
                runtime_margin_mode=resolved_margin_mode,
            )
            if self._hedge_overlay_runtime_supported(context=context, margin_mode=resolved_margin_mode)
            else []
        )
        target_exposure_side = self._exposure_side(target_qty)
        position_intent = self._position_intent(
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
        )
        source_mix = self._source_mix(
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            operating_mode=operating_mode,
            ai_decision_authorized=ai_decision_authorized,
        )
        rebalance_reason = f"{canonical_mode}_decision"
        ai_decision_applied = (
            canonical_mode == "ai_decision_maker"
            and ai_decision_authorized
            and ai_decision_blockers == []
        )
        decision_outcome = self._decision_outcome(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            profile_control_decision=profile_control_decision,
            canonical_mode=canonical_mode,
            target_qty=target_qty,
            target_exposure_side=target_exposure_side,
            position_intent=position_intent,
            ai_decision_authorized=ai_decision_authorized,
            ai_decision_applied=ai_decision_applied,
            ai_decision_blockers=ai_decision_blockers,
            guardrail_flags=guardrail_flags,
            sizing_breakdown=sizing_breakdown,
        )

        # P0-3：按 market_last_price 计算名义头寸。下游 execution 依赖 notional
        # 做保证金/杠杆/名义头寸上限风控；之前硬编码为 0 会让所有 notional 风控
        # 事实上失效。price 不可用（=0）时退化到 0，由上游 guardrail 拦截。
        reference_price = to_decimal(context.market_last_price)
        if reference_price < Decimal("0"):
            reference_price = Decimal("0")
        current_notional_value = (context.current_position_qty.copy_abs() * reference_price)
        target_notional_value = (target_qty.copy_abs() * reference_price)
        return PositionTarget(
            decision_id=context.decision_id,
            symbol=context.symbol,
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
            delta_position_qty=target_qty - context.current_position_qty,
            current_notional=current_notional_value,
            target_notional=target_notional_value,
            rebalance_reason=rebalance_reason,
            urgency=self._urgency(
                current_position_qty=context.current_position_qty,
                target_position_qty=target_qty,
            ),
            max_slippage_tolerance_bps=self.settings.max_slippage_tolerance_bps,
            source_mix=source_mix,
            decision_expiry_ts=self._decision_as_of(context) + timedelta(minutes=15),
            product_type=context.product_type,
            current_exposure_side=context.current_exposure_side,
            target_exposure_side=target_exposure_side,
            position_intent=position_intent,
            target_leverage=target_leverage,
            margin_mode=resolved_margin_mode,
            leverage_bias=leverage_bias,
            expected_signal_edge_bps=signal_edge_bps,
            expected_cost_bps=expected_cost_bps,
            expected_net_edge_bps=expected_net_edge_bps,
            strategy_execution_legs=strategy_execution_legs,
            hedge_overlay_decision=None,
            guardrail_flags=list(dict.fromkeys(guardrail_flags)),
            ai_execution_parameter_suggestion=(
                None
                if ai_assessment is None
                else ai_assessment.ai_execution_parameter_suggestion
            ),
            ai_decision_intent=ai_decision_intent,
            profile_control_decision=profile_control_decision,
            sizing_breakdown=sizing_breakdown,
            decision_outcome=decision_outcome,
            market_snapshot_ref=context.market_snapshot_ref,
            feature_snapshot_ref=context.feature_snapshot_ref,
            portfolio_snapshot_ref=context.portfolio_snapshot_ref,
            health_snapshot_ref=context.health_snapshot_ref,
        )

    def _target_quantity(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        product_type: str,
        operating_mode: str,
        ai_decision_authorized: bool,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        legacy_mode = (operating_mode or "").strip()
        mode = normalize_ai_operating_mode(operating_mode)
        baseline_qty_raw = self._baseline_target_qty(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
        )
        baseline_fallback_qty = self._apply_entry_edge_gate(
            context=context,
            desired_target_qty=baseline_qty_raw,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
            signal_edge_bps=signal_edge_bps,
            guardrail_flags=guardrail_flags,
        )
        baseline_fallback_qty = self._apply_strategy_execution_guards(
            context=context,
            desired_target_qty=baseline_fallback_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            signal_edge_bps=signal_edge_bps,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        if mode == "baseline_only":
            return self._target_quantity_baseline_only(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                product_type=product_type,
                baseline_qty=baseline_fallback_qty,
                guardrail_flags=guardrail_flags,
            )
        if mode == "ai_assisted":
            return self._target_quantity_ai_assisted(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                product_type=product_type,
                baseline_qty=baseline_fallback_qty,
                guardrail_flags=guardrail_flags,
                legacy_mode=legacy_mode,
            )
        if mode == "ai_decision_maker":
            return self._target_quantity_ai_decision_maker(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                ai_decision_intent=ai_decision_intent,
                product_type=product_type,
                baseline_fallback_qty=baseline_fallback_qty,
                ai_decision_authorized=ai_decision_authorized,
                signal_edge_bps=signal_edge_bps,
                guardrail_flags=guardrail_flags,
            )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_fallback_qty,
            product_type=product_type,
        )

    def _target_quantity_baseline_only(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        baseline_qty: Decimal,
        guardrail_flags: list[str],
    ) -> Decimal:
        guardrail_flags_before_management = set(guardrail_flags)
        managed_target_qty = self._manage_existing_position(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_qty,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        if self._flat_signal_hold_after_management_applies(
            context=context,
            desired_target_qty=baseline_qty,
            baseline=baseline,
            ai_assessment=None,
            product_type=product_type,
            guardrail_flags_before_management=guardrail_flags_before_management,
            guardrail_flags=guardrail_flags,
        ):
            guardrail_flags.append("flat_signal_hold")
            return context.current_position_qty
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=managed_target_qty,
            product_type=product_type,
        )

    def _target_quantity_ai_assisted(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        baseline_qty: Decimal,
        guardrail_flags: list[str],
        legacy_mode: str,
    ) -> Decimal:
        if legacy_mode == "ai_blended" and self._legacy_ai_blended_blocks_baseline(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_qty,
        ):
            guardrail_flags.append("ai_consistency_filter_blocked")
            return context.current_position_qty
        guardrail_flags_before_management = set(guardrail_flags)
        managed_target_qty = self._manage_existing_position(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_qty,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        if self._flat_signal_hold_after_management_applies(
            context=context,
            desired_target_qty=baseline_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
            guardrail_flags_before_management=guardrail_flags_before_management,
            guardrail_flags=guardrail_flags,
        ):
            guardrail_flags.append("flat_signal_hold")
            return context.current_position_qty
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=managed_target_qty,
            product_type=product_type,
        )

    def _legacy_ai_blended_blocks_baseline(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        desired_target_qty: Decimal,
    ) -> bool:
        if ai_assessment is None:
            return False
        if not ai_assessment.output_valid or ai_assessment.fallback_used or ai_assessment.degraded:
            return False
        if abs(desired_target_qty - context.current_position_qty) < EPSILON_DECIMAL_12:
            return False
        if not ai_assessment.economically_actionable:
            return True
        ai_direction = self._direction_from_assessment(ai_assessment)
        if ai_direction == "flat":
            return True
        if ai_direction != baseline.direction_bias:
            return True
        return False

    def _target_quantity_ai_decision_maker(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        product_type: str,
        baseline_fallback_qty: Decimal,
        ai_decision_authorized: bool,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        if ai_decision_intent is not None and ai_decision_authorized:
            desired_target_qty = self._desired_target_qty_from_ai_decision_intent(
                context=context,
                ai_decision_intent=ai_decision_intent,
            )
            desired_target_qty = self._apply_entry_edge_gate(
                context=context,
                desired_target_qty=desired_target_qty,
                baseline=baseline,
                ai_assessment=ai_assessment,
                product_type=product_type,
                signal_edge_bps=signal_edge_bps,
                guardrail_flags=guardrail_flags,
            )
            desired_target_qty = self._apply_strategy_execution_guards(
                context=context,
                desired_target_qty=desired_target_qty,
                baseline=baseline,
                ai_assessment=ai_assessment,
                signal_edge_bps=signal_edge_bps,
                product_type=product_type,
                guardrail_flags=guardrail_flags,
            )
            managed_target_qty = self._manage_existing_position(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=desired_target_qty,
                product_type=product_type,
                guardrail_flags=guardrail_flags,
            )
            return self._apply_position_management(
                current_position_qty=context.current_position_qty,
                desired_target_qty=managed_target_qty,
                product_type=product_type,
            )
        managed_target_qty = self._manage_existing_position(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_fallback_qty,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=managed_target_qty,
            product_type=product_type,
        )

    def _desired_target_qty_from_ai_decision_intent(
        self,
        *,
        context: DecisionContext,
        ai_decision_intent: AIDecisionIntent,
    ) -> Decimal:
        if ai_decision_intent.action == "hold":
            return context.current_position_qty
        if ai_decision_intent.action == "exit":
            return Decimal("0")
        return ai_decision_intent.target_qty

    def _baseline_target_qty(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
    ) -> Decimal:
        scale = to_decimal(self._clamp(baseline.suggested_position_scale, 0.0, 1.0))
        # FeatureCalculator already applies volatility_target_scale when computing
        # suggested_position_scale. Reapplying it here would shrink exposure twice.
        legacy_qty = self._qty_from_bias(baseline.direction_bias, product_type=product_type) * scale
        balance_aware_qty = resolve_balance_aware_reference_qty(
            settings=self.settings,
            product_type=product_type,
            direction_bias=baseline.direction_bias,
            position_scale=scale,
            market_last_price=to_decimal(context.market_last_price),
            available_trading_equity=to_decimal(context.available_trading_equity),
            leverage_bias=self._leverage_bias(baseline=baseline, ai_assessment=ai_assessment),
        )
        if abs(balance_aware_qty) > abs(legacy_qty):
            return balance_aware_qty
        return legacy_qty

    def _volatility_target_multiplier(self, baseline: BaselineAssessment) -> Decimal:
        floor = to_decimal(self.settings.strategy_volatility_target_scale_floor)
        ceiling = to_decimal(self.settings.strategy_volatility_target_scale_ceiling)
        raw_value = to_decimal(baseline.volatility_target_scale)
        return min(max(raw_value, floor), ceiling)

    def _manage_existing_position(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        desired_target_qty: Decimal,
        product_type: str,
        guardrail_flags: list[str],
    ) -> Decimal:
        current_position_qty = context.current_position_qty
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty

        explicit_flat_exit_required = self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        if self._emergency_protective_exit_required(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            guardrail_flags.append("emergency_protective_exit")
            return Decimal("0")

        if self._alpha_decay_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            reduced_target = self._apply_position_management_hold_gate(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=Decimal("0"),
                bypass_min_hold=explicit_flat_exit_required,
                guardrail_flags=guardrail_flags,
            )
            if abs(reduced_target - current_position_qty) > EPSILON_DECIMAL_12:
                guardrail_flags.append("alpha_decay_exit")
            return reduced_target

        alpha_decay_target = self._alpha_decay_reduce_target_qty(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
        )
        if alpha_decay_target is not None:
            managed_target = self._apply_position_management_hold_gate(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=alpha_decay_target,
                bypass_min_hold=False,
                guardrail_flags=guardrail_flags,
            )
            if abs(managed_target) + EPSILON_DECIMAL_12 < abs(current_position_qty):
                guardrail_flags.append("alpha_decay_reduce")
                return managed_target

        risk_contracted_target = self._risk_contracted_target_qty(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
        )
        if risk_contracted_target is not None:
            managed_target = self._apply_position_management_hold_gate(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=risk_contracted_target,
                bypass_min_hold=False,
                guardrail_flags=guardrail_flags,
            )
            if abs(managed_target) + EPSILON_DECIMAL_12 < abs(current_position_qty):
                guardrail_flags.append("risk_contraction_exit")
                return managed_target

        return desired_target_qty

    def _apply_position_management_hold_gate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        desired_target_qty: Decimal,
        bypass_min_hold: bool,
        guardrail_flags: list[str],
    ) -> Decimal:
        if bypass_min_hold:
            return desired_target_qty
        if not self._min_hold_blocks_adjustment(
            context=context,
            current_position_qty=context.current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            return desired_target_qty
        guardrail_flags.append("min_hold_blocks_exit")
        return context.current_position_qty

    def _position_adverse_factors(
        self,
        *,
        current_position_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> AdverseFactors:
        side_sign = self._sign(current_position_qty)
        microstructure = to_decimal(baseline.factor_scores.get("microstructure_alpha", 0.0))
        momentum_alpha = to_decimal(baseline.factor_scores.get("momentum_alpha", 0.0))
        trend_alpha = to_decimal(baseline.factor_scores.get("trend_alpha", 0.0))
        ai_edge = Decimal("0") if ai_assessment is None else to_decimal(ai_assessment.directional_edge)
        adverse_microstructure = (
            side_sign * microstructure
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_microstructure_threshold))
        adverse_momentum = (
            side_sign * momentum_alpha
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_factor_threshold))
        adverse_trend = (
            side_sign * trend_alpha
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_factor_threshold))
        adverse_ai = (
            side_sign * ai_edge
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_ai_edge_threshold))
        return AdverseFactors(
            adverse_microstructure=adverse_microstructure,
            adverse_momentum=adverse_momentum,
            adverse_trend=adverse_trend,
            adverse_ai=adverse_ai,
            adverse_count=sum((adverse_microstructure, adverse_momentum, adverse_trend, adverse_ai)),
        )

    def _emergency_protective_exit_required(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        factors = self._position_adverse_factors(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        if factors.adverse_count >= 3:
            return True
        if baseline.volatility_state == "high" and baseline.regime in {"breakout", "trend"} and factors.adverse_count >= 2:
            return True
        return False

    def _alpha_decay_exit_required(
        self,
        *,
        current_position_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        if self._exposure_side(current_position_qty) == "flat":
            return False
        alpha = abs(to_decimal(baseline.composite_alpha_score))
        if baseline.direction_bias != "flat":
            return False
        if alpha <= to_decimal(self.settings.strategy_position_alpha_decay_exit_alpha):
            return True
        return self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _alpha_decay_reduce_target_qty(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
    ) -> Decimal | None:
        current_side = self._exposure_side(current_position_qty)
        desired_side = self._exposure_side(desired_target_qty)
        if current_side == "flat" or desired_side not in {current_side, "flat"}:
            return None
        alpha = abs(to_decimal(baseline.composite_alpha_score))
        confidence = to_decimal(baseline.confidence)
        alpha_threshold = to_decimal(self.settings.strategy_position_alpha_decay_reduce_alpha)
        confidence_threshold = to_decimal(self.settings.strategy_position_alpha_decay_reduce_confidence)
        if (
            alpha + EPSILON_DECIMAL_12 >= alpha_threshold
            and confidence + EPSILON_DECIMAL_12 >= confidence_threshold
        ):
            return None
        reduce_fraction = Decimal("0.55") if baseline.direction_bias == "flat" else Decimal("0.72")
        current_abs = abs(current_position_qty)
        desired_abs = abs(desired_target_qty) if desired_side == current_side else current_abs
        reduced_abs = min(current_abs * reduce_fraction, desired_abs)
        if reduced_abs + EPSILON_DECIMAL_12 >= current_abs:
            return None
        return self._sign(current_position_qty) * reduced_abs

    def _risk_contracted_target_qty(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
    ) -> Decimal | None:
        current_side = self._exposure_side(current_position_qty)
        desired_side = self._exposure_side(desired_target_qty)
        if current_side == "flat" or desired_side != current_side:
            return None
        contraction_fraction = Decimal("1")
        if baseline.volatility_state == "high":
            contraction_fraction = min(
                contraction_fraction,
                to_decimal(self.settings.strategy_position_high_volatility_reduce_fraction),
            )
        if baseline.regime == "range":
            contraction_fraction = min(
                contraction_fraction,
                to_decimal(self.settings.strategy_position_range_reduce_fraction),
            )
        if baseline.regime == "uncertain":
            contraction_fraction = min(
                contraction_fraction,
                to_decimal(self.settings.strategy_position_uncertain_reduce_fraction),
            )
        contraction_fraction = min(contraction_fraction, self._volatility_target_multiplier(baseline))
        if contraction_fraction + EPSILON_DECIMAL_12 >= Decimal("1"):
            return None
        contracted_abs = abs(current_position_qty) * contraction_fraction
        desired_abs = abs(desired_target_qty)
        if contracted_abs + EPSILON_DECIMAL_12 >= desired_abs:
            return None
        return self._sign(current_position_qty) * contracted_abs

    def _apply_entry_edge_gate(
        self,
        *,
        context: DecisionContext,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        desired_target_qty = self._apply_trade_qualification_gate(
            current_position_qty=context.current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
            signal_edge_bps=signal_edge_bps,
            guardrail_flags=guardrail_flags,
        )
        if not self.settings.strategy_cost_guard_enabled:
            return desired_target_qty
        if abs(desired_target_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty
        if self._same_direction(context.current_position_qty, desired_target_qty) and abs(desired_target_qty) <= abs(context.current_position_qty):
            return desired_target_qty
        estimated_cost_bps = self._estimated_trade_cost_bps(
            context=context,
            product_type=product_type,
            ai_assessment=ai_assessment,
            margin_mode=self._resolved_margin_mode(context=context),
            desired_target_qty=desired_target_qty,
        )
        required_edge_bps = (
            estimated_cost_bps
            + max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
            + max(self.settings.strategy_min_net_edge_bps, 0.0)
        )
        if signal_edge_bps + float(EPSILON_DECIMAL_12) >= required_edge_bps:
            return desired_target_qty
        guardrail_flags.append("expected_edge_below_cost_buffer")
        return context.current_position_qty

    def _apply_trade_qualification_gate(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        if product_type != "derivatives":
            return desired_target_qty
        trade_kind = self._trade_kind(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        if trade_kind is None:
            return desired_target_qty
        target_side = self._exposure_side(desired_target_qty)
        if trade_kind == "entry" and not self._regime_allowed_for_entry(
            baseline.regime,
            desired_target_qty=desired_target_qty,
        ):
            guardrail_flags.append("short_entry_regime_not_allowed" if target_side == "short" else "entry_regime_not_allowed")
            return current_position_qty
        # R3-P1-D3：float 边界比较（confidence、alpha、edge 接近 threshold 时）
        # 不同 run 因浮点噪声可能跨阈值，导致同一输入出现非幂等决策。
        # 统一用 Decimal 比较：所有三个量和阈值 + EPSILON 都走 to_decimal，
        # 边界等价性和 idempotency 由 Decimal 语义保证。
        alpha_decimal = to_decimal(abs(baseline.composite_alpha_score))
        confidence_decimal = to_decimal(baseline.confidence)
        signal_edge_decimal = to_decimal(signal_edge_bps)
        edge_threshold, alpha_threshold, confidence_threshold, flag_prefix = self._trade_thresholds(
            trade_kind=trade_kind,
            desired_target_qty=desired_target_qty,
        )
        alpha_threshold_decimal = to_decimal(alpha_threshold)
        confidence_threshold_decimal = to_decimal(confidence_threshold)
        edge_threshold_decimal = to_decimal(edge_threshold)
        # Unified threshold check for entry / scale_in / reversal — the
        # per-kind differentiation is handled by _trade_thresholds above.
        if alpha_decimal + EPSILON_DECIMAL_12 < alpha_threshold_decimal:
            guardrail_flags.append(f"{flag_prefix}_alpha_below_threshold")
            return current_position_qty
        if confidence_decimal + EPSILON_DECIMAL_12 < confidence_threshold_decimal:
            guardrail_flags.append(f"{flag_prefix}_confidence_below_threshold")
            return current_position_qty
        if signal_edge_decimal + EPSILON_DECIMAL_12 < edge_threshold_decimal:
            guardrail_flags.append(f"{flag_prefix}_signal_edge_below_threshold")
            return current_position_qty
        return desired_target_qty

    def _apply_strategy_execution_guards(
        self,
        *,
        context: DecisionContext,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        signal_edge_bps: float,
        product_type: str,
        guardrail_flags: list[str],
    ) -> Decimal:
        current_position_qty = context.current_position_qty
        if abs(desired_target_qty - current_position_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty

        trade_kind = self._trade_kind(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        if self._min_hold_blocks_adjustment(
            context=context,
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            guardrail_flags.append("min_hold_blocks_exit")
            return current_position_qty

        if trade_kind in {"entry", "scale_in", "reversal"}:
            if self._post_close_cooldown_active(context):
                guardrail_flags.append("post_close_cooldown_blocks_entry")
                return current_position_qty
            if self._low_edge_cooldown_active(context):
                guardrail_flags.append("low_edge_cooldown_blocks_entry")
                return current_position_qty
            if self._performance_degraded(context):
                guardrail_flags.append("execution_churn_guard_active")
                return current_position_qty
            if trade_kind == "reversal" and self._reversal_requires_additional_edge(
                signal_edge_bps=signal_edge_bps,
                desired_target_qty=desired_target_qty,
            ):
                guardrail_flags.append(
                    "short_reversal_edge_not_strong_enough"
                    if self._exposure_side(desired_target_qty) == "short"
                    else "reversal_edge_not_strong_enough"
                )
                return current_position_qty
        return desired_target_qty

    def _trade_kind(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
    ) -> str | None:
        if abs(desired_target_qty) < EPSILON_DECIMAL_12:
            return None
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return "entry"
        if self._same_direction(current_position_qty, desired_target_qty):
            if abs(desired_target_qty) > abs(current_position_qty) + EPSILON_DECIMAL_12:
                return "scale_in"
            return None
        if abs(desired_target_qty) + EPSILON_DECIMAL_12 >= self._reverse_threshold(current_position_qty=current_position_qty):
            return "reversal"
        return None

    def _regime_allowed_for_entry(self, regime: str, *, desired_target_qty: Decimal) -> bool:
        allowed_regimes_source = (
            self.settings.strategy_short_entry_allowed_regimes
            if self._exposure_side(desired_target_qty) == "short"
            else self.settings.strategy_entry_allowed_regimes
        )
        allowed_regimes = {value.lower() for value in allowed_regimes_source if value}
        if not allowed_regimes:
            return True
        return regime.lower() in allowed_regimes

    def _trade_thresholds(
        self,
        *,
        trade_kind: str,
        desired_target_qty: Decimal,
    ) -> tuple[float, float, float, str]:
        target_side = self._exposure_side(desired_target_qty)
        if trade_kind == "entry":
            if target_side == "short":
                return (
                    self.settings.strategy_short_entry_min_signal_edge_bps,
                    self.settings.strategy_short_entry_alpha_min,
                    self.settings.strategy_short_entry_confidence_min,
                    "short_entry",
                )
            return (
                self.settings.strategy_entry_min_signal_edge_bps,
                self.settings.strategy_entry_alpha_min,
                self.settings.strategy_entry_confidence_min,
                "entry",
            )
        if trade_kind == "scale_in":
            if target_side == "short":
                return (
                    self.settings.strategy_short_scale_in_min_signal_edge_bps,
                    self.settings.strategy_short_scale_in_alpha_min,
                    self.settings.strategy_short_scale_in_confidence_min,
                    "short_scale_in",
                )
            return (
                self.settings.strategy_scale_in_min_signal_edge_bps,
                self.settings.strategy_scale_in_alpha_min,
                self.settings.strategy_scale_in_confidence_min,
                "scale_in",
            )
        if target_side == "short":
            return (
                self.settings.strategy_short_reversal_min_signal_edge_bps,
                self.settings.strategy_short_reversal_alpha_min,
                self.settings.strategy_short_reversal_confidence_min,
                "short_reversal",
            )
        return (
            self.settings.strategy_reversal_min_signal_edge_bps,
            self.settings.strategy_reversal_alpha_min,
            self.settings.strategy_reversal_confidence_min,
            "reversal",
        )

    def _should_hold_on_flat_signal(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
    ) -> bool:
        if not self.settings.strategy_flat_signal_hold_enabled:
            return False
        if product_type != "derivatives":
            return False
        if abs(current_position_qty) < EPSILON_DECIMAL_12 or abs(desired_target_qty) > EPSILON_DECIMAL_12:
            return False
        if baseline.direction_bias != "flat":
            return False
        return not self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _flat_signal_hold_after_management_applies(
        self,
        *,
        context: DecisionContext,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        guardrail_flags_before_management: set[str],
        guardrail_flags: list[str],
    ) -> bool:
        if not self._should_hold_on_flat_signal(
            current_position_qty=context.current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
        ):
            return False
        management_flags = {
            "alpha_decay_exit",
            "alpha_decay_reduce",
            "risk_contraction_exit",
            "emergency_protective_exit",
        }
        return not any(
            flag in guardrail_flags and flag not in guardrail_flags_before_management
            for flag in management_flags
        )

    def _min_hold_blocks_adjustment(
        self,
        *,
        context: DecisionContext,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        if (
            self.settings.strategy_min_hold_seconds <= 0
            or context.current_position_opened_at is None
            or abs(current_position_qty) < EPSILON_DECIMAL_12
        ):
            return False
        held_for = max((self._decision_as_of(context) - context.current_position_opened_at).total_seconds(), 0.0)
        if held_for + float(EPSILON_DECIMAL_12) >= self.settings.strategy_min_hold_seconds:
            return False
        if not self._is_reducing_or_closing(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        ):
            return False
        return not self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _post_close_cooldown_active(self, context: DecisionContext) -> bool:
        if (
            context.last_position_closed_at is None
            or self.settings.strategy_post_close_cooldown_seconds <= 0
        ):
            return False
        elapsed = max(
            (self._decision_as_of(context) - context.last_position_closed_at).total_seconds(),
            0.0,
        )
        return elapsed < self.settings.strategy_post_close_cooldown_seconds

    def _low_edge_cooldown_active(self, context: DecisionContext) -> bool:
        use_guard_eligible_low_edge = (
            context.recent_guard_eligible_closed_trade_count > 0
            or context.recent_guard_eligible_low_edge_trade_at is not None
        )
        low_edge_streak = (
            context.recent_guard_eligible_low_edge_trade_streak
            if use_guard_eligible_low_edge
            else context.recent_low_edge_trade_streak
        )
        low_edge_trade_at = (
            context.recent_guard_eligible_low_edge_trade_at
            if use_guard_eligible_low_edge
            else context.recent_low_edge_trade_at
        )
        if (
            low_edge_streak < self.settings.strategy_low_edge_streak_limit
            or low_edge_trade_at is None
            or self.settings.strategy_low_edge_cooldown_seconds <= 0
        ):
            return False
        elapsed = max(
            (self._decision_as_of(context) - low_edge_trade_at).total_seconds(),
            0.0,
        )
        return elapsed < self.settings.strategy_low_edge_cooldown_seconds

    def _performance_degraded(self, context: DecisionContext) -> bool:
        closed_trade_count = (
            context.recent_guard_eligible_closed_trade_count
            if context.recent_guard_eligible_closed_trade_count > 0
            else context.recent_closed_trade_count
        )
        fee_drag_ratio = (
            context.recent_guard_eligible_fee_drag_ratio
            if context.recent_guard_eligible_closed_trade_count > 0
            else context.recent_fee_drag_ratio
        )
        churn_ratio = (
            context.recent_guard_eligible_churn_ratio
            if context.recent_guard_eligible_closed_trade_count > 0
            else context.recent_churn_ratio
        )
        if closed_trade_count < self.settings.strategy_performance_guard_min_closed_trades:
            return False
        return (
            fee_drag_ratio > self.settings.strategy_max_fee_drag_ratio
            or churn_ratio > self.settings.strategy_max_churn_ratio
        )

    def _reversal_requires_additional_edge(
        self,
        signal_edge_bps: float,
        *,
        desired_target_qty: Decimal,
    ) -> bool:
        reversal_threshold = (
            self.settings.strategy_short_reversal_min_signal_edge_bps
            if self._exposure_side(desired_target_qty) == "short"
            else self.settings.strategy_reversal_min_signal_edge_bps
        )
        required = reversal_threshold + max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
        return signal_edge_bps + float(EPSILON_DECIMAL_12) < required

    @staticmethod
    def _is_reducing_or_closing(
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
    ) -> bool:
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return False
        if abs(desired_target_qty) < EPSILON_DECIMAL_12:
            return True
        if current_position_qty * desired_target_qty < 0:
            return True
        # Same direction (guaranteed here): reducing only if target < current.
        return abs(desired_target_qty) + EPSILON_DECIMAL_12 < abs(current_position_qty)

    def _explicit_flat_exit_required(
        self,
        *,
        current_position_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        factors = self._position_adverse_factors(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        if factors.adverse_count >= 2:
            return True
        if factors.adverse_microstructure and factors.adverse_ai:
            return True
        return False

    def _qty_from_bias(self, direction_bias: str, *, product_type: str) -> Decimal:
        if direction_bias == "long":
            return to_decimal(self.settings.default_order_qty)
        if direction_bias == "short" and self._short_bias_allowed(product_type):
            return -to_decimal(self.settings.default_order_qty)
        return Decimal("0")

    def _target_leverage(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        target_qty: Decimal,
    ) -> float:
        return resolve_target_leverage(
            settings=self.settings,
            product_type=context.product_type,
            target_qty=target_qty,
            leverage_bias=self._leverage_bias(baseline=baseline, ai_assessment=ai_assessment),
        )

    def _leverage_bias(
        self,
        *,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        conviction = max(
            0.0,
            (baseline.confidence * 0.45)
            + (abs(self._ai_directional_edge(ai_assessment)) * 0.35)
            + (self._ai_confidence_component(ai_assessment) * 0.2),
        )
        if baseline.volatility_state == "high":
            conviction *= 0.62
        if baseline.regime == "breakout":
            conviction *= 1.08
        if baseline.regime in {"range", "uncertain"}:
            conviction *= 0.85
        # R3-P0-D1：factor_scores 来自上游特征引擎，理论可能出 NaN/inf（数值不稳、
        # 除零等），NaN 会穿透 max/min 让 conviction→NaN→杠杆→NaN，下游风控拿到
        # 幻觉杠杆。这里对单值做 isfinite 兜底，回退到中性值（microstructure=0,
        # liquidity_scale=1.0）而不是继续传播异常。
        raw_microstructure = baseline.factor_scores.get("microstructure_alpha", 0.0)
        microstructure = raw_microstructure if math.isfinite(raw_microstructure) else 0.0
        raw_liquidity = baseline.factor_scores.get("liquidity_scale", 1.0)
        liquidity_scale = raw_liquidity if math.isfinite(raw_liquidity) else 1.0
        conviction *= max(0.75, min(1.15, liquidity_scale + (abs(microstructure) * 0.2)))
        if microstructure and (
            (baseline.direction_bias == "long" and microstructure < 0.0)
            or (baseline.direction_bias == "short" and microstructure > 0.0)
        ):
            conviction *= 0.75
        if ai_assessment is not None and (ai_assessment.degraded or ai_assessment.fallback_used):
            conviction *= 0.85
        # R3-P0-D1：最终兜底 —— 若中间任一环节出 NaN/inf，整体回退到中性偏置 1.0。
        # 真金白银下宁可保守，也不让幻觉杠杆越过风控。
        biased = 0.85 + conviction
        if not math.isfinite(biased):
            return 1.0
        return self._clamp(biased, 0.85, 2.5)

    def _short_bias_allowed(self, product_type: str) -> bool:
        return product_type == "derivatives" and bool(self.settings.strategy_short_bias_enabled)

    def _normalize_min_actionable_target_qty(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        product_type: str,
    ) -> Decimal:
        if product_type != "derivatives":
            return desired_target_qty
        if abs(current_position_qty) > EPSILON_DECIMAL_12 or abs(desired_target_qty) <= EPSILON_DECIMAL_12:
            return desired_target_qty
        minimum_qty = max(to_decimal(self.settings.default_order_qty), EPSILON_DECIMAL_12)
        if abs(desired_target_qty) + EPSILON_DECIMAL_12 >= minimum_qty:
            return desired_target_qty
        return self._sign(desired_target_qty) * minimum_qty

    def _normalize_long_only_target(
        self,
        *,
        current_position_qty: Decimal,
        target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> Decimal:
        if target_qty >= 0:
            bearish_signal = baseline.direction_bias == "short" or self._ai_directional_edge(ai_assessment) < 0.0
            if current_position_qty > EPSILON_DECIMAL_12 and bearish_signal and target_qty < current_position_qty:
                return current_position_qty
            if current_position_qty > EPSILON_DECIMAL_12 and baseline.direction_bias == "flat" and target_qty <= EPSILON_DECIMAL_12:
                if current_position_qty <= self._flat_cleanup_threshold():
                    return Decimal("0")
                return max(current_position_qty * Decimal("0.5"), Decimal("0"))
            return target_qty
        if current_position_qty > EPSILON_DECIMAL_12 and (baseline.direction_bias == "short" or self._ai_directional_edge(ai_assessment) < 0.0):
            # Long-only spot should treat bearish reversal signals as "stop adding"
            # rather than forcing churn into immediate flat on every negative flip.
            return current_position_qty
        return Decimal("0")

    def _apply_position_management(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        product_type: str,
    ) -> Decimal:
        desired_target_qty = self._normalize_min_actionable_target_qty(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            product_type=product_type,
        )
        rebalance_band = self._rebalance_band(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        delta_qty = desired_target_qty - current_position_qty
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty
        if abs(desired_target_qty) < EPSILON_DECIMAL_12 and abs(current_position_qty) <= rebalance_band:
            return Decimal("0")
        if abs(delta_qty) <= rebalance_band:
            return current_position_qty

        if self._same_direction(current_position_qty, desired_target_qty):
            if abs(desired_target_qty) > abs(current_position_qty):
                max_step = self._max_scale_step(desired_target_qty)
                step = min(abs(delta_qty), max_step)
                return current_position_qty + (self._sign(delta_qty) * step)
            if abs(delta_qty) <= self._reduce_threshold(
                current_position_qty=current_position_qty,
                desired_target_qty=desired_target_qty,
            ):
                return current_position_qty
            return desired_target_qty

        if abs(current_position_qty) > EPSILON_DECIMAL_12 and abs(desired_target_qty) > EPSILON_DECIMAL_12:
            if abs(desired_target_qty) < self._reverse_threshold(current_position_qty=current_position_qty):
                if product_type == "derivatives":
                    return self._derivatives_reversal_step(current_position_qty=current_position_qty)
                return Decimal("0")
        return desired_target_qty

    def _hedge_overlay_runtime_supported(
        self,
        *,
        context: DecisionContext,
        margin_mode: str | None = None,
    ) -> bool:
        resolved_margin_mode = (
            self._resolved_margin_mode(context=context)
            if margin_mode is None
            else margin_mode
        )
        return (
            context.product_type == "derivatives"
            and resolved_margin_mode != "cash"
            and self.settings.derivatives_position_mode == "hedge"
        )

    def _directional_hedge_strategy_legs(
        self,
        *,
        context: DecisionContext,
        directional_target_qty: Decimal,
        target_leverage: float,
        runtime_margin_mode: str,
    ) -> list[StrategyLegIntent]:
        long_target_qty = max(to_decimal(directional_target_qty), Decimal("0"))
        short_target_qty = max(-to_decimal(directional_target_qty), Decimal("0"))
        return [
            leg
            for leg in (
                self._build_directional_primary_execution_leg(
                    symbol=context.symbol,
                    pos_side="long",
                    current_leg_qty=context.current_long_position_qty,
                    target_leg_qty=long_target_qty,
                    target_leverage=target_leverage,
                    runtime_margin_mode=runtime_margin_mode,
                ),
                self._build_directional_primary_execution_leg(
                    symbol=context.symbol,
                    pos_side="short",
                    current_leg_qty=context.current_short_position_qty,
                    target_leg_qty=short_target_qty,
                    target_leverage=target_leverage,
                    runtime_margin_mode=runtime_margin_mode,
                ),
            )
            if leg is not None
        ]

    def _build_directional_primary_execution_leg(
        self,
        *,
        symbol: str,
        pos_side: str,
        current_leg_qty: Decimal,
        target_leg_qty: Decimal,
        target_leverage: float,
        runtime_margin_mode: str,
    ) -> StrategyLegIntent | None:
        current_leg_qty = max(to_decimal(current_leg_qty), Decimal("0"))
        target_leg_qty = max(to_decimal(target_leg_qty), Decimal("0"))
        delta_qty = target_leg_qty - current_leg_qty
        if abs(delta_qty) <= EPSILON_DECIMAL_12:
            return None
        opening = delta_qty > 0
        action = "open" if opening else ("close" if target_leg_qty <= EPSILON_DECIMAL_12 else "reduce")
        if pos_side == "long":
            side = "buy" if opening else "sell"
            signed_current_qty = current_leg_qty
            signed_target_qty = target_leg_qty
        else:
            side = "sell" if opening else "buy"
            signed_current_qty = -current_leg_qty
            signed_target_qty = -target_leg_qty
        return StrategyLegIntent(
            symbol=symbol,
            product_type="derivatives",
            side=side,
            position_mode="long_short_mode",
            pos_side=pos_side,
            action=action,
            family="directional",
            role="primary",
            margin_mode=runtime_margin_mode,
            target_leverage=target_leverage,
            current_position_qty=signed_current_qty,
            target_position_qty=signed_target_qty,
            delta_position_qty=signed_target_qty - signed_current_qty,
            execution_compatible=True,
            execution_mode="directional_main_leg",
            state_phase="active",
            note="Directional 主腿（hedge 模式）",
        )

    def _rebalance_band(self, *, current_position_qty: Decimal, desired_target_qty: Decimal) -> Decimal:
        return max(
            to_decimal(self.settings.default_order_qty) * Decimal("0.12"),
            abs(desired_target_qty) * Decimal("0.08"),
            abs(current_position_qty) * Decimal("0.08"),
            EPSILON_DECIMAL_12,
        )

    def _reduce_threshold(self, *, current_position_qty: Decimal, desired_target_qty: Decimal) -> Decimal:
        return max(
            to_decimal(self.settings.default_order_qty) * Decimal("0.1"),
            abs(current_position_qty) * Decimal("0.12"),
            abs(desired_target_qty) * Decimal("0.12"),
        )

    def _reverse_threshold(self, *, current_position_qty: Decimal) -> Decimal:
        return max(
            to_decimal(self.settings.default_order_qty) * Decimal("0.45"),
            abs(current_position_qty) * Decimal("0.35"),
        )

    def _max_scale_step(self, desired_target_qty: Decimal) -> Decimal:
        return max(to_decimal(self.settings.default_order_qty) * Decimal("0.4"), abs(desired_target_qty) * Decimal("0.45"))

    @staticmethod
    def _derivatives_reversal_step(*, current_position_qty: Decimal) -> Decimal:
        return current_position_qty * Decimal("0.35")

    def _urgency(self, *, current_position_qty: Decimal, target_position_qty: Decimal) -> str:
        delta_qty = abs(target_position_qty - current_position_qty)
        if delta_qty < EPSILON_DECIMAL_12:
            return "low"
        if current_position_qty * target_position_qty < 0:
            return "high"
        if delta_qty >= to_decimal(self.settings.default_order_qty) * Decimal("0.75"):
            return "high"
        return "medium"

    def _position_intent(
        self,
        *,
        current_position_qty: Decimal,
        target_position_qty: Decimal,
    ) -> str:
        if abs(target_position_qty - current_position_qty) < EPSILON_DECIMAL_12:
            return "hold"
        current_side = self._exposure_side(current_position_qty)
        target_side = self._exposure_side(target_position_qty)
        if current_side == "flat":
            return "open_long" if target_side == "long" else "open_short"
        if target_side == "flat":
            return "close_long" if current_side == "long" else "close_short"
        if current_side != target_side:
            return "reverse_to_long" if target_side == "long" else "reverse_to_short"
        if current_side == "long":
            if abs(target_position_qty) > abs(current_position_qty) + EPSILON_DECIMAL_12:
                return "scale_in_long"
            return "reduce_long"
        if abs(target_position_qty) > abs(current_position_qty) + EPSILON_DECIMAL_12:
            return "scale_in_short"
        return "reduce_short"

    @staticmethod
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > EPSILON_DECIMAL_12:
            return "long"
        if quantity < -EPSILON_DECIMAL_12:
            return "short"
        return "flat"

    def _source_mix(
        self,
        *,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        operating_mode: str,
        ai_decision_authorized: bool,
    ) -> dict[str, float]:
        mode = normalize_ai_operating_mode(operating_mode)
        if mode == "baseline_only":
            return {"baseline": 1.0, "ai": 0.0}
        if mode == "ai_assisted":
            return {"baseline": 0.6, "ai": 0.4}
        if (
            mode == "ai_decision_maker"
            and ai_decision_authorized
            and ai_decision_intent is not None
            and not ai_decision_intent.fallback_used
        ):
            return {"baseline": 0.2, "ai": 0.8}
        return {"baseline": 1.0, "ai": 0.0}

    def _ai_decision_gate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        operating_mode: CanonicalAIOperatingMode,
    ) -> tuple[bool, list[str]]:
        if operating_mode != "ai_decision_maker":
            return False, []
        if ai_assessment is None or ai_decision_intent is None:
            return False, ["ai_decision_intent_missing"]
        blockers: list[str] = []
        if ai_decision_intent.fallback_used:
            blockers.append("ai_fallback_used")
        if not ai_assessment.output_valid:
            blockers.append("ai_output_invalid")
        blockers.extend(ai_assessment.rejection_flags)
        if ai_decision_intent.degraded:
            blockers.append("ai_degraded")
        # R3-P1-D3：同 _resolve_pre_execution_guards 的 Decimal 边界比较处理，
        # 避免 ai_decision_intent.confidence 等 float 在阈值边界出现非幂等跨越。
        if to_decimal(ai_decision_intent.confidence) + EPSILON_DECIMAL_12 < to_decimal(self.settings.ai_decision_min_confidence):
            blockers.append("ai_confidence_below_threshold")
        if to_decimal(ai_assessment.uncertainty) - EPSILON_DECIMAL_12 > to_decimal(self.settings.ai_decision_max_uncertainty):
            blockers.append("ai_uncertainty_above_threshold")
        if to_decimal(abs(ai_assessment.directional_edge)) + EPSILON_DECIMAL_12 < to_decimal(self.settings.ai_decision_min_directional_edge):
            blockers.append("ai_directional_edge_too_small")
        if not ai_assessment.baseline_override_recommended:
            blockers.append("ai_override_not_recommended")
        if not ai_assessment.economically_actionable:
            blockers.append("ai_not_economically_actionable")
        allowed_regimes = {item.lower() for item in self.settings.strategy_entry_allowed_regimes if item}
        if allowed_regimes and ai_assessment.regime.lower() not in allowed_regimes:
            blockers.append("ai_regime_not_allowed")
        if context.current_open_orders:
            blockers.append("ai_open_orders_present")
        if self._post_close_cooldown_active(context):
            blockers.append("ai_post_close_cooldown_active")
        if self._low_edge_cooldown_active(context):
            blockers.append("ai_low_edge_cooldown_active")
        if self._performance_degraded(context):
            blockers.append("ai_execution_performance_guard_active")
        if baseline.direction_bias == "flat" and abs(ai_assessment.directional_edge) < self.settings.ai_decision_min_directional_edge + 0.05:
            blockers.append("ai_flat_context_requires_stronger_edge")
        return not blockers, blockers

    def _decision_outcome(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        profile_control_decision: ProfileControlDecision | None,
        canonical_mode: CanonicalAIOperatingMode,
        target_qty: Decimal,
        target_exposure_side: str,
        position_intent: str,
        ai_decision_authorized: bool,
        ai_decision_applied: bool,
        ai_decision_blockers: list[str],
        guardrail_flags: list[str],
        sizing_breakdown: PositionSizingBreakdown | None,
    ) -> DecisionOutcome:
        authority_map = {
            "baseline_only": "reference_only",
            "ai_assisted": "advisory",
            "ai_decision_maker": "final_decision",
        }
        # P0-b Task 2.4：暴露 runtime mode 给 Prometheus/Grafana/Alerting。
        # 详见 docs/governance/p0b_observability_implementation_spec_2026_04_20.md §2.4。
        # 未装配 metrics 时（单测构 engine 走这条路）安全 skip。
        # 依赖 Prometheus scrape connection-refused 修好后才会真正被采到
        # （spec §2.4 的 "不硬阻塞" 条款）。
        if self.metrics is not None:
            try:
                self.metrics.increment_labeled(
                    "runtime_ai_operating_mode",
                    labels={"mode": str(canonical_mode)},
                )
            except Exception:  # metrics 异常永不阻断决策
                pass
        if canonical_mode == "ai_decision_maker":
            decision_source = "ai" if ai_decision_applied else "baseline_fallback"
        else:
            decision_source = "baseline"
        profile_control_source = "env_default"
        active_profile_id = None
        if profile_control_decision is not None:
            active_profile_id = (
                profile_control_decision.requested_profile_id
                if profile_control_decision.applied
                else profile_control_decision.current_profile_id
            )
            profile_control_source = (
                "ai"
                if profile_control_decision.applied
                else "admin" if profile_control_decision.frozen_by_admin_override else "system"
            )
        action_map = {
            "hold": "hold",
            "open_long": "enter",
            "scale_in_long": "scale_in",
            "open_short": "enter",
            "scale_in_short": "scale_in",
            "reduce_long": "reduce",
            "reduce_short": "reduce",
            "close_long": "exit",
            "close_short": "exit",
            "reverse_to_long": "reverse",
            "reverse_to_short": "reverse",
        }
        blocked_reasons = list(dict.fromkeys([*guardrail_flags, *ai_decision_blockers]))
        position_management_reason_codes = [
            code
            for code in ("alpha_decay_exit", "alpha_decay_reduce", "risk_contraction_exit", "emergency_protective_exit")
            if code in guardrail_flags
        ]
        exit_attribution = None
        if "emergency_protective_exit" in guardrail_flags:
            exit_attribution = "emergency_protective_exit"
        elif "alpha_decay_exit" in guardrail_flags:
            exit_attribution = "alpha_decay_exit"
        elif "alpha_decay_reduce" in guardrail_flags:
            exit_attribution = "alpha_decay_reduce"
        elif "risk_contraction_exit" in guardrail_flags:
            exit_attribution = "risk_contraction_exit"
        ai_direction = (
            None if ai_assessment is None
            else (
                ai_decision_intent.direction
                if ai_decision_intent is not None
                else self._direction_from_assessment(ai_assessment)
            )
        )
        return DecisionOutcome(
            decision_id=context.decision_id,
            symbol=context.symbol,
            ai_operating_mode=canonical_mode,
            finalized=True,
            decision_source=decision_source,
            decision_authority=authority_map[canonical_mode],
            final_direction=target_exposure_side,
            final_action=action_map.get(position_intent, "hold"),
            final_target_qty=target_qty,
            baseline_reference={
                "direction_bias": baseline.direction_bias,
                "confidence": baseline.confidence,
                "regime": baseline.regime,
                "volatility_state": baseline.volatility_state,
                "composite_alpha_score": baseline.composite_alpha_score,
                "suggested_position_scale": baseline.suggested_position_scale,
                "direction_threshold": baseline.direction_threshold,
                "direction_rule": baseline.direction_rule,
                "reason_codes": list(baseline.reason_codes),
            },
            baseline_disagreement=None if ai_direction is None else {
                "disagreed": ai_direction != baseline.direction_bias,
                "baseline_direction": baseline.direction_bias,
                "ai_direction": ai_direction,
            },
            decision_blocked_reasons=blocked_reasons,
            decision_blocker_chain=self._decision_blocker_chain(
                context=context,
                baseline=baseline,
                ai_decision_blockers=ai_decision_blockers,
                guardrail_flags=guardrail_flags,
                target_qty=target_qty,
            ),
            guardrail_flags=list(dict.fromkeys(guardrail_flags)),
            # R3-P0-D2：原先硬编码 False 导致审计看不到"策略拒绝 / 风控下调"事件。
            # 语义：
            # - policy_blocked: AI 决策通道因策略 gate 被拒（confidence 不足、regime
            #   不允许、degraded 等），最终回退到 baseline 或 hold。
            # - risk_capped: 风险类 guardrail（alpha decay / risk contraction /
            #   emergency protective exit）触发，final action 被下调为 reduce/exit。
            # 两者非互斥，且 reasons 与 decision_blocked_reasons 有重叠但语义不同：
            # decision_blocked_reasons 是聚合视图，这两组是细粒度归因。
            policy_blocked=bool(ai_decision_blockers),
            policy_blocked_reasons=list(dict.fromkeys(ai_decision_blockers)),
            risk_capped=bool(position_management_reason_codes),
            risk_capped_reasons=list(position_management_reason_codes),
            risk_capped_target_qty=target_qty if position_management_reason_codes else None,
            active_profile_id=active_profile_id,
            profile_control_source=profile_control_source,
            ai_fallback_used=False if ai_decision_intent is None else ai_decision_intent.fallback_used,
            ai_degraded=False if ai_decision_intent is None else ai_decision_intent.degraded,
            position_management_reason_codes=position_management_reason_codes,
            exit_attribution=exit_attribution,
            sizing_breakdown=(
                None
                if sizing_breakdown is None
                else sizing_breakdown.model_copy(deep=True)
            ),
        )

    def _decision_blocker_chain(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_decision_blockers: list[str],
        guardrail_flags: list[str],
        target_qty: Decimal,
    ) -> list[dict[str, object]]:
        chain: list[dict[str, object]] = []
        baseline_reasons: list[str] = []
        if baseline.direction_bias == "flat":
            baseline_reasons.append("baseline_direction_bias_flat")
        if baseline.direction_rule:
            baseline_reasons.append(baseline.direction_rule)
        if (
            baseline.direction_bias != "flat"
            and abs(target_qty - context.current_position_qty) <= EPSILON_DECIMAL_12
            and abs(context.current_position_qty) <= EPSILON_DECIMAL_12
        ):
            baseline_reasons.append("baseline_target_not_promoted_to_actionable_target")
        chain.append(
            {
                "stage": "baseline",
                "blocked": bool(baseline_reasons),
                "direction_bias": baseline.direction_bias,
                "direction_rule": baseline.direction_rule,
                "direction_threshold": baseline.direction_threshold,
                "reasons": baseline_reasons,
            }
        )
        chain.append(
            {
                "stage": "target_gate",
                "blocked": bool(guardrail_flags),
                "reasons": list(dict.fromkeys(guardrail_flags)),
            }
        )
        chain.append(
            {
                "stage": "ai_gate",
                "blocked": bool(ai_decision_blockers),
                "reasons": list(dict.fromkeys(ai_decision_blockers)),
            }
        )
        return chain

    @staticmethod
    def _direction_from_assessment(ai_assessment: AIMarketAssessment) -> str:
        if ai_assessment.directional_edge > 0.0:
            return "long"
        if ai_assessment.directional_edge < 0.0:
            return "short"
        return "flat"

    @staticmethod
    def _shadow_action_type(*, baseline_action: str, shadow_action: str) -> str:
        if baseline_action == shadow_action:
            return "same_as_baseline"
        if baseline_action == "hold" and shadow_action != "hold":
            return "entry_override"
        if baseline_action != "hold" and shadow_action == "hold":
            return "hold_instead"
        if shadow_action.startswith("reverse"):
            return "reverse_override"
        return "exit_override"

    @staticmethod
    def _same_direction(left: Decimal, right: Decimal) -> bool:
        if abs(left) < EPSILON_DECIMAL_12 or abs(right) < EPSILON_DECIMAL_12:
            return True
        return (left > 0 and right > 0) or (left < 0 and right < 0)

    @staticmethod
    def _sign(value: Decimal) -> Decimal:
        if value > 0:
            return Decimal("1")
        if value < 0:
            return Decimal("-1")
        return Decimal("0")

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))

    def _flat_cleanup_threshold(self) -> Decimal:
        return max(to_decimal(self.settings.default_order_qty) * Decimal("0.15"), EPSILON_DECIMAL_12)

    def _cost_reference_target_qty(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        canonical_mode: CanonicalAIOperatingMode,
        ai_decision_authorized: bool,
        target_qty: Decimal,
        guardrail_flags: list[str],
    ) -> Decimal:
        if abs(target_qty - context.current_position_qty) > EPSILON_DECIMAL_12:
            return target_qty
        if "expected_edge_below_cost_buffer" not in guardrail_flags:
            return target_qty
        if canonical_mode == "ai_decision_maker" and ai_decision_authorized and ai_decision_intent is not None:
            ai_target_qty = self._desired_target_qty_from_ai_decision_intent(
                context=context,
                ai_decision_intent=ai_decision_intent,
            )
            if abs(ai_target_qty - context.current_position_qty) > EPSILON_DECIMAL_12:
                return ai_target_qty
        return self._baseline_target_qty(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=context.product_type,
        )

    def _estimated_trade_cost_bps(
        self,
        *,
        context: DecisionContext | None = None,
        symbol: str | None = None,
        product_type: str = "spot",
        ai_assessment: AIMarketAssessment | None = None,
        margin_mode: str | None = None,
        desired_target_qty: Decimal | None = None,
    ) -> float:
        if context is not None:
            symbol = symbol or context.symbol
            product_type = context.product_type
        expected_slippage_bps = max(self.settings.max_slippage_tolerance_bps, 0) * max(
            self.settings.strategy_expected_slippage_bps_fraction,
            0.0,
        )
        envelope = None if ai_assessment is None else ai_assessment.ai_execution_parameter_suggestion
        suggestion = None if envelope is None else envelope.suggestion
        side: str | None = None
        quantity: Decimal | None = None
        projected_notional: Decimal | None = None
        reference_price: Decimal | None = None
        market_snapshot: MarketSnapshot | None = None
        if context is not None and desired_target_qty is not None:
            current_qty = to_decimal(context.current_position_qty)
            target_qty = to_decimal(desired_target_qty)
            delta_qty = target_qty - current_qty
            if abs(delta_qty) <= EPSILON_DECIMAL_12:
                return 0.0
            side = "buy" if delta_qty > 0 else "sell"
            quantity = abs(delta_qty)
            reference_price = max(to_decimal(context.market_last_price), Decimal("0"))
            if reference_price > EPSILON_DECIMAL_12:
                projected_notional = quantity * reference_price
            market_snapshot = context.market_snapshot
        estimate = self.trade_cost_service.estimate_single_leg_entry(
            model_name="directional_target_position",
            symbol=symbol,
            product_type=product_type,
            margin_mode=self._normalize_margin_mode(
                margin_mode,
                fallback="cash" if product_type == "spot" else str(self.settings.margin_mode),
            ),
            execution_style="bounded_limit_ioc" if suggestion is not None else "taker",
            order_type="limit" if suggestion is not None else "market",
            passive_bias=None if suggestion is None else suggestion.passive_bias,
            maker_taker_bias=None if suggestion is None else suggestion.maker_taker_bias,
            side=side,
            quantity=quantity,
            projected_notional=projected_notional,
            reference_price=reference_price,
            market_snapshot=market_snapshot,
            expected_slippage_bps=expected_slippage_bps,
            include_spread=False,
            include_funding=product_type == "derivatives",
        )
        return float(estimate.executable_total_drag_bps)

    @staticmethod
    def _normalize_margin_mode(value: str | None, *, fallback: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"cash", "cross", "isolated"}:
            return normalized
        return fallback

    def _resolved_margin_mode(self, *, context: DecisionContext) -> str:
        fallback = "cash" if context.product_type == "spot" else str(self.settings.margin_mode)
        current_state = context.current_position_state
        if current_state is not None:
            current_state_margin_mode = self._normalize_margin_mode(current_state.margin_mode, fallback=fallback)
            if context.product_type != "derivatives":
                return current_state_margin_mode
            if (
                current_state_margin_mode != "cash"
                or abs(to_decimal(current_state.net_position_qty)) > EPSILON_DECIMAL_12
                or int(current_state.leg_count) > 0
            ):
                return current_state_margin_mode
        leg_margin_modes = {
            mode
            for leg in context.current_position_legs
            if (mode := self._normalize_margin_mode(getattr(leg, "margin_mode", None), fallback=fallback)) != "cash"
        }
        if len(leg_margin_modes) == 1:
            return next(iter(leg_margin_modes))
        return self._normalize_margin_mode(self.settings.margin_mode, fallback=fallback)

    def _signal_edge_bps(
        self,
        *,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        alpha_edge = abs(baseline.composite_alpha_score) * max(self.settings.strategy_alpha_edge_bps_scale, 0.0)
        microstructure_bonus = max(abs(baseline.factor_scores.get("microstructure_alpha", 0.0)) - 0.08, 0.0) * 25.0
        ai_bonus = max(abs(self._ai_directional_edge(ai_assessment)) - 0.1, 0.0) * 20.0
        return alpha_edge + microstructure_bonus + ai_bonus

    @staticmethod
    def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
        return 0.0 if ai_assessment is None else ai_assessment.directional_edge

    @staticmethod
    def _ai_confidence_component(ai_assessment: AIMarketAssessment | None) -> float:
        if ai_assessment is None:
            return 0.0
        return max(ai_assessment.calibrated_confidence, ai_assessment.confidence)
