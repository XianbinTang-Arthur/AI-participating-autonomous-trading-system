from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.strategy_profiles import (
    StrategyProfileComparisonReport,
    StrategyProfileComparisonRow,
    StrategyProfileEvaluationContextSnapshot,
    StrategyProfileEvaluationRecord,
    StrategyProfileRevision,
    StrategyProfileActivationState,
    strategy_profile_axes_from_payload,
    summarize_strategy_profile_payload,
)
from aats.schemas.strategy_profile_reports import (
    StrategyProfileOptimizationCandidate,
    StrategyProfileOptimizationReport,
)

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


def build_comparison_report(
    service: "StrategyProfileControlService",
    *,
    revisions: list[StrategyProfileRevision],
    state: StrategyProfileActivationState,
    evaluations: list[StrategyProfileEvaluationRecord] | None = None,
    evaluation_limit: int,
) -> StrategyProfileComparisonReport:
    if evaluations is None:
        evaluations = service.repo.list_evaluations(
            product_type=service.settings.trading_product_type,
            margin_mode=service.settings.margin_mode,
        )[:evaluation_limit]
    by_profile: dict[str, list[StrategyProfileEvaluationRecord]] = {}
    for item in evaluations:
        by_profile.setdefault(item.profile_id, []).append(item)

    rows: list[StrategyProfileComparisonRow] = []
    for revision in revisions:
        profile_evaluations = by_profile.get(revision.profile_id, [])
        trade_count = sum(item.trade_count for item in profile_evaluations)
        evaluation_count = len(profile_evaluations)
        avg_net_realized_pnl = (
            sum(item.net_realized_pnl for item in profile_evaluations) / evaluation_count if evaluation_count else 0.0
        )
        avg_fee_ratio = (
            sum(item.fee_to_gross_pnl_ratio for item in profile_evaluations) / evaluation_count
            if evaluation_count
            else 0.0
        )
        avg_churn_ratio = (
            sum(item.small_pnl_churn_ratio for item in profile_evaluations) / evaluation_count
            if evaluation_count
            else 0.0
        )
        avg_win_rate = (
            sum(item.win_rate for item in profile_evaluations) / evaluation_count if evaluation_count else 0.0
        )
        latest_status = profile_evaluations[0].status if profile_evaluations else None
        score_breakdown = {
            "net_realized_pnl": round(avg_net_realized_pnl, 6),
            "win_rate": round(avg_win_rate * 100.0, 6),
            "fee_penalty": round(
                avg_fee_ratio * float(service.settings.strategy_profile_score_fee_penalty_weight), 6
            ),
            "churn_penalty": round(
                avg_churn_ratio * float(service.settings.strategy_profile_score_churn_penalty_weight),
                6,
            ),
            "status_penalty": (
                float(service.settings.strategy_profile_score_degraded_status_penalty)
                if latest_status in {"degraded", "rollback_recommended", "rollback_executed"}
                else 0.0
            ),
        }
        score = round(sum(score_breakdown.values()), 6)
        rows.append(
            StrategyProfileComparisonRow(
                profile_id=revision.profile_id,
                profile_label=revision.profile_label,
                risk_level=revision.risk_level,
                market_intent=revision.market_intent,
                axes=strategy_profile_axes_from_payload(
                    revision.payload,
                    product_type=revision.product_type,
                ),
                evaluation_count=evaluation_count,
                total_trade_count=trade_count,
                avg_net_realized_pnl=avg_net_realized_pnl,
                avg_fee_to_gross_pnl_ratio=avg_fee_ratio,
                avg_small_pnl_churn_ratio=avg_churn_ratio,
                avg_win_rate=avg_win_rate,
                latest_status=latest_status,
                active=revision.profile_id == state.active_profile_id,
                pending=revision.profile_id == state.pending_profile_id,
                score=score,
                score_breakdown=score_breakdown,
                expected_behavior=list(revision.expected_behavior),
                summary=summarize_strategy_profile_payload(
                    revision.payload,
                    product_type=revision.product_type,
                ),
            )
        )
    rows.sort(key=lambda item: (-item.score, item.profile_id))
    return StrategyProfileComparisonReport(
        scope=service._scope(),
        shadow_summary=shadow_summary_for_profiles(service),
        active_profile_id=state.active_profile_id,
        rows=rows,
    )


