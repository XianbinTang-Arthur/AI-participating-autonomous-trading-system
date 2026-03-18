from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.operator import AuthSource, OperatorActionRecord, OperatorRole
from aats.schemas.strategy_profiles import (
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileGuardrails,
    StrategyProfilePayload,
    StrategyProfileRecommendation,
    StrategyProfileRecommendationOutput,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
    apply_strategy_profile_payload,
    diff_strategy_profile_payload,
    strategy_profile_payload_from_settings,
)
from aats.services.ai_service.openai_provider import OpenAIProvider
from aats.services.ai_service.provider import AIProviderError, AIProviderTimeoutError
from aats.storage.base import EventStore, StrategyProfileRepository

if TYPE_CHECKING:
    from aats.bootstrap.config import ApplicationRuntime


def _copy_payload(payload: StrategyProfilePayload, **updates: Any) -> StrategyProfilePayload:
    raw = payload.model_dump(mode="python")
    raw.update(updates)
    return StrategyProfilePayload.model_validate(raw)


def _seed_revisions(*, settings: AATSSettings, payload: StrategyProfilePayload) -> list[StrategyProfileRevision]:
    common = {
        "product_type": settings.trading_product_type,
        "margin_mode": settings.margin_mode,
        "allowed_symbols": settings.allowed_symbols,
        "guardrails": StrategyProfileGuardrails(),
        "created_by": "system_seed",
        "created_reason": "initial_seed",
    }
    return [
        StrategyProfileRevision(
            profile_id="trend_normal",
            profile_label="Trend Normal",
            status="active",
            risk_level="normal",
            market_intent="trend",
            payload=payload,
            description="Baseline trend profile derived from current runtime settings.",
            expected_behavior=["preserve current trend thresholds", "serve as rollback baseline"],
            manual_approval_required=False,
            auto_switch_allowed=True,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="trend_strict",
            profile_label="Trend Strict",
            risk_level="normal",
            market_intent="trend",
            payload=_copy_payload(
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 60.0),
                max_decisions_per_minute=min(payload.max_decisions_per_minute, 3),
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 5.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.0005),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 8.0),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.2),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.68),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.28),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.74),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.34),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.82),
            ),
            description="Keep trend trading enabled while raising entry, scale-in, and reversal thresholds.",
            expected_behavior=["reduce weak-trend entries", "preserve trend-following capability"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="range_defensive",
            profile_label="Range Defensive",
            risk_level="conservative",
            market_intent="range",
            payload=_copy_payload(
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 90.0),
                max_decisions_per_minute=min(payload.max_decisions_per_minute, 2),
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 6.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.0008),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 10.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.24),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.7),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.3),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.76),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.38),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.84),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 180.0
                ),
            ),
            description="Use in range markets or when fee pressure is elevated.",
            expected_behavior=["reduce churn", "raise net-edge threshold"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="high_volatility_defensive",
            profile_label="High Vol Defensive",
            risk_level="conservative",
            market_intent="high_volatility",
            payload=_copy_payload(
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 120.0),
                max_decisions_per_minute=min(payload.max_decisions_per_minute, 2),
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 8.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.001),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 12.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.26),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.74),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.32),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.8),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.4),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.88),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 240.0
                ),
            ),
            description="Use when volatility expands materially and execution risk rises.",
            expected_behavior=["reduce false triggers in high vol", "avoid overtrading when execution degrades"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="execution_degraded_safe",
            profile_label="Execution Safe",
            risk_level="conservative",
            market_intent="execution_degraded",
            payload=_copy_payload(
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 180.0),
                max_decisions_per_minute=1,
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 8.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.001),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 14.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.28),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.8),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.34),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.84),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.42),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.9),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 300.0
                ),
            ),
            description="Use when exchange busy responses or execution jitter increase.",
            expected_behavior=["cut decision frequency sharply", "avoid repeated submit and reversal loops"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
    ]


def seed_strategy_profiles(*, settings: AATSSettings, repo: StrategyProfileRepository) -> None:
    payload = strategy_profile_payload_from_settings(settings)
    existing = repo.list_revisions(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
    )
    if not existing:
        for revision in _seed_revisions(settings=settings, payload=payload):
            repo.save_revision(revision)
    state = repo.activation_state(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
        allowed_symbols=settings.allowed_symbols,
    )
    if state.active_revision_id is None:
        active = repo.list_revisions(
            product_type=settings.trading_product_type,
            margin_mode=settings.margin_mode,
            profile_id="trend_normal",
        )[0]
        repo.save_revision(active.model_copy(update={"status": "active", "updated_at": utc_now()}))
        repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": active.revision_id,
                    "active_profile_id": active.profile_id,
                    "last_activation_result": "activation_succeeded",
                    "last_activation_at": utc_now(),
                    "last_switch_reason": "initial_seed",
                    "last_switch_actor": "system_seed",
                }
            )
        )


