from __future__ import annotations

import json
from decimal import Decimal

from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.decision import BaselineAssessment, DecisionContext
from aats.schemas.features import FeatureSnapshot
from aats.services.portfolio_service.decimals import to_decimal


class PromptBuilder:
    def build_brief(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        feature_snapshot: FeatureSnapshot | None,
        margin_mode: str,
        fee_bps: Decimal | float,
        funding_fee_bps: Decimal | float,
        max_slippage_tolerance_bps: Decimal | float,
        expected_slippage_proxy_bps: Decimal | float,
        min_net_edge_bps: Decimal | float,
        degraded: bool,
    ) -> AIDecisionBrief:
        analysis = feature_snapshot.analysis_context if feature_snapshot is not None else None
        liquidity = analysis.liquidity if analysis is not None else None
        primary_tf = analysis.timeframe_features.get(context.timeframe) if analysis is not None else None
        flags = {item.lower() for item in context.policy_flags}
        review_required = any("review" in item for item in flags)
        halted = "kill_switch_active" in flags
        reconciliation_halt_required = any("reconciliation" in item and "halt" in item for item in flags)
        market_snapshot_fresh = "market_data_stale" not in flags
        account_snapshot_fresh = "account_state_stale" not in flags
        safe_to_trade = not (review_required or halted or reconciliation_halt_required)
        return AIDecisionBrief(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            product_type=context.product_type,
            margin_mode=margin_mode,
            last_price=primary_tf.close_price if primary_tf is not None else None,
            mid_price=None,
            spread_bps=liquidity.spread_bps if liquidity is not None else None,
            regime_indicator=baseline.regime,
            regime_confidence=feature_snapshot.regime_confidence if feature_snapshot is not None else baseline.confidence,
            composite_alpha_score=baseline.composite_alpha_score,
            momentum_score=feature_snapshot.momentum_score if feature_snapshot is not None else 0.0,
            trend_strength=baseline.trend_strength,
            volatility_state=baseline.volatility_state,
            volatility_value=feature_snapshot.volatility_value if feature_snapshot is not None else 0.0,
            multi_timeframe_alignment=feature_snapshot.multi_timeframe_alignment if feature_snapshot is not None else None,
            liquidity_score=feature_snapshot.liquidity_score if feature_snapshot is not None else None,
            execution_quality_scale=liquidity.execution_quality_scale if liquidity is not None else None,
            top_of_book_imbalance=liquidity.top_of_book_imbalance if liquidity is not None else None,
            depth_imbalance=liquidity.depth_imbalance if liquidity is not None else None,
            trade_flow_imbalance=liquidity.trade_flow_imbalance if liquidity is not None else None,
            current_position_qty=context.current_position_qty,
            current_exposure_side=context.current_exposure_side,
            current_open_order_count=len(context.current_open_orders),
            has_pending_close=any("close" in item.lower() for item in context.current_open_orders),
            gross_exposure=context.risk_budget_state.get("gross_exposure"),
            margin_usage=context.risk_budget_state.get("margin_usage"),
            baseline_direction_bias=baseline.direction_bias,
            baseline_confidence=baseline.confidence,
            baseline_suggested_position_scale=baseline.suggested_position_scale,
            baseline_reason_codes=list(baseline.reason_codes),
            fee_bps=to_decimal(fee_bps),
            funding_fee_bps=to_decimal(funding_fee_bps),
            max_slippage_tolerance_bps=to_decimal(max_slippage_tolerance_bps),
            expected_slippage_proxy_bps=to_decimal(expected_slippage_proxy_bps),
            min_net_edge_bps=to_decimal(min_net_edge_bps),
            safe_to_trade=safe_to_trade,
            review_required=review_required,
            halted=halted,
            reconciliation_halt_required=reconciliation_halt_required,
            market_snapshot_fresh=market_snapshot_fresh,
            account_snapshot_fresh=account_snapshot_fresh,
            execution_condition="degraded" if degraded else "normal",
        )

    def build(
        self,
        *,
        brief: AIDecisionBrief,
        operating_mode: str,
        include_execution_suggestion: bool = False,
    ) -> str:
        response_contract = {
            "regime": "trend|range|breakout|uncertain",
            "directional_edge": "float between -1 and 1",
            "expected_volatility": "non-negative float",
            "confidence": "float between 0 and 1",
            "uncertainty": "float between 0 and 1",
            "expected_holding_horizon": brief.timeframe,
            "invalidation_conditions": ["string", "string"],
            "risk_tags": ["string"],
            "rationale_summary": "short string",
            "baseline_override_recommended": "boolean",
            "override_reason_codes": ["string"],
        }
        if include_execution_suggestion:
            response_contract["execution_parameter_suggestion"] = {
                "passive_bias": "optional float between 0 and 1",
                "maker_taker_bias": "optional float between -1 and 1",
                "max_cross_spread_bps": "optional non-negative float",
                "slice_count": "optional positive integer",
                "max_participation_rate": "optional float between 0 and 1",
                "cancel_replace_patience_ms": "optional non-negative integer",
            }
        payload = {
            "task": "ai_primary_market_assessment",
            "operating_mode": operating_mode,
            "instructions": {
                "goal": "Return a high-discipline market assessment for a crypto trading system.",
                "requirements": [
                    "Prefer neutral output when edge is weak or costs are not covered.",
                    "Do not recommend baseline override when risk or execution state is degraded.",
                    "If baseline_override_recommended is true, include override_reason_codes.",
                    "If directional_edge is strong, include at least two invalidation_conditions.",
                    "Return strict JSON only.",
                ],
            },
            "decision_brief": brief.model_dump(mode="json"),
            "response_contract": response_contract,
        }
        if include_execution_suggestion:
            payload["instructions"]["requirements"].insert(
                -1,
                "execution_parameter_suggestion must be null unless there is a strong reason to prefer a bounded execution posture.",
            )
            payload["instructions"]["requirements"].insert(
                -1,
                "If execution_parameter_suggestion is present, only use the whitelisted bounded fields from the response contract.",
            )
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