def build_optimization_report(
    service: "StrategyProfileControlService",
    *,
    state: StrategyProfileActivationState,
    comparison_report: StrategyProfileComparisonReport,
    evaluations: list[StrategyProfileEvaluationRecord],
    context_snapshot: StrategyProfileEvaluationContextSnapshot | None = None,
) -> StrategyProfileOptimizationReport:
    if context_snapshot is None:
        context_snapshot = service._tuning_context()
    ai_performance_summary = shadow_summary_for_profiles(service)
    context = service._context_payload(context_snapshot)
    signals = service._resolved_context_signals(context)
    replay_summary = recent_replay_summary(
        service,
        symbol=service.settings.default_symbol,
        regime=signals["regime"],
        active_profile_id=state.active_profile_id,
    )
    replay_pipeline = build_offline_replay_pipeline(
        service,
        comparison_rows=comparison_report.rows,
        symbol=service.settings.default_symbol,
        regime=signals["regime"],
        active_profile_id=state.active_profile_id,
    )
    replay_summary = replay_pipeline.get("primary_summary") or replay_summary
    control_summary = service._profile_control_summary(
        context=context,
        replay_summary=replay_summary,
        active_profile_id=state.active_profile_id,
    )
    safety_profile_required = bool(control_summary.get("safety_profile_required"))
    evidence = control_summary.get("evidence") or {}
    cold_start_active = bool(evidence.get("cold_start_active"))
    previous_report = service._latest_optimization_report()
    evaluation_refs_by_profile: dict[str, list[str]] = {}
    for evaluation in evaluations:
        evaluation_refs_by_profile.setdefault(evaluation.profile_id, []).append(evaluation.evaluation_id)

    candidates: list[StrategyProfileOptimizationCandidate] = []
    for row in comparison_report.rows:
        shadow_adjustment = service._shadow_adjustment_for_profile(
            row=row, ai_performance_summary=ai_performance_summary
        )
        replay_scorecard = (replay_pipeline.get("candidate_scores") or {}).get(row.profile_id) or {}
        replay_adjustment = float(replay_scorecard.get("aggregate_adjustment") or 0.0)
        stability_adjustment = service._stability_adjustment_for_profile(row=row, replay_summary=replay_summary)
        composite_score = round(row.score + shadow_adjustment + replay_adjustment + stability_adjustment, 6)
        selection_blocked_reasons: list[str] = []
        if service._is_safety_profile_id(row.profile_id) and not safety_profile_required:
            selection_blocked_reasons.append("strategy_profile_safety_profile_requires_explicit_trigger")
        if cold_start_active and row.profile_id != state.active_profile_id and not (
            service._is_safety_profile_id(row.profile_id) and safety_profile_required
        ):
            selection_blocked_reasons.append("strategy_profile_cold_start_lock_active")
        reasons = service._optimization_reasons(
            row=row,
            shadow_adjustment=shadow_adjustment,
            replay_adjustment=replay_adjustment,
            stability_adjustment=stability_adjustment,
            ai_performance_summary=ai_performance_summary,
            replay_summary=replay_summary,
        )
        candidates.append(
            StrategyProfileOptimizationCandidate(
                profile_id=row.profile_id,
                profile_label=row.profile_label,
                risk_level=row.risk_level,
                market_intent=row.market_intent,
                base_score=row.score,
                shadow_adjustment=shadow_adjustment,
                replay_adjustment=replay_adjustment,
                stability_adjustment=stability_adjustment,
                composite_score=composite_score,
                recommendation_strength=round(max(composite_score, 0.0), 6),
                offline_replay_score=replay_adjustment,
                offline_replay_breakdown=replay_scorecard,
                selection_eligible=not selection_blocked_reasons,
                selection_blocked_reasons=selection_blocked_reasons,
                reasons=reasons,
                evaluation_refs=evaluation_refs_by_profile.get(row.profile_id, []),
                metrics={
                    "evaluation_count": row.evaluation_count,
                    "avg_net_realized_pnl": row.avg_net_realized_pnl,
                    "avg_fee_to_gross_pnl_ratio": row.avg_fee_to_gross_pnl_ratio,
                    "avg_small_pnl_churn_ratio": row.avg_small_pnl_churn_ratio,
                    "avg_win_rate": row.avg_win_rate,
                    "latest_status": row.latest_status,
                    "shadow_summary": ai_performance_summary,
                    "selection_eligible": not selection_blocked_reasons,
                    "selection_blocked_reasons": selection_blocked_reasons,
                },
            )
        )
    candidates.sort(key=lambda item: (-item.composite_score, item.profile_id))
    eligible_candidates = [item for item in candidates if item.selection_eligible]
    recommended_candidate = eligible_candidates[0] if eligible_candidates else next(
        (item for item in candidates if item.profile_id == state.active_profile_id),
        None,
    )
    recommended = recommended_candidate.profile_id if recommended_candidate is not None else (candidates[0].profile_id if candidates else None)
    active_candidate = next((item for item in candidates if item.profile_id == state.active_profile_id), None)
    winner_candidate = recommended_candidate if recommended_candidate is not None else (candidates[0] if candidates else None)
    score_delta_vs_active = round(
        float(winner_candidate.composite_score if winner_candidate is not None else 0.0)
        - float(active_candidate.composite_score if active_candidate is not None else 0.0),
        6,
    )
    notes = [
        "offline_optimization_uses_historical_profile_evaluations",
        "shadow_guard_adjustment_derived_from_latest_ai_performance_reports",
        "replay_adjustment_derived_from_recent_replay_validations",
        "replay_cross_bucket_scoring_enabled_for_symbol_regime_profile",
    ]
    winner_selection_policy = service._winner_selection_policy(
        candidates=candidates,
        ai_performance_summary=ai_performance_summary,
    )
    version_experiments = service._profile_version_experiments(
        revisions=service.repo.list_revisions(
            product_type=service.settings.trading_product_type,
            margin_mode=service.settings.margin_mode,
        ),
        replay_pipeline=replay_pipeline,
    )
    return StrategyProfileOptimizationReport(
        version=1 if previous_report is None else previous_report.version + 1,
        parent_report_id=None if previous_report is None else previous_report.report_id,
        scope=service._scope(),
        product_type=service.settings.trading_product_type,
        margin_mode=service.settings.margin_mode,
        allowed_symbols=tuple(service.settings.allowed_symbols),
        context_snapshot_id=context_snapshot.snapshot_id,
        active_profile_id=state.active_profile_id,
        recommended_profile_id=recommended,
        recommended_by="winner_engine",
        score_delta_vs_active=score_delta_vs_active,
        replay_summary=replay_summary,
        offline_replay_pipeline=replay_pipeline,
        ai_performance_summary=ai_performance_summary,
        control_summary=control_summary,
        winner_selection_policy=winner_selection_policy,
        version_experiments=version_experiments,
        candidates=candidates,
        notes=notes,
    )