@dataclass
class StrategyProfileControlService:
    runtime: ApplicationRuntime

    def __post_init__(self) -> None:
        self.repo = self.runtime.strategy_profile_repo
        self.settings = self.runtime.settings
        self.event_store: EventStore = self.runtime.event_store
        self.provider = OpenAIProvider(settings=self.settings) if self.settings.ai_provider_configured else None
        self.prompt_version = "strategy_tuning_v1"

    def ensure_seed_profiles(self) -> None:
        seed_strategy_profiles(settings=self.settings, repo=self.repo)

    @property
    def runtime_state_scope(self):
        from aats.services.runtime_scope import runtime_state_scope

        return runtime_state_scope(self.settings)

    def snapshot(self) -> dict[str, Any]:
        self.ensure_seed_profiles()
        state = self._activation_state()
        active = self._revision(state.active_revision_id)
        pending = self._revision(state.pending_revision_id)
        latest_recommendation = self.repo.latest_recommendation(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
        )
        revisions = self.repo.list_revisions(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        return {
            "scope": self._scope(),
            "activation": state.model_dump(mode="json"),
            "active_revision": self._revision_view(active),
            "pending_revision": self._revision_view(pending),
            "latest_recommendation": latest_recommendation.model_dump(mode="json") if latest_recommendation else None,
            "revisions": [self._revision_view(item) for item in revisions],
            "activation_history": [
                item.model_dump(mode="json")
                for item in self.repo.list_activation_history(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                )[:20]
            ],
            "rejections": [
                item.model_dump(mode="json")
                for item in self.repo.list_rejections(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                )[:20]
            ],
        }

    async def evaluate_now(self) -> dict[str, Any]:
        self.ensure_seed_profiles()
        context = self._tuning_context()
        recommendation = await self._generate_recommendation(context=context)
        self.repo.save_recommendation(recommendation)
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_RECOMMENDATIONS,
                key=recommendation.recommended_profile_id,
                payload_model=recommendation,
                source_component="strategy_profile_service",
            )
        )
        return {
            "recommendation": recommendation.model_dump(mode="json"),
            "validation": self._recommendation_validation(recommendation),
        }

    def accept_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        activation_mode: str,
        reason: str,
    ) -> dict[str, Any]:
        self.ensure_seed_profiles()
        recommendation = self.repo.get_recommendation(recommendation_id)
        if recommendation is None:
            raise KeyError("strategy_profile_recommendation_not_found")
        if recommendation.expires_at <= utc_now():
            raise ValueError("strategy_profile_recommendation_expired")
        revision = self._revision_for_profile(recommendation.recommended_profile_id)
        if revision is None:
            raise ValueError("strategy_profile_revision_missing")
        state = self._activation_state()

        if activation_mode == "stage_only":
            state = state.model_copy(
                update={
                    "pending_revision_id": revision.revision_id,
                    "pending_profile_id": revision.profile_id,
                    "activation_mode": "staged",
                    "last_switch_reason": reason,
                    "last_switch_actor": actor_identity,
                }
            )
            self.repo.save_activation_state(state)
            updated = recommendation.model_copy(
                update={
                    "decision_status": "accepted",
                    "decision_reason_code": "staged_for_manual_activation",
                    "decision_reason_detail": reason,
                }
            )
            self.repo.save_recommendation(updated)
            return {
                "status": "accepted_and_staged",
                "recommendation": updated.model_dump(mode="json"),
                "activation": state.model_dump(mode="json"),
            }

        blockers = self._activation_blockers()
        if blockers:
            self._reject_recommendation_record(
                recommendation=recommendation,
                source="local_guard",
                reason_code=blockers[0],
                reason_detail=";".join(blockers),
                actor_identity=actor_identity,
                actor_role=actor_role,
            )
            raise ValueError(blockers[0])

        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=recommendation.recommendation_id,
            reason_code="operator_accept_recommendation",
            reason_detail=reason,
        )
        updated = recommendation.model_copy(
            update={
                "decision_status": "accepted",
                "decision_reason_code": "operator_accepted",
                "decision_reason_detail": reason,
            }
        )
        self.repo.save_recommendation(updated)
        return {
            "status": "accepted_and_activated",
            "recommendation": updated.model_dump(mode="json"),
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def reject_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        reason_code: str,
        reason_detail: str | None,
    ) -> dict[str, Any]:
        self.ensure_seed_profiles()
        recommendation = self.repo.get_recommendation(recommendation_id)
        if recommendation is None:
            raise KeyError("strategy_profile_recommendation_not_found")
        updated = recommendation.model_copy(
            update={
                "decision_status": "rejected",
                "decision_reason_code": reason_code,
                "decision_reason_detail": reason_detail,
            }
        )
        self.repo.save_recommendation(updated)
        rejection = self._reject_recommendation_record(
            recommendation=updated,
            source="operator",
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
        return {
            "status": "rejected",
            "recommendation": updated.model_dump(mode="json"),
            "rejection": rejection.model_dump(mode="json"),
        }

    def activate_pending(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        state = self._activation_state()
        if state.pending_revision_id is None:
            raise ValueError("strategy_profile_pending_revision_missing")
        blockers = self._activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self._revision(state.pending_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_pending_revision_missing")
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_activate_pending_profile",
            reason_detail=reason,
        )
        return {
            "status": "activated",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def rollback(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        state = self._activation_state()
        if state.previous_active_revision_id is None:
            raise ValueError("strategy_profile_no_previous_revision")
        blockers = self._activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self._revision(state.previous_active_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_previous_revision_missing")
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_manual_rollback",
            reason_detail=reason,
        )
        return {
            "status": "rolled_back",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def audit_payload(
        self,
        *,
        action: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> OperatorActionRecord:
        return OperatorActionRecord(
            action=action,  # type: ignore[arg-type]
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason="strategy_profile_control",
            status=status,
            details=details or {},
        )

    def _tuning_context(self) -> dict[str, Any]:
        baseline_event = self.event_store.latest(topics.BASELINE_ASSESSMENTS)
        feature_event = self.event_store.latest(topics.FEATURE_SNAPSHOTS)
        latest_portfolio = self.runtime.portfolio_repo.latest()
        fills = self.runtime.execution_repo.fills_for_scope(scope=self.runtime_state_scope, limit=20)
        activation = self._activation_state()
        gross_realized = sum(getattr(item, "realized_pnl", 0.0) for item in fills)
        fee_total = sum(item.fee_amount for item in fills)
        trade_count = len(fills)
        fee_ratio = fee_total / gross_realized if gross_realized > 0 else (1.0 if fee_total > 0 else 0.0)
        return {
            "scope": self._scope(),
            "baseline": baseline_event.payload if baseline_event is not None else None,
            "features": feature_event.payload if feature_event is not None else None,
            "portfolio": latest_portfolio.model_dump(mode="json") if latest_portfolio is not None else None,
            "execution_health": {
                "open_order_count": len(
                    self.runtime.execution_repo.order_states_for_scope(scope=self.runtime_state_scope, open_only=True)
                ),
                "recent_execution_error_count": len(
                    self.runtime.event_store.recent_by_topic(topics.EXECUTION_ERROR_SUMMARIES, limit=20)
                ),
            },
            "performance": {
                "trade_count": trade_count,
                "gross_realized_pnl": gross_realized,
                "fee_total": fee_total,
                "fee_to_gross_pnl_ratio": fee_ratio,
            },
            "current_profile_id": activation.active_profile_id,
            "candidate_profiles": [
                {
                    "profile_id": item.profile_id,
                    "profile_label": item.profile_label,
                    "risk_level": item.risk_level,
                    "market_intent": item.market_intent,
                }
                for item in self.repo.list_revisions(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                )
            ],
        }

    async def _generate_recommendation(self, *, context: dict[str, Any]) -> StrategyProfileRecommendation:
        active_state = self._activation_state()
        if self.provider is not None:
            try:
                response = await self.provider.generate_assessment(
                    prompt=self._prompt(context),
                    response_schema=StrategyProfileRecommendationOutput.model_json_schema(),
                )
                output = StrategyProfileRecommendationOutput.model_validate(response.payload)
                return StrategyProfileRecommendation(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                    allowed_symbols=self.settings.allowed_symbols,
                    active_profile_id=active_state.active_profile_id,
                    recommended_profile_id=output.recommended_profile_id,
                    fallback_profile_id=output.fallback_profile_id,
                    confidence=output.confidence,
                    market_regime_assessment=output.market_regime_assessment,
                    reason_codes=output.reason_codes,
                    human_summary=output.human_summary,
                    risk_notes=output.risk_notes,
                    valid_for_minutes=output.valid_for_minutes,
                    generated_by=response.provider_name,
                    model_name=self.settings.ai_model_name,
                    prompt_version=self.prompt_version,
                    input_digest=self._input_digest(context),
                    input_snapshot=context,
                    expires_at=utc_now() + timedelta(minutes=output.valid_for_minutes),
                )
            except (AIProviderError, AIProviderTimeoutError, ValueError):
                pass
        return self._fallback_recommendation(context=context, active_profile_id=active_state.active_profile_id)

    def _fallback_recommendation(self, *, context: dict[str, Any], active_profile_id: str | None) -> StrategyProfileRecommendation:
        baseline = context.get("baseline") or {}
        features = context.get("features") or {}
        regime = baseline.get("regime") or features.get("regime_indicator") or "uncertain"
        volatility_state = baseline.get("volatility_state") or features.get("volatility_state") or "unknown"
        composite_alpha = float(baseline.get("composite_alpha_score", features.get("composite_alpha_score", 0.0)) or 0.0)
        fee_ratio = float(context.get("performance", {}).get("fee_to_gross_pnl_ratio", 0.0) or 0.0)
        execution_errors = int(context.get("execution_health", {}).get("recent_execution_error_count", 0) or 0)

        if execution_errors >= 3:
            recommended = "execution_degraded_safe"
            summary = "Execution errors are elevated; switch to the safety profile to reduce decision frequency."
            reasons = ["execution_errors_elevated"]
        elif volatility_state == "high":
            recommended = "high_volatility_defensive"
            summary = "Volatility is elevated; move to the high-volatility defensive profile."
            reasons = ["high_volatility_detected"]
        elif regime in {"range", "uncertain"} or fee_ratio >= 0.5 or abs(composite_alpha) < 0.12:
            recommended = "range_defensive"
            summary = "Range behavior or fee compression is dominant; reduce churn with the range defensive profile."
            reasons = ["range_regime_detected", "fee_churn_elevated"]
        elif abs(composite_alpha) >= 0.22:
            recommended = "trend_normal"
            summary = "Trend signal remains healthy; keep the standard trend profile."
            reasons = ["trend_signal_supported"]
        else:
            recommended = "trend_strict"
            summary = "Trend exists but edge is moderate; prefer the stricter trend profile."
            reasons = ["trend_signal_moderate"]

        return StrategyProfileRecommendation(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            active_profile_id=active_profile_id,
            recommended_profile_id=recommended,
            fallback_profile_id="trend_normal",
            confidence=0.72,
            market_regime_assessment={
                "regime": str(regime),
                "volatility_state": str(volatility_state),
                "execution_condition": "degraded" if execution_errors >= 3 else "normal",
            },
            reason_codes=reasons,
            human_summary=summary,
            risk_notes=["fallback_rule_based_recommendation"],
            valid_for_minutes=120,
            generated_by="rule_fallback",
            model_name="rule_fallback",
            prompt_version=self.prompt_version,
            input_digest=self._input_digest(context),
            input_snapshot=context,
            expires_at=utc_now() + timedelta(minutes=120),
        )

    def _prompt(self, context: dict[str, Any]) -> str:
        return (
            "You are a strategy tuning advisor for a cryptocurrency trading system. "
            "Choose exactly one approved profile from candidate_profiles. "
            "Optimize for financial safety, lower fee churn, lower low-edge trading, and execution reliability. "
            "Do not invent new profiles. Return only JSON. "
            f"prompt_version={self.prompt_version} context={json.dumps(context, ensure_ascii=False, sort_keys=True)}"
        )

    @staticmethod
    def _input_digest(context: dict[str, Any]) -> str:
        serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
        return str(abs(hash(serialized)))

    def _activation_state(self) -> StrategyProfileActivationState:
        return self.repo.activation_state(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
        )

    def _scope(self) -> dict[str, Any]:
        return {
            "product_type": self.settings.trading_product_type,
            "margin_mode": self.settings.margin_mode,
            "allowed_symbols": list(self.settings.allowed_symbols),
        }

    def _revision(self, revision_id: str | None) -> StrategyProfileRevision | None:
        return self.repo.get_revision(revision_id) if revision_id is not None else None

    def _revision_for_profile(self, profile_id: str) -> StrategyProfileRevision | None:
        rows = self.repo.list_revisions(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            profile_id=profile_id,
        )
        return rows[0] if rows else None

    def _revision_view(self, revision: StrategyProfileRevision | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        return {
            **revision.model_dump(mode="json"),
            "summary": diff_strategy_profile_payload(
                strategy_profile_payload_from_settings(self.settings),
                revision.payload,
            ),
        }

    def _activation_blockers(self) -> list[str]:
        open_orders = self.runtime.execution_repo.order_states_for_scope(
            scope=self.runtime_state_scope,
            open_only=True,
        )
        return ["strategy_profile_open_orders_present"] if open_orders else []

    def _recommendation_validation(self, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        active = self._activation_state()
        revision = self._revision_for_profile(recommendation.recommended_profile_id)
        blocked_reasons: list[str] = []
        if revision is None:
            blocked_reasons.append("strategy_profile_revision_missing")
        if recommendation.expires_at <= utc_now():
            blocked_reasons.append("strategy_profile_recommendation_expired")
        if recommendation.recommended_profile_id == active.active_profile_id:
            blocked_reasons.append("strategy_profile_already_active")
        if active.cooldown_until is not None and active.cooldown_until > utc_now():
            blocked_reasons.append("strategy_profile_switch_cooldown_active")
        blocked_reasons.extend(self._activation_blockers())
        return {
            "auto_apply_allowed": False,
            "blocked_reasons": blocked_reasons,
            "requires_manual_approval": True,
        }

    def _activate_revision(
        self,
        *,
        target: StrategyProfileRevision,
        state: StrategyProfileActivationState,
        trigger_type: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        recommendation_id: str | None,
        reason_code: str,
        reason_detail: str,
    ) -> StrategyProfileActivationRecord:
        previous = self._revision(state.active_revision_id)
        previous_payload = previous.payload if previous is not None else strategy_profile_payload_from_settings(self.settings)
        if previous is not None and previous.revision_id != target.revision_id:
            self.repo.save_revision(previous.model_copy(update={"status": "superseded", "updated_at": utc_now()}))
        target = target.model_copy(update={"status": "active", "updated_at": utc_now()})
        self.repo.save_revision(target)
        apply_strategy_profile_payload(self.settings, target.payload)
        next_state = state.model_copy(
            update={
                "previous_active_revision_id": state.active_revision_id,
                "active_revision_id": target.revision_id,
                "active_profile_id": target.profile_id,
                "pending_revision_id": None,
                "pending_profile_id": None,
                "activation_mode": "manual" if trigger_type == "manual" else "rollback",
                "last_activation_result": "rollback_succeeded" if trigger_type == "rollback" else "activation_succeeded",
                "last_activation_at": utc_now(),
                "last_activation_error": None,
                "last_switch_reason": reason_code,
                "last_switch_actor": actor_identity,
                "cooldown_until": utc_now() + timedelta(minutes=120),
            }
        )
        self.repo.save_activation_state(next_state)
        record = StrategyProfileActivationRecord(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            from_revision_id=previous.revision_id if previous is not None else None,
            to_revision_id=target.revision_id,
            from_profile_id=previous.profile_id if previous is not None else None,
            to_profile_id=target.profile_id,
            trigger_type=trigger_type,  # type: ignore[arg-type]
            actor_identity=actor_identity,
            actor_role=actor_role,
            auth_source=auth_source,
            recommendation_id=recommendation_id,
            result="rolled_back" if trigger_type == "rollback" else "succeeded",
            reason_code=reason_code,
            reason_detail=reason_detail,
            hot_safe=True,
            restart_required=False,
            diff=diff_strategy_profile_payload(previous_payload, target.payload),
        )
        self.repo.save_activation_record(record)
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_ACTIVATIONS,
                key=target.profile_id,
                payload_model=record,
                source_component="strategy_profile_service",
            )
        )
        return record

    def _reject_recommendation_record(
        self,
        *,
        recommendation: StrategyProfileRecommendation,
        source: str,
        reason_code: str,
        reason_detail: str | None,
        actor_identity: str | None,
        actor_role: OperatorRole,
    ) -> StrategyProfileRejectionRecord:
        record = StrategyProfileRejectionRecord(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            recommendation_id=recommendation.recommendation_id,
            recommended_profile_id=recommendation.recommended_profile_id,
            rejection_source=source,
            rejection_reason_code=reason_code,
            rejection_reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
        self.repo.save_rejection(record)
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_REJECTIONS,
                key=recommendation.recommended_profile_id,
                payload_model=record,
                source_component="strategy_profile_service",
            )
        )
        return record