def shadow_summary_for_profiles(service: "StrategyProfileControlService") -> dict[str, Any]:
    latest_report = service.event_store.latest_by_topic_scoped(
        topics.AI_PERFORMANCE_REPORTS,
        scope=service.runtime_state_scope,
    )
    if latest_report is not None and isinstance(latest_report.payload, dict):
        payload = latest_report.payload
        summary = payload.get("summary") or {}
        return {
            "window_count": payload.get("window_count", 0),
            "outperformed_count": summary.get("outperformed_count", 0),
            "underperformed_count": summary.get("underperformed_count", 0),
            "latest_net_pnl_delta": summary.get("latest_net_pnl_delta", 0.0),
            "latest_fee_ratio_delta": summary.get("latest_fee_ratio_delta", 0.0),
            "latest_churn_ratio_delta": summary.get("latest_churn_ratio_delta", 0.0),
            "review_required": payload.get("review_required", False),
            "latest_status": payload.get("latest_status", "insufficient_data"),
        }
    rows = list(
        reversed(
            service.event_store.by_topic_scoped(
                topics.AI_SHADOW_EVALUATIONS,
                scope=service.runtime_state_scope,
                limit=10,
            )
        )
    )[:10]
    payloads = [item.payload for item in rows if isinstance(item.payload, dict)]
    if not payloads:
        return {
            "window_count": 0,
            "outperformed_count": 0,
            "underperformed_count": 0,
            "latest_net_pnl_delta": 0.0,
            "latest_fee_ratio_delta": 0.0,
            "latest_churn_ratio_delta": 0.0,
        }
    latest = payloads[0]
    return {
        "window_count": len(payloads),
        "outperformed_count": sum(1 for item in payloads if item.get("shadow_outperformed") is True),
        "underperformed_count": sum(1 for item in payloads if item.get("shadow_outperformed") is False),
        "latest_net_pnl_delta": round(
            float(Decimal(str(latest.get("shadow_net_pnl") or "0")) - Decimal(str(latest.get("baseline_net_pnl") or "0"))),
            6,
        ),
        "latest_fee_ratio_delta": round(
            float(latest.get("shadow_fee_ratio") or 0.0) - float(latest.get("baseline_fee_ratio") or 0.0),
            6,
        ),
        "latest_churn_ratio_delta": round(
            float(latest.get("shadow_churn_ratio") or 0.0) - float(latest.get("baseline_churn_ratio") or 0.0),
            6,
        ),
    }


def recent_replay_summary(
    service: "StrategyProfileControlService",
    *,
    symbol: str | None = None,
    regime: str | None = None,
    active_profile_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    rows = list(reversed(service.event_store.by_topic(topics.REPLAY_VALIDATIONS)))
    scoped_rows: list[dict[str, Any]] = []
    for event in rows:
        payload = event.payload if isinstance(event.payload, dict) else None
        if payload is None:
            continue
        product_type = payload.get("product_type")
        margin_mode = payload.get("margin_mode")
        allowed_symbols = tuple(payload.get("allowed_symbols") or ())
        if product_type and product_type != service.settings.trading_product_type:
            continue
        if margin_mode and margin_mode != service.settings.margin_mode:
            continue
        if allowed_symbols and allowed_symbols != tuple(service.settings.allowed_symbols):
            continue
        scoped_rows.append(payload)
        if len(scoped_rows) >= limit:
            break
    matched_symbol_rows = [item for item in scoped_rows if symbol and item.get("symbol") == symbol]
    matched_regime_rows = [item for item in scoped_rows if regime and item.get("regime") == regime]
    matched_profile_rows = [
        item for item in scoped_rows if active_profile_id and item.get("active_profile_id") == active_profile_id
    ]
    matched_cross_rows = [
        item
        for item in scoped_rows
        if (symbol is None or item.get("symbol") == symbol)
        and (regime is None or item.get("regime") == regime)
        and (active_profile_id is None or item.get("active_profile_id") == active_profile_id)
    ]
    if not scoped_rows:
        return {
            "validation_count": 0,
            "healthy_rate": 0.0,
            "avg_divergence_count": None,
            "avg_divergence_density": None,
            "avg_chain_health_score": None,
            "avg_portfolio_issue_count": None,
            "avg_decision_chain_issue_count": None,
            "avg_execution_chain_issue_count": None,
            "avg_audit_issue_count": None,
            "avg_baseline_switch_issue_count": None,
            "latest_validation": None,
            "target_symbol": symbol,
            "target_regime": regime,
            "target_profile_id": active_profile_id,
            "bucket_scores": {},
            "cross_bucket_scores": [],
            "current_cross_bucket": {
                "count": 0,
                "healthy_rate": 0.0,
                "avg_chain_health_score": None,
                "avg_divergence_density": None,
            },
        }

    def _bucket(rows_subset: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows_subset:
            return {"count": 0, "healthy_rate": 0.0, "avg_chain_health_score": None, "avg_divergence_density": None}
        return {
            "count": len(rows_subset),
            "healthy_rate": round(sum(1 for item in rows_subset if item.get("healthy")) / len(rows_subset), 6),
            "avg_chain_health_score": round(
                sum(float(item.get("chain_health_score") or 0.0) for item in rows_subset) / len(rows_subset),
                6,
            ),
            "avg_divergence_density": round(
                sum(float(item.get("divergence_density") or 0.0) for item in rows_subset) / len(rows_subset),
                6,
            ),
        }

    cross_bucket_map: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in scoped_rows:
        cross_key = (
            str(item.get("symbol") or "unknown"),
            str(item.get("regime") or "unknown"),
            str(item.get("active_profile_id") or "unknown"),
        )
        cross_bucket_map.setdefault(cross_key, []).append(item)
    cross_bucket_scores = [
        {
            "symbol": cross_key[0],
            "regime": cross_key[1],
            "profile_id": cross_key[2],
            **_bucket(bucket_rows),
        }
        for cross_key, bucket_rows in cross_bucket_map.items()
    ]
    cross_bucket_scores.sort(
        key=lambda item: (
            -int(item.get("count") or 0),
            -float(item.get("avg_chain_health_score") or 0.0),
            str(item.get("symbol") or ""),
            str(item.get("regime") or ""),
            str(item.get("profile_id") or ""),
        )
    )
    return {
        "validation_count": len(scoped_rows),
        "healthy_rate": round(sum(1 for item in scoped_rows if item.get("healthy")) / len(scoped_rows), 6),
        "avg_divergence_count": round(
            sum(float(item.get("divergence_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_divergence_density": round(
            sum(float(item.get("divergence_density") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_chain_health_score": round(
            sum(float(item.get("chain_health_score") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_portfolio_issue_count": round(
            sum(float(item.get("portfolio_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_decision_chain_issue_count": round(
            sum(float(item.get("decision_chain_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_execution_chain_issue_count": round(
            sum(float(item.get("execution_chain_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_audit_issue_count": round(
            sum(float(item.get("audit_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "avg_baseline_switch_issue_count": round(
            sum(float(item.get("baseline_switch_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
            6,
        ),
        "latest_validation": scoped_rows[0],
        "target_symbol": symbol,
        "target_regime": regime,
        "target_profile_id": active_profile_id,
        "bucket_scores": {
            "symbol": _bucket(matched_symbol_rows),
            "regime": _bucket(matched_regime_rows),
            "profile": _bucket(matched_profile_rows),
        },
        "cross_bucket_scores": cross_bucket_scores[:12],
        "current_cross_bucket": _bucket(matched_cross_rows),
    }


def build_offline_replay_pipeline(
    service: "StrategyProfileControlService",
    *,
    comparison_rows: list[StrategyProfileComparisonRow],
    symbol: str | None,
    regime: str | None,
    active_profile_id: str | None,
) -> dict[str, Any]:
    windows = sorted({int(item) for item in service.settings.strategy_profile_offline_replay_windows if int(item) > 0})
    if not windows:
        windows = [20]
    window_reports: list[dict[str, Any]] = []
    primary_summary: dict[str, Any] | None = None
    for index, window in enumerate(windows):
        summary = recent_replay_summary(
            service,
            symbol=symbol,
            regime=regime,
            active_profile_id=active_profile_id,
            limit=window,
        )
        if index == 0:
            primary_summary = summary
        window_reports.append({"window": window, "summary": summary})
    candidate_scores: dict[str, dict[str, Any]] = {}
    for row in comparison_rows:
        experiments: list[dict[str, Any]] = []
        for window_report in window_reports:
            summary = window_report["summary"]
            scorecard = service._offline_replay_scorecard_for_row(row=row, replay_summary=summary)
            scorecard["window"] = window_report["window"]
            experiments.append(scorecard)
        aggregate_adjustment = (
            round(sum(float(item.get("final_adjustment") or 0.0) for item in experiments) / len(experiments), 6)
            if experiments
            else 0.0
        )
        aggregate_confidence = (
            round(sum(float(item.get("confidence_weight") or 0.0) for item in experiments) / len(experiments), 6)
            if experiments
            else 0.0
        )
        consensus = "positive" if aggregate_adjustment > 0 else "negative" if aggregate_adjustment < 0 else "neutral"
        candidate_scores[row.profile_id] = {
            "aggregate_adjustment": aggregate_adjustment,
            "aggregate_confidence": aggregate_confidence,
            "consensus": consensus,
            "experiments": experiments,
        }
    return {
        "pipeline_version": service.settings.strategy_profile_offline_replay_pipeline_version,
        "history_window": {
            "window_sizes": windows,
            "target_symbol": symbol,
            "target_regime": regime,
            "active_profile_id": active_profile_id,
        },
        "stages": [
            "scope_history",
            "run_multi_window_replay_scoring",
            "score_symbol_bucket",
            "score_regime_bucket",
            "score_profile_bucket",
            "score_symbol_regime_profile_cross_bucket",
            "aggregate_window_experiments",
            "emit_candidate_adjustments",
        ],
        "candidate_scores": candidate_scores,
        "window_reports": window_reports,
        "primary_summary": primary_summary or {},
    }
