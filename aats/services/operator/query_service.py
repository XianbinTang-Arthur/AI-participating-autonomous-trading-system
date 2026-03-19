from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger, log_event
from aats.events import topics
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.execution import OrderState
from aats.schemas.operator import (
    AuthSource,
    BlockerSnapshotRecord,
    ExecutionErrorSummary,
    OperatorActionRecord,
    OperatorRole,
    OperatorUserRecord,
    ReconciliationValidationSummary,
    ReplayValidationSummary,
)
from aats.services.execution_engine.baseline_import import AccountBaselineImportService
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator
from aats.services.operator.accounts import (
    create_operator_user as create_managed_operator_user,
    delete_operator_user as delete_managed_operator_user,
    enabled_admin_count,
    update_operator_user as update_managed_operator_user,
)
from aats.services.operator.runtime_profiles import RuntimeProfileControlService
from aats.services.operator.strategy_profiles import StrategyProfileControlService
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.reconciliation_service.replay import ReplayEngine, ReplayResult
from aats.services.runtime_scope import (
    fills_for_scope,
    latest_reconciliation_for_scope,
    latest_snapshot_for_scope,
    order_states_for_scope,
    reconciliation_reports_for_scope,
    snapshots_for_scope,
    runtime_state_scope,
    latest_topic_event_for_scope,
)
from aats.services.strategy_execution_health import compute_strategy_execution_health

if TYPE_CHECKING:
    from aats.bootstrap.config import ApplicationRuntime


class OperatorQueryService:
    _STUCK_SUBMISSION_STATUSES = {"CREATED", "SUBMITTING"}

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self.runtime = runtime
        self.logger = get_logger("aats.operator_api")
        self.recovery_posture = RecoveryPostureEvaluator(runtime)
        self.state_scope = runtime_state_scope(runtime.settings)
        self._cache: dict[str, Any] = {}
        self.runtime_profiles = RuntimeProfileControlService(
            settings=runtime.settings,
            repo=runtime.runtime_profile_repo,
            execution_repo=runtime.execution_repo,
            event_store=runtime.event_store,
        )
        self.strategy_profiles = StrategyProfileControlService(runtime)

    def _cached(self, key: str, loader):
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def _invalidate_cache(self) -> None:
        self._cache.clear()

    def _scoped_order_states(self):
        return self._cached(
            "scoped_order_states",
            lambda: order_states_for_scope(self.runtime.execution_repo, self.state_scope),
        )

    def _scoped_open_order_states(self):
        return self._cached(
            "scoped_open_order_states",
            lambda: order_states_for_scope(self.runtime.execution_repo, self.state_scope, open_only=True),
        )

    def _scoped_fills(self):
        return self._cached(
            "scoped_fills",
            lambda: fills_for_scope(self.runtime.execution_repo, self.state_scope),
        )

    def _latest_scoped_snapshot(self):
        return self._cached(
            "latest_scoped_snapshot",
            lambda: latest_snapshot_for_scope(self.runtime.portfolio_repo, self.state_scope),
        )

    def _current_symbol_position_qty(self, symbol: str) -> Any:
        snapshot = self._latest_scoped_snapshot()
        if snapshot is None:
            return Decimal("0")
        for position in snapshot.positions:
            if position.symbol == symbol:
                return position.position_qty
        if snapshot.product_type == "spot" and "-" in symbol:
            base_currency, _quote_currency = symbol.split("-", 1)
            return snapshot.balances.get(base_currency, Decimal("0"))
        return Decimal("0")

    def strategy_execution_health(self, symbol: str | None = None) -> dict[str, Any]:
        target_symbol = symbol or self.runtime.settings.default_symbol
        snapshot = compute_strategy_execution_health(
            settings=self.runtime.settings,
            symbol=target_symbol,
            fills=self._scoped_fills(),
            snapshots=snapshots_for_scope(self.runtime.portfolio_repo, self.state_scope),
            current_position_qty=self._current_symbol_position_qty(target_symbol),
        )
        return snapshot.as_payload(
            settings=self.runtime.settings,
            as_of=utc_now(),
            current_position_qty=self._current_symbol_position_qty(target_symbol),
        )

    def _current_runtime_started_at(self) -> datetime:
        return self.runtime.started_at

    def _is_current_runtime_timestamp(self, value: datetime | str | None) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            try:
                value = datetime.fromisoformat(normalized)
            except ValueError:
                return False
        return value >= self._current_runtime_started_at()

    def _latest_scoped_reconciliation(self):
        return self._cached(
            "latest_scoped_reconciliation",
            lambda: latest_reconciliation_for_scope(self.runtime.reconciliation_repo, self.state_scope),
        )

    def _latest_scoped_portfolio_event(self):
        return latest_topic_event_for_scope(self.runtime.event_store, topics.PORTFOLIO_SNAPSHOTS, self.state_scope)

    def payload(self, envelope: EventEnvelope | None) -> dict[str, Any] | None:
        if envelope is None:
            return None
        payload = dict(envelope.payload)
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def payload_by_ref(self, ref: str | None) -> dict[str, Any] | None:
        if ref is None:
            return None
        return self.payload(self.runtime.event_store.get(ref))

    def payloads_by_refs(self, refs: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in refs:
            payload = self.payload_by_ref(ref)
            if payload is not None:
                rows.append(payload)
        return rows

    @staticmethod
    def _paginate_rows(
        rows: list[Any],
        *,
        limit: int,
        offset: int,
        key: str,
        serializer=None,
    ) -> dict[str, Any]:
        total_available = len(rows)
        paged_rows = rows[offset : offset + limit]
        payloads = [serializer(item) for item in paged_rows] if serializer is not None else list(paged_rows)
        return {
            key: payloads,
            "limit": limit,
            "offset": offset,
            "total_available": total_available,
            "has_more": offset + len(paged_rows) < total_available,
        }

    def latest_order(self):
        rows = self._scoped_order_states()
        return max(rows, key=lambda item: item.last_update_ts or item.created_at, default=None)

    def latest_fill(self):
        rows = self._scoped_fills()
        return max(rows, key=lambda item: item.ingestion_timestamp, default=None)

    def latest_account_baseline(self) -> dict[str, Any] | None:
        latest = self._cached(
            "latest_account_baseline_event",
            lambda: latest_topic_event_for_scope(
                self.runtime.event_store,
                topics.ACCOUNT_BASELINES,
                self.state_scope,
            ),
        )
        return latest.payload if latest is not None else None

    def recent_fills(self, *, limit: int = 50):
        return sorted(
            self._scoped_fills(),
            key=lambda item: (item.ingestion_timestamp, item.fill_id),
            reverse=True,
        )[:limit]

    def latest_decision_id(self) -> str | None:
        latest = self._cached("latest_decision_record", self.runtime.audit_repo.latest)
        return latest.decision_id if latest is not None else None

    def ai_runtime(self) -> dict[str, Any]:
        return self.runtime.ai_service.status()

    def _recent_ai_takeover_events(self, *, limit: int | None = None):
        if not self._ai_history_visible():
            return []
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.AI_TAKEOVER_DECISIONS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_ai_shadow_evaluation_events(self, *, limit: int | None = None):
        if not self._ai_history_visible():
            return []
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.AI_SHADOW_EVALUATIONS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_ai_performance_report_events(self, *, limit: int | None = None):
        if not self._ai_history_visible():
            return []
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.AI_PERFORMANCE_REPORTS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_strategy_profile_optimization_report_events(self, *, limit: int | None = None):
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    def _recent_strategy_profile_selection_decision_events(self, *, limit: int | None = None):
        rows = list(
            reversed(
                self.runtime.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                    scope=self.state_scope,
                )
            )
        )
        if limit is None:
            return rows
        return rows[:limit]

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator, 6)

    def _ai_takeover_summary(self, *, limit: int = 50) -> dict[str, Any]:
        payloads = [self.payload(item) for item in self._recent_ai_takeover_events(limit=limit)]
        payloads = [item for item in payloads if item is not None]
        attempted = len(payloads)
        allowed = sum(1 for item in payloads if item.get("ai_takeover_allowed"))
        applied = sum(1 for item in payloads if item.get("ai_takeover_applied"))
        disagreement = sum(
            1
            for item in payloads
            if item.get("baseline_direction") not in {None, ""}
            and item.get("ai_direction") not in {None, ""}
            and item.get("baseline_direction") != item.get("ai_direction")
        )
        blocker_counts: dict[str, int] = {}
        for item in payloads:
            for blocker in item.get("ai_takeover_blockers", []):
                blocker_counts[str(blocker)] = blocker_counts.get(str(blocker), 0) + 1
        top_blockers = sorted(
            (
                {"blocker": blocker, "count": count}
                for blocker, count in blocker_counts.items()
            ),
            key=lambda row: (-row["count"], row["blocker"]),
        )[:5]
        latest = payloads[0] if payloads else None
        return {
            "window_size": attempted,
            "attempted_count": attempted,
            "allowed_count": allowed,
            "applied_count": applied,
            "blocked_count": max(attempted - applied, 0),
            "disagreement_count": disagreement,
            "allowed_rate": self._ratio(allowed, attempted),
            "applied_rate": self._ratio(applied, attempted),
            "blocked_rate": self._ratio(max(attempted - applied, 0), attempted),
            "disagreement_rate": self._ratio(disagreement, attempted),
            "top_blockers": top_blockers,
            "latest_takeover_at": latest.get("created_at") if latest is not None else None,
        }

    def _ai_shadow_summary(self, *, limit: int = 10) -> dict[str, Any]:
        payloads = [self.payload(item) for item in self._recent_ai_shadow_evaluation_events(limit=limit)]
        payloads = [item for item in payloads if item is not None]
        latest = payloads[0] if payloads else None
        outperformed_count = sum(1 for item in payloads if item.get("shadow_outperformed") is True)
        underperformed_count = sum(1 for item in payloads if item.get("shadow_outperformed") is False)
        if latest is None:
            return {
                "window_count": 0,
                "status": "insufficient_data",
                "review_required": False,
                "outperformed_count": 0,
                "underperformed_count": 0,
                "outperformed_rate": 0.0,
                "latest_evaluation": None,
                "latest_net_pnl_delta": None,
                "latest_fee_ratio_delta": None,
                "latest_churn_ratio_delta": None,
            }
        latest_net_pnl_delta = (
            Decimal(str(latest.get("shadow_net_pnl") or "0"))
            - Decimal(str(latest.get("baseline_net_pnl") or "0"))
        )
        latest_fee_ratio_delta = round(
            float(latest.get("shadow_fee_ratio") or 0.0) - float(latest.get("baseline_fee_ratio") or 0.0),
            6,
        )
        latest_churn_ratio_delta = round(
            float(latest.get("shadow_churn_ratio") or 0.0) - float(latest.get("baseline_churn_ratio") or 0.0),
            6,
        )
        review_required = (
            len(payloads) >= 2
            and (
                underperformed_count >= min(2, len(payloads))
                or latest_fee_ratio_delta > 0.05
                or latest_churn_ratio_delta > 0.08
            )
        )
        status = "healthy"
        if review_required:
            status = "review_required"
        elif latest.get("shadow_outperformed") is False:
            status = "underperforming"
        elif outperformed_count and underperformed_count:
            status = "mixed"
        return {
            "window_count": len(payloads),
            "status": status,
            "review_required": review_required,
            "outperformed_count": outperformed_count,
            "underperformed_count": underperformed_count,
            "outperformed_rate": self._ratio(outperformed_count, len(payloads)),
            "latest_evaluation": latest,
            "latest_net_pnl_delta": latest_net_pnl_delta,
            "latest_fee_ratio_delta": latest_fee_ratio_delta,
            "latest_churn_ratio_delta": latest_churn_ratio_delta,
        }

    def _ai_shadow_performance_windows(self) -> dict[str, Any]:
        latest_report_payload = self._latest_ai_performance_report_payload()
        if latest_report_payload is not None and isinstance(latest_report_payload.get("windows"), dict):
            return latest_report_payload["windows"]
        evaluations = [self.payload(item) for item in self._recent_ai_shadow_evaluation_events(limit=40)]
        evaluations = [item for item in evaluations if item is not None]
        windows = {
            "short": {"sample_size": 3, "label": "recent_3_windows"},
            "medium": {"sample_size": 5, "label": "recent_5_windows"},
            "long": {"sample_size": 10, "label": "recent_10_windows"},
        }
        result: dict[str, Any] = {}
        for key, config in windows.items():
            sample_size = config["sample_size"]
            rows = evaluations[:sample_size]
            if not rows:
                result[key] = {
                    "label": config["label"],
                    "sample_size": 0,
                    "outperformed_rate": 0.0,
                    "baseline_net_pnl_total": None,
                    "shadow_net_pnl_total": None,
                    "net_pnl_delta_total": None,
                    "avg_fee_ratio_delta": None,
                    "avg_churn_ratio_delta": None,
                    "review_required_count": 0,
                }
                continue
            baseline_net_total = sum(Decimal(str(item.get("baseline_net_pnl") or "0")) for item in rows)
            shadow_net_total = sum(Decimal(str(item.get("shadow_net_pnl") or "0")) for item in rows)
            fee_ratio_deltas = [
                float(item.get("shadow_fee_ratio") or 0.0) - float(item.get("baseline_fee_ratio") or 0.0)
                for item in rows
            ]
            churn_ratio_deltas = [
                float(item.get("shadow_churn_ratio") or 0.0) - float(item.get("baseline_churn_ratio") or 0.0)
                for item in rows
            ]
            outperformed = sum(1 for item in rows if item.get("shadow_outperformed") is True)
            review_required_count = sum(
                1
                for item in rows
                if (
                    (float(item.get("shadow_fee_ratio") or 0.0) - float(item.get("baseline_fee_ratio") or 0.0))
                    > self.runtime.settings.ai_outcome_max_fee_ratio_delta
                )
                or (
                    (float(item.get("shadow_churn_ratio") or 0.0) - float(item.get("baseline_churn_ratio") or 0.0))
                    > self.runtime.settings.ai_outcome_max_churn_ratio_delta
                )
                or item.get("shadow_outperformed") is False
            )
            result[key] = {
                "label": config["label"],
                "sample_size": len(rows),
                "outperformed_rate": self._ratio(outperformed, len(rows)),
                "baseline_net_pnl_total": baseline_net_total,
                "shadow_net_pnl_total": shadow_net_total,
                "net_pnl_delta_total": shadow_net_total - baseline_net_total,
                "avg_fee_ratio_delta": round(sum(fee_ratio_deltas) / len(fee_ratio_deltas), 6),
                "avg_churn_ratio_delta": round(sum(churn_ratio_deltas) / len(churn_ratio_deltas), 6),
                "review_required_count": review_required_count,
            }
        return result

    def _latest_ai_performance_report_payload(self) -> dict[str, Any] | None:
        rows = [self.payload(item) for item in self._recent_ai_performance_report_events(limit=1)]
        rows = [item for item in rows if item is not None]
        return rows[0] if rows else None

    def ai_performance_overview(self) -> dict[str, Any]:
        reports = [self.payload(item) for item in self._recent_ai_performance_report_events(limit=20)]
        reports = [item for item in reports if item is not None]
        latest = reports[0] if reports else None
        recent_replay = self.replay_recent_validations(limit=10, offset=0)["validations"]
        if latest is None:
            return {
                "latest_report": None,
                "report_count": 0,
                "status_counts": {},
                "trend": {
                    "avg_short_net_pnl_delta": None,
                    "avg_medium_net_pnl_delta": None,
                    "avg_long_net_pnl_delta": None,
                    "review_required_rate": 0.0,
                },
                "recent_reports": [],
                "replay_context": {
                    "validation_count": len(recent_replay),
                    "healthy_rate": self._ratio(
                        sum(1 for item in recent_replay if item.get("healthy")),
                        len(recent_replay),
                    ),
                    "latest_validation": recent_replay[0] if recent_replay else None,
                },
            }
        status_counts: dict[str, int] = {}
        short_deltas: list[Decimal] = []
        medium_deltas: list[Decimal] = []
        long_deltas: list[Decimal] = []
        review_required_count = 0
        for report in reports:
            status = str(report.get("latest_status") or "insufficient_data")
            status_counts[status] = status_counts.get(status, 0) + 1
            if report.get("review_required"):
                review_required_count += 1
            windows = report.get("windows") or {}
            for target, bucket in (("short", short_deltas), ("medium", medium_deltas), ("long", long_deltas)):
                value = ((windows.get(target) or {}).get("net_pnl_delta_total"))
                if value is not None:
                    bucket.append(Decimal(str(value)))
        return {
            "latest_report": latest,
            "report_count": len(reports),
            "status_counts": status_counts,
            "trend": {
                "avg_short_net_pnl_delta": (sum(short_deltas, start=Decimal("0")) / Decimal(len(short_deltas))) if short_deltas else None,
                "avg_medium_net_pnl_delta": (sum(medium_deltas, start=Decimal("0")) / Decimal(len(medium_deltas))) if medium_deltas else None,
                "avg_long_net_pnl_delta": (sum(long_deltas, start=Decimal("0")) / Decimal(len(long_deltas))) if long_deltas else None,
                "review_required_rate": self._ratio(review_required_count, len(reports)),
            },
            "recent_reports": reports[:10],
            "replay_context": {
                "validation_count": len(recent_replay),
                "healthy_rate": self._ratio(
                    sum(1 for item in recent_replay if item.get("healthy")),
                    len(recent_replay),
                ),
                "latest_validation": recent_replay[0] if recent_replay else None,
            },
        }

    def _ai_downgrade_state(self) -> dict[str, Any]:
        runtime = self.ai_runtime()
        return {
            "configured_mode": runtime.get("configured_operating_mode"),
            "effective_mode": runtime.get("effective_operating_mode"),
            "provider_state": runtime.get("provider_state"),
            "outcome_state": runtime.get("outcome_state"),
            "degraded": runtime.get("degraded"),
            "provider_degraded": runtime.get("provider_degraded"),
            "outcome_review_required": runtime.get("outcome_review_required"),
            "outcome_auto_downgrade_active": runtime.get("outcome_auto_downgrade_active"),
            "degradation_reason": runtime.get("degradation_reason"),
            "outcome_degradation_reason": runtime.get("outcome_degradation_reason"),
            "last_provider_degraded_at": runtime.get("last_provider_degraded_at"),
            "last_provider_recovered_at": runtime.get("last_provider_recovered_at"),
            "last_outcome_degraded_at": runtime.get("last_outcome_degraded_at"),
            "last_outcome_recovered_at": runtime.get("last_outcome_recovered_at"),
            "recovery_probe_after": runtime.get("recovery_probe_after"),
            "recovery_probe_ready": runtime.get("recovery_probe_ready"),
            "failure_budget": runtime.get("failure_budget"),
            "outcome_policy": runtime.get("outcome_policy"),
        }

    def ai_overview(self) -> dict[str, Any]:
        latest = self.ai_latest()
        shadow_latest = self.ai_shadow_latest()
        latest_decision_id = self.latest_decision_id()
        latest_decision_detail = self.decision_view(latest_decision_id) if latest_decision_id is not None else None
        latest_degradation = latest_topic_event_for_scope(
            self.runtime.event_store,
            topics.AI_DEGRADATION_EVENTS,
            self.state_scope,
        )
        if not self._ai_history_visible():
            latest_degradation = None
        return {
            "runtime": self.ai_runtime(),
            "latest_brief": latest.get("brief"),
            "latest_assessment": latest.get("assessment"),
            "latest_takeover": latest.get("takeover"),
            "latest_shadow_decision": shadow_latest.get("shadow_decision"),
            "latest_degradation": self.payload(latest_degradation),
            "takeover_summary": self._ai_takeover_summary(),
            "shadow_summary": self._ai_shadow_summary(),
            "performance_windows": self._ai_shadow_performance_windows(),
            "latest_performance_report": self._latest_ai_performance_report_payload(),
            "performance_view": self.ai_performance_overview(),
            "downgrade_state": self._ai_downgrade_state(),
            "latest_execution_suggestion": None if latest_decision_detail is None else latest_decision_detail.get("ai_execution_suggestion"),
        }

    def _ai_history_visible(self) -> bool:
        return self.runtime.settings.ai_operating_mode != "baseline_only"

    def ai_latest(self) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"brief": None, "assessment": None, "takeover": None, "execution_suggestion": None}
        brief = latest_topic_event_for_scope(self.runtime.event_store, topics.AI_DECISION_BRIEFS, self.state_scope)
        assessment = latest_topic_event_for_scope(self.runtime.event_store, topics.AI_ASSESSMENTS, self.state_scope)
        takeover = latest_topic_event_for_scope(self.runtime.event_store, topics.AI_TAKEOVER_DECISIONS, self.state_scope)
        execution_plan = latest_topic_event_for_scope(self.runtime.event_store, topics.EXECUTION_PLANS, self.state_scope)
        order_intent = latest_topic_event_for_scope(self.runtime.event_store, topics.ORDER_INTENTS, self.state_scope)
        assessment_payload = self.payload(assessment)
        execution_plan_payload = self.payload(execution_plan)
        order_intent_payload = self.payload(order_intent)
        return {
            "brief": self.payload(brief),
            "assessment": assessment_payload,
            "takeover": self.payload(takeover),
            "execution_suggestion": self._ai_execution_suggestion_summary(
                ai_assessment=assessment_payload,
                execution_plan=execution_plan_payload,
                latest_order_intent=order_intent_payload,
            ),
        }

    def ai_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"assessments": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.runtime.event_store.by_topic_scoped(topics.AI_ASSESSMENTS, scope=self.state_scope)
        rows = list(reversed(rows))
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="assessments",
            serializer=self.payload,
        )

    def ai_shadow_latest(self) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"shadow_decision": None}
        shadow = latest_topic_event_for_scope(self.runtime.event_store, topics.AI_SHADOW_DECISIONS, self.state_scope)
        return {"shadow_decision": self.payload(shadow)}

    def ai_shadow_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"shadow_decisions": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.runtime.event_store.by_topic_scoped(topics.AI_SHADOW_DECISIONS, scope=self.state_scope)
        rows = list(reversed(rows))
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="shadow_decisions",
            serializer=self.payload,
        )

    def ai_shadow_evaluations(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"evaluations": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.runtime.event_store.by_topic_scoped(topics.AI_SHADOW_EVALUATIONS, scope=self.state_scope)
        rows = list(reversed(rows))
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="evaluations",
            serializer=self.payload,
        )

    def ai_performance_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"reports": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self._recent_ai_performance_report_events()
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="reports",
            serializer=self.payload,
        )

    def ai_takeovers_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"takeovers": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self._recent_ai_takeover_events()
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="takeovers",
            serializer=lambda envelope: {
                **(self.payload(envelope) or {}),
                "direction_disagreement": (
                    envelope.payload.get("baseline_direction") != envelope.payload.get("ai_direction")
                    if isinstance(envelope.payload, dict)
                    else False
                ),
            },
        )

    async def evaluate_ai_shadow(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        if not self._ai_history_visible():
            return {"evaluation": None, "status": "baseline_only_ai_history_hidden"}
        evaluation, created = self.runtime.ai_service.evaluate_shadow_window(
            limit=self.runtime.settings.ai_shadow_evaluation_window
        )
        if evaluation is None:
            return {"evaluation": None, "status": "no_shadow_decisions"}
        if created:
            await publish_model(
                bus=self.runtime.bus,
                topic=topics.AI_SHADOW_EVALUATIONS,
                key=evaluation.symbol,
                payload_model=evaluation,
                source_component="operator_api",
            )
            self._append_event(
                topic=topics.OPERATOR_ACTIONS,
                key="ai_shadow",
                payload_model=OperatorActionRecord(
                    action="ai_shadow_evaluate",
                    actor_role=actor_role,
                    actor_identity=actor_identity,
                    auth_source=auth_source,
                    reason="operator_ai_shadow_evaluate",
                    status="evaluation_created",
                    details={
                        "evaluation_id": evaluation.evaluation_id,
                        "window_size": evaluation.summary.get("window_size"),
                        "override_rate": evaluation.summary.get("override_rate"),
                    },
                ),
            )
            status = "evaluation_created"
        else:
            status = "evaluation_reused"
            self._append_event(
                topic=topics.OPERATOR_ACTIONS,
                key="ai_shadow",
                payload_model=OperatorActionRecord(
                    action="ai_shadow_evaluate",
                    actor_role=actor_role,
                    actor_identity=actor_identity,
                    auth_source=auth_source,
                    reason="operator_ai_shadow_evaluate",
                    status="evaluation_reused",
                    details={
                        "evaluation_id": evaluation.evaluation_id,
                        "window_size": evaluation.summary.get("window_size"),
                        "override_rate": evaluation.summary.get("override_rate"),
                    },
                ),
            ),
        return {"evaluation": evaluation.model_dump(mode="json"), "status": status}

    def latest_operator_action(self, action: str) -> dict[str, Any] | None:
        actions = self._cached(
            "operator_action_events",
            lambda: self.runtime.event_store.by_topic(topics.OPERATOR_ACTIONS),
        )
        for item in reversed(actions):
            if item.payload.get("action") == action:
                return item.payload
        return None

    def record_operator_login(
        self,
        *,
        actor_identity: str,
        actor_role: OperatorRole,
        auth_source: AuthSource = "session",
    ) -> None:
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="login",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_login",
                status="login_succeeded",
                details={"database_backed": self.runtime.database_runtime is not None},
            ),
        )

    def operator_users(self, *, actor_identity: str | None = None) -> dict[str, Any]:
        users = self.runtime.operator_repo.all_users()
        protected_last_admin = enabled_admin_count(self.runtime.operator_repo) <= 1
        return {
            "users": [
                self._operator_user_view(
                    user,
                    actor_identity=actor_identity,
                    last_admin_protected=protected_last_admin,
                )
                for user in users
            ],
            "enabled_user_count": self.runtime.operator_repo.count(enabled_only=True),
            "enabled_admin_count": enabled_admin_count(self.runtime.operator_repo),
        }

    def runtime_profile_snapshot(self) -> dict[str, Any]:
        snapshot = self._cached("runtime_profile_snapshot", self.runtime_profiles.snapshot)
        activation = snapshot["activation"]
        return {
            **snapshot,
            "profile_source": self.runtime.runtime_profile_resolution.profile_source,
            "active_revision_id": activation.get("active_revision_id"),
            "pending_revision_id": activation.get("pending_revision_id"),
            "restart_required": activation.get("restart_required", False),
        }

    def record_runtime_profile_action(
        self,
        *,
        action: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
        status: str,
        previous_revision_id: str | None = None,
        new_revision_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="runtime_profile",
            payload_model=self.runtime_profiles.audit_payload(
                action=action,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status=status,
                previous_revision_id=previous_revision_id,
                new_revision_id=new_revision_id,
                details=details,
            ),
        )

    def strategy_profile_snapshot(self) -> dict[str, Any]:
        return self._cached("strategy_profile_snapshot", self.strategy_profiles.snapshot)

    def strategy_profile_optimization_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self._recent_strategy_profile_optimization_report_events()
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="reports",
            serializer=self.payload,
        )

    def strategy_profile_selection_decisions(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self._recent_strategy_profile_selection_decision_events()
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="decisions",
            serializer=self.payload,
        )

    def strategy_profile_auto_rollback_policy(self) -> dict[str, Any]:
        return self.strategy_profiles.snapshot().get("auto_rollback_policy", {})

    def strategy_profile_auto_rollback_policy_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.strategy_profiles.snapshot().get("auto_rollback_policy_history", [])
        return self._paginate_rows(rows, limit=limit, offset=offset, key="history")

    def strategy_profile_activation_policy(self) -> dict[str, Any]:
        return self.strategy_profiles.snapshot().get("activation_policy", {})

    def strategy_profile_activation_policy_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.strategy_profiles.snapshot().get("activation_policy_history", [])
        return self._paginate_rows(rows, limit=limit, offset=offset, key="history")

    def strategy_profile_recommendations(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_recommendations(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="recommendations",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    def strategy_profile_activation_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_activation_history(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="history",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    def strategy_profile_rejections(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_rejections(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="rejections",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    def strategy_profile_evaluations(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_evaluations(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="evaluations",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    async def evaluate_strategy_profile(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = await self.strategy_profiles.evaluate_now()
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_evaluate",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="recommendation_generated",
                details={"recommended_profile_id": result["recommendation"]["recommended_profile_id"]},
            ),
        )
        self._invalidate_cache()
        return result

    def accept_strategy_profile_recommendation(
        self,
        *,
        recommendation_id: str,
        activation_mode: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.accept_recommendation(
            recommendation_id=recommendation_id,
            activation_mode=activation_mode,
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_accept",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status=result["status"],
                details={
                    "recommendation_id": recommendation_id,
                    "activation_mode": activation_mode,
                    "active_profile_id": result.get("active_revision", {}).get("profile_id"),
                },
            ),
        )
        self._invalidate_cache()
        return result

    def reject_strategy_profile_recommendation(
        self,
        *,
        recommendation_id: str,
        reason_code: str,
        reason_detail: str | None,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.reject_recommendation(
            recommendation_id=recommendation_id,
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_role=actor_role,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_reject",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="recommendation_rejected",
                details={"recommendation_id": recommendation_id, "reason_code": reason_code},
            ),
        )
        self._invalidate_cache()
        return result

    def activate_pending_strategy_profile(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.activate_pending(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_activate_pending",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="pending_profile_activated",
                details={"active_profile_id": result["active_revision"]["profile_id"]},
            ),
        )
        self._invalidate_cache()
        return result

    def rollback_strategy_profile(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.rollback(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_rollback",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="profile_rolled_back",
                details={"active_profile_id": result["active_revision"]["profile_id"]},
            ),
        )
        self._invalidate_cache()
        return result

    def update_strategy_profile_auto_rollback_policy(
        self,
        *,
        enabled: bool,
        review_required_only: bool,
        min_trade_count: int,
        cooldown_seconds: float,
        matrix_allowed_symbols: tuple[str, ...],
        matrix_allowed_regimes: tuple[str, ...],
        matrix_allowed_profiles: tuple[str, ...],
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.update_auto_rollback_policy(
            enabled=enabled,
            review_required_only=review_required_only,
            min_trade_count=min_trade_count,
            cooldown_seconds=cooldown_seconds,
            matrix_allowed_symbols=matrix_allowed_symbols,
            matrix_allowed_regimes=matrix_allowed_regimes,
            matrix_allowed_profiles=matrix_allowed_profiles,
            reason=reason,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_rollback",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="auto_rollback_policy_updated",
                details=policy,
            ),
        )
        self._invalidate_cache()
        return {"policy": policy}

    def approve_strategy_profile_auto_rollback_policy(
        self,
        *,
        policy_id: str | None,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.approve_auto_rollback_policy(
            policy_id=policy_id,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_rollback",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="auto_rollback_policy_approved",
                details=policy,
            ),
        )
        self._invalidate_cache()
        return {"policy": policy}

    def freeze_strategy_profile_auto_rollback_policy(
        self,
        *,
        frozen: bool,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.freeze_auto_rollback_policy(
            frozen=frozen,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_rollback",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="auto_rollback_policy_frozen" if frozen else "auto_rollback_policy_unfrozen",
                details=policy,
            ),
        )
        self._invalidate_cache()
        return {"policy": policy}

    def update_strategy_profile_activation_policy(
        self,
        *,
        enabled: bool,
        min_composite_score: float,
        min_offline_replay_score: float,
        min_recommendation_strength: float,
        require_positive_replay_consensus: bool,
        disallow_when_shadow_review_required: bool,
        matrix_allowed_symbols: tuple[str, ...],
        matrix_allowed_regimes: tuple[str, ...],
        matrix_allowed_profiles: tuple[str, ...],
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.update_activation_policy(
            enabled=enabled,
            min_composite_score=min_composite_score,
            min_offline_replay_score=min_offline_replay_score,
            min_recommendation_strength=min_recommendation_strength,
            require_positive_replay_consensus=require_positive_replay_consensus,
            disallow_when_shadow_review_required=disallow_when_shadow_review_required,
            matrix_allowed_symbols=matrix_allowed_symbols,
            matrix_allowed_regimes=matrix_allowed_regimes,
            matrix_allowed_profiles=matrix_allowed_profiles,
            reason=reason,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_activation_policy",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="activation_policy_updated",
                details=policy,
            ),
        )
        self._invalidate_cache()
        return {"policy": policy}

    def approve_strategy_profile_activation_policy(
        self,
        *,
        policy_id: str | None,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.approve_activation_policy(
            policy_id=policy_id,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_activation_policy",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="activation_policy_approved",
                details=policy,
            ),
        )
        self._invalidate_cache()
        return {"policy": policy}

    def freeze_strategy_profile_activation_policy(
        self,
        *,
        frozen: bool,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.freeze_activation_policy(
            frozen=frozen,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action="strategy_profile_activation_policy",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status="activation_policy_frozen" if frozen else "activation_policy_unfrozen",
                details=policy,
            ),
        )
        self._invalidate_cache()
        return {"policy": policy}

    def create_operator_user(
        self,
        *,
        username: str,
        password: str,
        role: OperatorRole,
        enabled: bool,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        user = create_managed_operator_user(
            self.runtime.operator_repo,
            username=username,
            password=password,
            role=role,
            enabled=enabled,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="user_create",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_user_create",
                status="user_created",
                details={
                    "target_username": user.username,
                    "target_role": user.role,
                    "target_enabled": user.enabled,
                },
            ),
        )
        return {"user": self._operator_user_view(user, actor_identity=actor_identity)}

    def update_operator_user(
        self,
        *,
        username: str,
        role: OperatorRole | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        user, changes = update_managed_operator_user(
            self.runtime.operator_repo,
            username=username,
            role=role,
            enabled=enabled,
            password=password,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="user_update",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_user_update",
                status="user_updated",
                details={
                    "target_username": user.username,
                    "changes": changes,
                },
            ),
        )
        return {
            "user": self._operator_user_view(user, actor_identity=actor_identity),
            "changes": changes,
        }

    def delete_operator_user(
        self,
        *,
        username: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        user = delete_managed_operator_user(
            self.runtime.operator_repo,
            username=username,
            actor_identity=actor_identity,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="auth",
            payload_model=OperatorActionRecord(
                action="user_delete",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_user_delete",
                status="user_deleted",
                details={
                    "target_username": user.username,
                    "target_role": user.role,
                },
            ),
        )
        return {"status": "deleted", "user": self._operator_user_view(user, actor_identity=actor_identity)}

    def recovery_view(self) -> dict[str, Any]:
        return self._cached("recovery_view", self._build_recovery_view)

    def _build_recovery_view(self) -> dict[str, Any]:
        latest_reconciliation = self._latest_scoped_reconciliation()
        latest_baseline = self.latest_account_baseline()
        latest_rebaseline_action = self.latest_operator_action("rebaseline")
        latest_resume_action = self.latest_operator_action("resume")
        latest_ai_degradation = latest_topic_event_for_scope(
            self.runtime.event_store,
            topics.AI_DEGRADATION_EVENTS,
            self.state_scope,
        )
        latest_ai_shadow_evaluation = latest_topic_event_for_scope(
            self.runtime.event_store,
            topics.AI_SHADOW_EVALUATIONS,
            self.state_scope,
        )
        base = self.recovery_posture.finalize_status(latest_reconciliation=latest_reconciliation)
        if not self._ai_history_visible():
            latest_ai_degradation = None
            latest_ai_shadow_evaluation = None
        return {
            **base.model_dump(mode="json"),
            "last_rebaseline_action": latest_rebaseline_action,
            "last_resume_action": latest_resume_action,
            "latest_account_baseline": latest_baseline,
            "latest_reconciliation": latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None,
            "latest_ai_degradation": self.payload(latest_ai_degradation),
            "latest_ai_shadow_evaluation": self.payload(latest_ai_shadow_evaluation),
            "ai_runtime": self.ai_runtime(),
        }

    def system_recovery(self) -> dict[str, Any]:
        recovery = self.recovery_view()
        return {
            "recovery": recovery,
            "latest_rebaseline_action": recovery["last_rebaseline_action"],
            "latest_resume_action": recovery["last_resume_action"],
            "latest_account_baseline": recovery["latest_account_baseline"],
        }

    def system_mode(self) -> dict[str, Any]:
        return self._cached("system_mode", self._build_system_mode)

    def _build_system_mode(self) -> dict[str, Any]:
        snapshot = dict(self.runtime.mode_controller.snapshot())
        readiness = self.runtime.execution_adapter.readiness()
        recovery = self.recovery_view()
        submit_blocked_reasons = list(
            dict.fromkeys(
                list(snapshot.get("submit_blocked_reasons", []))
                + list(readiness.get("submit_blocked_reasons", []))
            )
        )
        health_blockers = list(dict.fromkeys(self.runtime.health_service.execution_blockers()))
        recovery_blockers = list(dict.fromkeys(recovery["resume_blocked_reasons"]))
        exchange_submit_allowed = bool(
            readiness.get("exchange_submit_allowed", snapshot.get("exchange_submit_allowed", False))
        )
        execution_blockers = self.recovery_posture.execution_blockers(
            health_blockers=health_blockers,
            recovery_blockers=recovery_blockers,
            submit_blocked_reasons=submit_blocked_reasons,
        )

        snapshot["exchange_submit_allowed"] = exchange_submit_allowed
        snapshot["submit_blocked"] = bool(submit_blocked_reasons) or not exchange_submit_allowed
        snapshot["submit_blocked_reasons"] = submit_blocked_reasons
        snapshot["execution_blocked"] = bool(execution_blockers)
        snapshot["blocked_reason"] = execution_blockers[0] if execution_blockers else None
        snapshot["recovery_state"] = recovery["recovery_state"]
        snapshot["review_required"] = recovery["review_required"]
        snapshot["rebaseline_available"] = recovery["rebaseline_available"]
        snapshot["profile_source"] = self.runtime.runtime_profile_resolution.profile_source
        snapshot["active_profile_revision_id"] = self.runtime.runtime_profile_resolution.activation_state.active_revision_id
        snapshot["pending_profile_revision_id"] = self.runtime.runtime_profile_resolution.activation_state.pending_revision_id
        snapshot["restart_required"] = self.runtime.runtime_profile_resolution.activation_state.restart_required
        return snapshot

    def _effective_taker_fee_bps(self, *, symbol: str | None = None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is not None and hasattr(resolver, "taker_fee_bps"):
            return float(resolver.taker_fee_bps(symbol=symbol))
        getter = getattr(self.runtime.account_service, "effective_taker_fee_bps", None)
        if callable(getter):
            try:
                resolved = getter(symbol=symbol)
            except TypeError:
                resolved = getter(symbol) if symbol is not None else getter()
            if resolved is not None:
                return max(float(resolved), 0.0)
        return max(self.runtime.settings.paper_taker_fee_bps, 0.0)

    def _effective_maker_fee_bps(self, *, symbol: str | None = None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is not None and hasattr(resolver, "maker_fee_bps"):
            return float(resolver.maker_fee_bps(symbol=symbol))
        return self._effective_taker_fee_bps(symbol=symbol)

    def _funding_fee_bps(self, *, symbol: str | None = None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is not None and hasattr(resolver, "funding_fee_bps"):
            return float(resolver.funding_fee_bps(symbol=symbol))
        return 0.0

    @staticmethod
    def _execution_suggestion_payload(ai_assessment: dict[str, Any] | None) -> dict[str, Any] | None:
        envelope = None if ai_assessment is None else ai_assessment.get("ai_execution_parameter_suggestion")
        if not isinstance(envelope, dict):
            return None
        suggestion = envelope.get("suggestion")
        return suggestion if isinstance(suggestion, dict) else None

    def _estimated_execution_fee_bps(self, *, symbol: str | None, ai_assessment: dict[str, Any] | None) -> float:
        resolver = getattr(self.runtime, "fee_resolver", None)
        if resolver is None or not hasattr(resolver, "estimated_execution_fee_bps"):
            return self._effective_taker_fee_bps(symbol=symbol)
        suggestion = self._execution_suggestion_payload(ai_assessment)
        return float(
            resolver.estimated_execution_fee_bps(
                symbol=symbol,
                execution_style="bounded_limit_ioc" if suggestion is not None else "taker",
                order_type="limit" if suggestion is not None else "market",
                passive_bias=None if suggestion is None else suggestion.get("passive_bias"),
                maker_taker_bias=None if suggestion is None else suggestion.get("maker_taker_bias"),
            )
        )

    def _ai_economic_actionability(
        self,
        *,
        ai_assessment: dict[str, Any] | None,
        ai_takeover_decision: dict[str, Any] | None,
        position_target: dict[str, Any] | None,
        ai_decision_brief: dict[str, Any] | None,
        strategy_execution_health: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if ai_assessment is None:
            return None
        symbol = None
        if position_target is not None:
            symbol = position_target.get("symbol")
        if not symbol:
            symbol = ai_assessment.get("symbol")
        effective_taker_fee_bps = self._effective_taker_fee_bps(symbol=symbol)
        effective_maker_fee_bps = self._effective_maker_fee_bps(symbol=symbol)
        estimated_execution_fee_bps = self._estimated_execution_fee_bps(symbol=symbol, ai_assessment=ai_assessment)
        funding_fee_bps = (
            self._funding_fee_bps(symbol=symbol)
            if self.runtime.settings.trading_product_type == "derivatives"
            else 0.0
        )
        required_total_edge_bps = (
            estimated_execution_fee_bps
            + (
                max(self.runtime.settings.max_slippage_tolerance_bps, 0)
                * max(self.runtime.settings.strategy_expected_slippage_bps_fraction, 0.0)
            )
            + funding_fee_bps
            + max(self.runtime.settings.strategy_edge_noise_buffer_bps, 0.0)
            + max(self.runtime.settings.strategy_min_net_edge_bps, 0.0)
        )
        funding_fee_summary = None
        if hasattr(self.runtime.account_service, "recent_funding_fee_summary"):
            funding_fee_summary = self.runtime.account_service.recent_funding_fee_summary(symbol=symbol)
        return {
            "economically_actionable": ai_assessment.get("economically_actionable"),
            "fallback_used": ai_assessment.get("fallback_used"),
            "degraded": ai_assessment.get("degraded"),
            "estimated_edge_bps": ai_assessment.get("estimated_edge_bps"),
            "estimated_cost_bps": ai_assessment.get("estimated_cost_bps"),
            "estimated_net_edge_bps": ai_assessment.get("estimated_net_edge_bps"),
            "effective_taker_fee_bps": effective_taker_fee_bps,
            "effective_maker_fee_bps": effective_maker_fee_bps,
            "estimated_execution_fee_bps": estimated_execution_fee_bps,
            "funding_fee_bps": funding_fee_bps,
            "funding_fee_summary": funding_fee_summary,
            "min_required_net_edge_bps": self.runtime.settings.strategy_min_net_edge_bps,
            "noise_buffer_bps": self.runtime.settings.strategy_edge_noise_buffer_bps,
            "required_total_edge_bps": required_total_edge_bps,
            "target_expected_signal_edge_bps": position_target.get("expected_signal_edge_bps") if position_target else None,
            "target_expected_cost_bps": position_target.get("expected_cost_bps") if position_target else None,
            "target_expected_net_edge_bps": position_target.get("expected_net_edge_bps") if position_target else None,
            "validation_flags": list(ai_assessment.get("validation_flags") or []),
            "rejection_flags": list(ai_assessment.get("rejection_flags") or []),
            "takeover_allowed": None if ai_takeover_decision is None else ai_takeover_decision.get("ai_takeover_allowed"),
            "takeover_applied": None if ai_takeover_decision is None else ai_takeover_decision.get("ai_takeover_applied"),
            "takeover_blockers": [] if ai_takeover_decision is None else list(ai_takeover_decision.get("ai_takeover_blockers") or []),
            "market_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("market_snapshot_fresh"),
            "account_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("account_snapshot_fresh"),
            "safe_to_trade": None if ai_decision_brief is None else ai_decision_brief.get("safe_to_trade"),
            "execution_condition": ai_assessment.get("execution_condition"),
            "current_open_order_count": None if ai_decision_brief is None else ai_decision_brief.get("current_open_order_count"),
            "recent_fee_drag_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_fee_drag_ratio"),
            "recent_churn_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_churn_ratio"),
            "recent_low_edge_trade_streak": None if strategy_execution_health is None else strategy_execution_health.get("recent_low_edge_trade_streak"),
            "guardrail_flags": [] if position_target is None else list(position_target.get("guardrail_flags") or []),
        }

    @staticmethod
    def _direction_from_edge(edge: Any) -> str:
        try:
            numeric = float(edge or 0.0)
        except (TypeError, ValueError):
            return "flat"
        if numeric > 0:
            return "long"
        if numeric < 0:
            return "short"
        return "flat"

    def _ai_decision_audit(
        self,
        *,
        audit,
        decision_context: dict[str, Any] | None,
        ai_decision_brief: dict[str, Any] | None,
        baseline_assessment: dict[str, Any] | None,
        ai_assessment: dict[str, Any] | None,
        ai_takeover_decision: dict[str, Any] | None,
        position_target: dict[str, Any] | None,
        strategy_execution_health: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if ai_assessment is None and ai_takeover_decision is None:
            return None
        return {
            "configured_mode": self.runtime.settings.ai_operating_mode,
            "assessment_operating_mode": None if ai_assessment is None else ai_assessment.get("operating_mode"),
            "provider_name": None if ai_assessment is None else ai_assessment.get("provider_name"),
            "provider_request_id": None if ai_assessment is None else ai_assessment.get("provider_request_id"),
            "fallback_used": None if ai_assessment is None else ai_assessment.get("fallback_used"),
            "degraded": None if ai_assessment is None else ai_assessment.get("degraded"),
            "baseline_direction": None if baseline_assessment is None else baseline_assessment.get("direction_bias"),
            "ai_direction": self._direction_from_edge(None if ai_assessment is None else ai_assessment.get("directional_edge")),
            "final_direction": None if position_target is None else position_target.get("target_exposure_side"),
            "takeover_attempted": ai_takeover_decision is not None,
            "takeover_allowed": None if ai_takeover_decision is None else ai_takeover_decision.get("ai_takeover_allowed"),
            "takeover_applied": None if ai_takeover_decision is None else ai_takeover_decision.get("ai_takeover_applied"),
            "takeover_blockers": [] if ai_takeover_decision is None else list(ai_takeover_decision.get("ai_takeover_blockers") or []),
            "market_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("market_snapshot_fresh"),
            "account_snapshot_fresh": None if ai_decision_brief is None else ai_decision_brief.get("account_snapshot_fresh"),
            "safe_to_trade": None if ai_decision_brief is None else ai_decision_brief.get("safe_to_trade"),
            "execution_condition": None if ai_assessment is None else ai_assessment.get("execution_condition"),
            "current_open_order_count": None if ai_decision_brief is None else ai_decision_brief.get("current_open_order_count"),
            "guardrail_flags": [] if position_target is None else list(position_target.get("guardrail_flags") or []),
            "strategy_guardrail_flags": [] if decision_context is None else list(decision_context.get("strategy_guardrail_flags") or []),
            "recent_fee_drag_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_fee_drag_ratio"),
            "recent_churn_ratio": None if strategy_execution_health is None else strategy_execution_health.get("recent_churn_ratio"),
            "recent_low_edge_trade_streak": None if strategy_execution_health is None else strategy_execution_health.get("recent_low_edge_trade_streak"),
            "audit_ref_counts": {
                "order_intents": len(audit.order_intent_refs),
                "order_updates": len(audit.order_state_refs),
                "fills": len(audit.fill_event_refs),
                "reconciliations": len(audit.reconciliation_refs),
                "shadow_decisions": len(audit.ai_shadow_decision_refs),
                "shadow_evaluations": len(audit.ai_shadow_evaluation_refs),
            },
        }

    def _ai_execution_suggestion_summary(
        self,
        *,
        ai_assessment: dict[str, Any] | None,
        execution_plan: dict[str, Any] | None,
        latest_order_intent: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assessment_suggestion = None if ai_assessment is None else ai_assessment.get("ai_execution_parameter_suggestion")
        plan_translation = None if execution_plan is None else execution_plan.get("ai_execution_parameter_suggestion")
        intent_translation = (
            None if latest_order_intent is None else latest_order_intent.get("ai_execution_parameter_suggestion")
        )
        latest_translation = intent_translation or plan_translation or assessment_suggestion
        return {
            "configured_mode": self.runtime.settings.ai_execution_suggestion_mode,
            "assessment_suggestion": assessment_suggestion,
            "execution_plan_translation": plan_translation,
            "latest_order_intent_translation": intent_translation,
            "latest_translation": latest_translation,
            "live_order_type": None if latest_order_intent is None else latest_order_intent.get("order_type"),
            "live_time_in_force": None if latest_order_intent is None else latest_order_intent.get("time_in_force"),
            "live_limit_price": None if latest_order_intent is None else latest_order_intent.get("limit_price"),
            "live_execution_style": None if latest_order_intent is None else latest_order_intent.get("execution_style"),
            "suggestion_present": assessment_suggestion is not None,
            "translation_present": plan_translation is not None or intent_translation is not None,
            "status": "absent" if latest_translation is None else latest_translation.get("status"),
        }

    def system_health(self) -> dict[str, Any]:
        snapshot = self.runtime.health_service.snapshot()
        mode_snapshot = self.system_mode()
        recovery = self.recovery_view()
        market = self.runtime.market_gateway.status()
        account = self.runtime.account_service.status()
        execution = self.runtime.execution_adapter.readiness()
        latest_reconciliation = self._latest_scoped_reconciliation()
        latest_portfolio = self._latest_scoped_snapshot()
        blockers = self.blockers()
        account_baseline = self.latest_account_baseline()
        reconciliation_component = next(
            (component for component in snapshot.components if component.component == "reconciliation"),
            None,
        )
        warnings = [
            {
                "component": component.component,
                "detail": component.detail,
                "blockers": component.blockers,
            }
            for component in snapshot.components
            if component.status == "warn"
        ]
        if self.runtime.kill_switch.halted:
            runtime_state = "halted"
        elif any(item["affects_execution"] for item in blockers):
            runtime_state = "blocked"
        elif warnings:
            runtime_state = "degraded"
        else:
            runtime_state = "healthy"
        self._persist_blocker_snapshot(
            source="system_health",
            runtime_state=runtime_state,
            mode_snapshot=mode_snapshot,
            blockers=blockers,
        )
        return {
            "overall_status": snapshot.status,
            "runtime_state": runtime_state,
            "operating_state": snapshot.operating_state,
            "mode": snapshot.mode,
            "runtime_profile": self.runtime.runtime_profile.to_dict(),
            "environment_capabilities": self.runtime.environment_capabilities.to_dict(),
            "policy_profile": self.runtime.policy_profile.to_dict(),
            "recovery_policy": self.runtime.recovery_policy.to_dict(),
            "profile_control": self.runtime_profile_snapshot(),
            "halted": self.runtime.kill_switch.halted,
            "blockers": blockers,
            "warnings": warnings,
            "execution_blocked": mode_snapshot["execution_blocked"],
            "submit_blocked": mode_snapshot["submit_blocked"],
            "submit_blocked_reasons": mode_snapshot["submit_blocked_reasons"],
            "subsystems": {
                "market_data": market,
                "account_state": account,
                "execution_adapter": execution,
                "reconciliation": {
                    "ready": reconciliation_component.status == "ok" if reconciliation_component is not None else False,
                    "fresh": reconciliation_component.fresh if reconciliation_component is not None else False,
                    "last_update_ts": (
                        reconciliation_component.last_update_ts
                        if reconciliation_component is not None
                        else (latest_reconciliation.as_of_ts if latest_reconciliation else None)
                    ),
                    "severity": latest_reconciliation.severity if latest_reconciliation else None,
                    "halt_required": latest_reconciliation.halt_required if latest_reconciliation else False,
                    "blockers": (
                        list(reconciliation_component.blockers)
                        if reconciliation_component is not None
                        else []
                    ),
                },
                "storage": {
                    "ready": True,
                    "fresh": True,
                    "detail": self.runtime.settings.storage_mode,
                },
                "audit_replay": {
                    "ready": True,
                    "fresh": bool(self.runtime.replay_validation_history),
                    "audit_record_count": self.runtime.audit_repo.count(),
                    "last_replay_validation": (
                        self.runtime.replay_validation_history[-1]
                        if self.runtime.replay_validation_history
                        else None
                    ),
                },
            },
            "freshness": {
                "market_fresh": market.get("fresh", False),
                "account_fresh": account.get("fresh", False),
                "reconciliation_fresh": (
                    reconciliation_component.fresh if reconciliation_component is not None else False
                ),
            },
            "last_success_timestamps": {
                "market": market.get("last_update_ts"),
                "account": account.get("last_update_ts"),
                "portfolio": latest_portfolio.snapshot_ts if latest_portfolio else None,
                "reconciliation": latest_reconciliation.as_of_ts if latest_reconciliation else None,
            },
            "recovery": recovery,
            "recovery_state": recovery["recovery_state"],
            "review_required": recovery["review_required"],
            "rebaseline_available": recovery["rebaseline_available"],
            "account_baseline": account_baseline,
            "mode_contract": mode_snapshot,
        }

    def system_runtime(self) -> dict[str, Any]:
        latest_decision = self.runtime.event_store.latest(topics.DECISION_CONTEXTS)
        latest_fill = self.latest_fill()
        latest_reconciliation = self._latest_scoped_reconciliation()
        account_baseline = self.latest_account_baseline()
        recovery = self.recovery_view()
        now = utc_now()
        return {
            "runtime_profile": self.runtime.runtime_profile.to_dict(),
            "environment_capabilities": self.runtime.environment_capabilities.to_dict(),
            "policy_profile": self.runtime.policy_profile.to_dict(),
            "recovery_policy": self.runtime.recovery_policy.to_dict(),
            "profile_source": self.runtime.runtime_profile_resolution.profile_source,
            "runtime_profile_control": self.runtime_profile_snapshot(),
            "symbols": [self.runtime.settings.default_symbol],
            "enabled_timeframes": list(self.runtime.settings.enabled_decision_timeframes),
            "decision_cadence": {
                "decision_min_interval_seconds_15m": self.runtime.settings.decision_min_interval_seconds_15m,
                "decision_min_interval_seconds_1h": self.runtime.settings.decision_min_interval_seconds_1h,
                "decision_min_price_move_bps": self.runtime.settings.decision_min_price_move_bps,
                "decision_min_momentum_delta": self.runtime.settings.decision_min_momentum_delta,
                "max_decisions_per_minute": self.runtime.settings.max_decisions_per_minute,
            },
            "storage_mode": self.runtime.settings.storage_mode,
            "operator_auth_enabled": self.runtime.settings.operator_auth_enabled,
            "operator_auth": {
                "auth_enabled": self.runtime.settings.operator_auth_enabled,
                "session_enabled": self.runtime.settings.operator_session_configured,
                "database_backed": self.runtime.database_runtime is not None,
                "stored_user_count": self.runtime.operator_repo.count() if hasattr(self.runtime, "operator_repo") else 0,
                "api_key_compatibility_enabled": bool(
                    self.runtime.settings.operator_read_api_key or self.runtime.settings.operator_write_api_key
                ),
                "unsafe_write_without_auth": self.runtime.settings.operator_unsafe_write_without_auth,
            },
            "startup_timestamp": self.runtime.started_at,
            "uptime_seconds": max((now - self.runtime.started_at).total_seconds(), 0.0),
            "last_decision_timestamp": latest_decision.event_timestamp if latest_decision else None,
            "last_fill_timestamp": latest_fill.ingestion_timestamp if latest_fill else None,
            "last_reconciliation_timestamp": latest_reconciliation.as_of_ts if latest_reconciliation else None,
            "recovery": {
                "recovery_state": recovery["recovery_state"],
                "review_required": recovery["review_required"],
                "rebaseline_available": recovery["rebaseline_available"],
                "resume_eligible": recovery["resume_eligible"],
                "safe_to_trade": recovery["safe_to_trade"],
            },
            "baseline_takeover": {
                "status": self.runtime.recovery_status.baseline_status,
                "baseline_imported": self.runtime.recovery_status.baseline_imported,
                "baseline_imported_at": self.runtime.recovery_status.baseline_imported_at,
                "baseline_source": self.runtime.recovery_status.baseline_source,
                "baseline_kind": account_baseline.get("baseline_kind") if account_baseline is not None else None,
                "requires_operator_review": self.runtime.recovery_status.baseline_requires_operator_review,
                "safe_for_automatic_continuation": self.runtime.recovery_status.baseline_safe_for_automatic_continuation,
                "balance_count": self.runtime.recovery_status.baseline_balance_count,
                "position_count": self.runtime.recovery_status.baseline_position_count,
                "open_order_count": self.runtime.recovery_status.baseline_open_order_count,
                "fill_count": self.runtime.recovery_status.baseline_fill_count,
                "event_ref": self.runtime.recovery_status.baseline_event_ref,
                "last_rebaseline_event_ref": self.runtime.recovery_status.last_rebaseline_event_ref,
                "last_rebaseline_at": self.runtime.recovery_status.last_rebaseline_at,
                "snapshot": account_baseline,
            },
        }

    def decision_view(self, decision_id: str) -> dict[str, Any]:
        audit = self.runtime.audit_repo.get(decision_id)
        if audit is None:
            raise KeyError(f"decision_not_found:{decision_id}")
        decision_context = self.payload_by_ref(audit.decision_context_ref)
        health_snapshot = None
        if decision_context is not None:
            health_snapshot = self.payload_by_ref(decision_context.get("health_snapshot_ref"))
        order_intents = self.payloads_by_refs(audit.order_intent_refs)
        order_updates = self.payloads_by_refs(audit.order_state_refs)
        fills = self.payloads_by_refs(audit.fill_event_refs)
        reconciliations = self.payloads_by_refs(audit.reconciliation_refs)
        ai_visible = self._ai_history_visible()
        ai_decision_brief = self.payload_by_ref(audit.ai_decision_brief_ref) if ai_visible else None
        ai_assessment = self.payload_by_ref(audit.ai_market_assessment_ref) if ai_visible else None
        ai_takeover_decision = self.payload_by_ref(audit.ai_takeover_decision_ref) if ai_visible else None
        baseline_assessment = self.payload_by_ref(audit.baseline_assessment_ref)
        position_target = self.payload_by_ref(audit.position_target_ref)
        execution_plan = self.payload_by_ref(audit.execution_plan_ref)
        strategy_execution_health = self.strategy_execution_health(
            decision_context.get("symbol") if decision_context is not None else None
        )
        return {
            "decision_id": decision_id,
            "health_snapshot": health_snapshot,
            "decision_context": decision_context,
            "baseline_assessment": baseline_assessment,
            "ai_decision_brief": ai_decision_brief,
            "ai_assessment": ai_assessment,
            "ai_takeover_decision": ai_takeover_decision,
            "ai_shadow_decisions": self.payloads_by_refs(audit.ai_shadow_decision_refs) if ai_visible else [],
            "ai_shadow_evaluations": self.payloads_by_refs(audit.ai_shadow_evaluation_refs) if ai_visible else [],
            "position_target": position_target,
            "policy_decision": self.payload_by_ref(audit.policy_decision_ref),
            "risk_decision": self.payload_by_ref(audit.risk_decision_ref),
            "execution_plan": execution_plan,
            "audit": audit.model_dump(mode="json"),
            "latest_order_intent": order_intents[-1] if order_intents else None,
            "latest_order_update": order_updates[-1] if order_updates else None,
            "latest_fill_event": fills[-1] if fills else None,
            "latest_reconciliation": reconciliations[-1] if reconciliations else None,
            "order_intents": order_intents,
            "order_updates": order_updates,
            "fills": fills,
            "portfolio_snapshot": self.payload_by_ref(audit.portfolio_delta_ref),
            "reconciliations": reconciliations,
            "strategy_execution_health": strategy_execution_health,
            "ai_decision_audit": self._ai_decision_audit(
                audit=audit,
                decision_context=decision_context,
                ai_decision_brief=ai_decision_brief,
                baseline_assessment=baseline_assessment,
                ai_assessment=ai_assessment,
                ai_takeover_decision=ai_takeover_decision,
                position_target=position_target,
                strategy_execution_health=strategy_execution_health,
            ),
            "ai_economic_actionability": self._ai_economic_actionability(
                ai_assessment=ai_assessment,
                ai_takeover_decision=ai_takeover_decision,
                position_target=position_target,
                ai_decision_brief=ai_decision_brief,
                strategy_execution_health=strategy_execution_health,
            ),
            "ai_execution_suggestion": self._ai_execution_suggestion_summary(
                ai_assessment=ai_assessment,
                execution_plan=execution_plan,
                latest_order_intent=order_intents[-1] if order_intents else None,
            ),
        }

    def recent_decisions(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = self.runtime.audit_repo.recent(limit=limit + offset)
        paged_rows = rows[offset : offset + limit]
        payloads: list[dict[str, Any]] = []
        for record in paged_rows:
            context = self.payload_by_ref(record.decision_context_ref)
            target = self.payload_by_ref(record.position_target_ref)
            policy = self.payload_by_ref(record.policy_decision_ref)
            risk = self.payload_by_ref(record.risk_decision_ref)
            payloads.append(
                {
                    "decision_id": record.decision_id,
                    "symbol": context.get("symbol") if context else None,
                    "timeframe": context.get("timeframe") if context else None,
                    "decision_time": context.get("as_of_ts") if context else None,
                    "product_type": (
                        target.get("product_type")
                        if target
                        else (context.get("product_type") if context else None)
                    ),
                    "margin_mode": (
                        target.get("margin_mode")
                        if target
                        else (context.get("margin_mode") if context else None)
                    ),
                    "position_intent": target.get("position_intent") if target else None,
                    "current_position_qty": target.get("current_position_qty") if target else None,
                    "target_position_qty": target.get("target_position_qty") if target else None,
                    "delta_position_qty": target.get("delta_position_qty") if target else None,
                    "target_delta_qty": target.get("delta_position_qty") if target else None,
                    "guardrail_flags": target.get("guardrail_flags") if target else [],
                    "expected_net_edge_bps": target.get("expected_net_edge_bps") if target else None,
                    "policy_result": policy.get("execution_allowed") if policy else None,
                    "risk_result": risk.get("approved") if risk else None,
                    "execution_result": {
                        "order_count": len(record.order_intent_refs),
                        "fill_count": len(record.fill_event_refs),
                        "reconciled": bool(record.reconciliation_refs),
                    },
                }
            )
        total_available = self.runtime.audit_repo.count()
        return {
            "decisions": payloads,
            "limit": limit,
            "offset": offset,
            "total_available": total_available,
            "has_more": offset + len(payloads) < total_available,
        }

    def latest_decision(self) -> dict[str, Any]:
        decision_id = self.latest_decision_id()
        if decision_id is None:
            return {
                "decision_id": None,
                "decision_context": None,
                "baseline_assessment": None,
                "ai_assessment": None,
                "position_target": None,
                "policy_decision": None,
                "risk_decision": None,
                "execution_plan": None,
                "audit": None,
                "order_intents": [],
                "order_updates": [],
                "fills": [],
                "portfolio_snapshot": None,
                "reconciliations": [],
                "summary": None,
                "strategy_execution_health": self.strategy_execution_health(),
            }
        detail = self.decision_view(decision_id)
        detail["summary"] = next((item for item in self.recent_decisions(limit=1)["decisions"] if item["decision_id"] == decision_id), None)
        detail["strategy_execution_health"] = self.strategy_execution_health(
            detail["decision_context"]["symbol"] if detail["decision_context"] else None
        )
        return detail

    def latest_risk(self) -> dict[str, Any]:
        return self._latest_topic_summary(topics.RISK_DECISIONS)

    def recent_risks(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = list(reversed(self.runtime.event_store.by_topic(topics.RISK_DECISIONS)))
        return self._paginate_rows(rows, limit=limit, offset=offset, key="risks", serializer=lambda item: item.payload)

    def latest_policy(self) -> dict[str, Any]:
        return self._latest_topic_summary(topics.POLICY_DECISIONS)

    def recent_policies(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = list(reversed(self.runtime.event_store.by_topic(topics.POLICY_DECISIONS)))
        return self._paginate_rows(rows, limit=limit, offset=offset, key="policies", serializer=lambda item: item.payload)

    def blockers(self) -> list[dict[str, Any]]:
        snapshot = self.runtime.health_service.snapshot()
        recovery = self.recovery_view()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for blocker in snapshot.blockers:
            subsystem = "system"
            if blocker.startswith("market_"):
                subsystem = "market_data"
            elif blocker.startswith("account_"):
                subsystem = "account_state"
            elif blocker.startswith("reconciliation_"):
                subsystem = "reconciliation"
            elif blocker.startswith("kill_switch"):
                subsystem = "execution_control"
            elif blocker.startswith("guarded_") or blocker.startswith("live_submit"):
                subsystem = "execution_adapter"
            rows.append(self._blocker_entry(blocker=blocker, subsystem=subsystem))
            seen.add(blocker)
        if self.runtime.kill_switch.halted and "kill_switch_active" not in seen:
            rows.insert(0, self._blocker_entry(blocker="kill_switch_active", subsystem="execution_control"))
            seen.add("kill_switch_active")
        for blocker in self.system_mode()["submit_blocked_reasons"]:
            if blocker in seen:
                continue
            subsystem = "execution_adapter"
            if blocker in {"local_demo_no_exchange_submission", "real_market_paper_uses_local_paper_execution"}:
                subsystem = "mode"
            rows.append(self._blocker_entry(blocker=blocker, subsystem=subsystem, submit_only=True))
            seen.add(blocker)
        for blocker in recovery["resume_blocked_reasons"]:
            if blocker in seen:
                continue
            subsystem = "recovery"
            if blocker.startswith("reconciliation_"):
                subsystem = "reconciliation"
            rows.append(self._blocker_entry(blocker=blocker, subsystem=subsystem))
            seen.add(blocker)
        return rows

    def blocker_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = [item.payload for item in reversed(self.runtime.event_store.by_topic(topics.BLOCKER_SNAPSHOTS))]
        return self._paginate_rows(rows, limit=limit, offset=offset, key="history")

    def metrics(self) -> dict[str, Any]:
        snapshot = self._latest_scoped_snapshot()
        metrics = self.runtime.metrics.snapshot()
        fills = self._scoped_fills()
        snapshot_events = [
            event
            for event in self.runtime.event_store.by_topic_scoped(
                topics.PORTFOLIO_SNAPSHOTS,
                scope=self.state_scope,
            )
        ]
        snapshot_fill_ids = {
            event.payload.get("source_fill_id")
            for event in snapshot_events
            if isinstance(event.payload, dict) and event.payload.get("source_fill_id")
        }
        reconciliation_refs = {
            report.portfolio_snapshot_ref
            for report in self.runtime.reconciliation_repo.history_for_scope(scope=self.state_scope)
        }
        return {
            "decision_cycle_count": metrics.get("decision_cycles", 0),
            "order_intent_count": metrics.get("order_intents_generated", 0),
            "fill_count": len(fills),
            "rejection_count": len(
                order_states_for_scope(
                    self.runtime.execution_repo,
                    self.state_scope,
                    statuses=("FAILED", "REJECTED", "BLOCKED"),
                    limit=200,
                )
            ),
            "reconciliation_mismatch_count": metrics.get("reconciliation_mismatches", 0),
            "processing_failure_count": metrics.get("processing_failures", 0),
            "portfolio_snapshot_repair_count": metrics.get("portfolio_snapshot_repairs", 0),
            "current_open_order_count": len(self._scoped_open_order_states()),
            "portfolio_snapshot_count": len(snapshot_events),
            "fill_without_snapshot_count": sum(1 for fill in fills if fill.fill_id not in snapshot_fill_ids),
            "snapshot_without_reconciliation_count": sum(
                1 for event in snapshot_events if event.event_id not in reconciliation_refs
            ),
            "recent_execution_errors": self.execution_errors()["errors"][:10],
            "exposure_summary": None if snapshot is None else {
                "gross_exposure": snapshot.gross_exposure,
                "net_exposure": snapshot.net_exposure,
                "total_equity": snapshot.total_equity,
            },
            "strategy_execution_health": self.strategy_execution_health(),
        }

    def portfolio_latest(self) -> dict[str, Any]:
        snapshot = self._latest_scoped_snapshot()
        return {
            "portfolio": snapshot.model_dump(mode="json") if snapshot is not None else None,
            "latest_update_timestamp": snapshot.snapshot_ts if snapshot is not None else None,
        }

    def portfolio_history(self, *, limit: int = 20) -> dict[str, Any]:
        history = snapshots_for_scope(self.runtime.portfolio_repo, self.state_scope, limit=limit)
        return {
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in history],
            "total_available": len(snapshots_for_scope(self.runtime.portfolio_repo, self.state_scope)),
        }

    def balances(self) -> dict[str, Any]:
        snapshot = self._latest_scoped_snapshot()
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_balances": snapshot.balances if snapshot is not None else {},
            "exchange_balances": [item.model_dump(mode="json") for item in exchange.balances] if exchange is not None else [],
        }

    def positions(self) -> dict[str, Any]:
        snapshot = self._latest_scoped_snapshot()
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_positions": [item.model_dump(mode="json") for item in snapshot.positions] if snapshot is not None else [],
            "exchange_positions": [item.model_dump(mode="json") for item in exchange.positions] if exchange is not None else [],
        }

    def account_state(self) -> dict[str, Any]:
        status = self.runtime.account_service.status()
        recovery = self.recovery_view()
        return {
            "backend": self.runtime.settings.account_backend,
            "read_enabled": self.runtime.settings.account_read_enabled,
            "last_refresh_timestamp": status.get("last_update_ts"),
            "fresh": status.get("fresh", False),
            "connected": status.get("connected", False),
            "ready": status.get("ready", False),
            "last_error": status.get("last_error"),
            "private_ws_connected": status.get("private_ws_connected", False),
            "private_ws_last_message_ts": status.get("private_ws_last_message_ts"),
            "private_ws_last_error": status.get("private_ws_last_error"),
            "private_ws_fresh": status.get("private_ws_fresh", False),
            "maker_fee_rate": status.get("maker_fee_rate"),
            "taker_fee_rate": status.get("taker_fee_rate"),
            "fee_rates_source": status.get("fee_rates_source"),
            "recent_bills_count": status.get("recent_bills_count", 0),
            "last_bills_error": status.get("last_bills_error"),
            "blockers": status.get("blockers", []),
            "current_blocking_reason": next(iter(status.get("blockers", [])), None),
            "detail": status.get("detail"),
            "recovery": {
                "recovery_state": recovery["recovery_state"],
                "review_required": recovery["review_required"],
                "rebaseline_available": recovery["rebaseline_available"],
                "resume_eligible": recovery["resume_eligible"],
                "safe_to_trade": recovery["safe_to_trade"],
            },
            "baseline_takeover": {
                "status": self.runtime.recovery_status.baseline_status,
                "baseline_imported": self.runtime.recovery_status.baseline_imported,
                "baseline_imported_at": self.runtime.recovery_status.baseline_imported_at,
                "baseline_source": self.runtime.recovery_status.baseline_source,
                "requires_operator_review": self.runtime.recovery_status.baseline_requires_operator_review,
                "safe_for_automatic_continuation": self.runtime.recovery_status.baseline_safe_for_automatic_continuation,
                "balance_count": self.runtime.recovery_status.baseline_balance_count,
                "position_count": self.runtime.recovery_status.baseline_position_count,
                "open_order_count": self.runtime.recovery_status.baseline_open_order_count,
                "fill_count": self.runtime.recovery_status.baseline_fill_count,
                "event_ref": self.runtime.recovery_status.baseline_event_ref,
            },
        }

    async def account_recent_bills(self, *, limit: int = 50) -> dict[str, Any]:
        rows = await self.runtime.account_service.recent_bills(
            symbol=self.runtime.settings.default_symbol,
            limit=limit,
        )
        return {
            "bills": rows[:limit],
            "total_available": len(rows),
            "limit": limit,
            "latest_bill": rows[0] if rows else None,
        }

    def account_open_orders(self) -> dict[str, Any]:
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_open_orders": [order.model_dump(mode="json") for order in self._scoped_open_order_states()],
            "exchange_open_orders": [order.model_dump(mode="json") for order in exchange.open_orders] if exchange is not None else [],
        }

    def account_recent_fills(self) -> dict[str, Any]:
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_fills": [fill.model_dump(mode="json") for fill in self.recent_fills(limit=50)],
            "exchange_fills": [fill.model_dump(mode="json") for fill in exchange.fills[:50]] if exchange is not None else [],
        }

    def orders_open(self) -> dict[str, Any]:
        return self.account_open_orders()

    def orders_recent(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        all_orders = sorted(
            order_states_for_scope(self.runtime.execution_repo, self.state_scope),
            key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id),
            reverse=True,
        )
        orders = all_orders[offset : offset + limit]
        return {
            "orders": [order.model_dump(mode="json") for order in orders],
            "limit": limit,
            "offset": offset,
            "total_available": len(all_orders),
            "has_more": offset + len(orders) < len(all_orders),
        }

    def order_detail(self, client_order_id: str) -> dict[str, Any]:
        order = next(
            (item for item in self._scoped_order_states() if item.client_order_id == client_order_id),
            None,
        )
        if order is None:
            raise KeyError(f"order_not_found:{client_order_id}")
        fills = self._scoped_fills_for_order(client_order_id)
        return {
            "order": order.model_dump(mode="json"),
            "fills": [fill.model_dump(mode="json") for fill in fills],
            "stuck_submission_resolution": self._stuck_submission_resolution(
                order=order,
                fills=fills,
                exchange_snapshot=self.runtime.account_service.latest_snapshot(),
            ),
        }

    def fills_recent(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        all_fills = self.recent_fills(limit=limit + offset)
        fills = all_fills[offset : offset + limit]
        total_available = len(self._scoped_fills())
        return {
            "fills": [fill.model_dump(mode="json") for fill in fills],
            "limit": limit,
            "offset": offset,
            "total_available": total_available,
            "has_more": offset + len(fills) < total_available,
        }

    def fill_detail(self, fill_id: str) -> dict[str, Any]:
        fill = next((item for item in self._scoped_fills() if item.fill_id == fill_id), None)
        if fill is None:
            raise KeyError(f"fill_not_found:{fill_id}")
        return {"fill": fill.model_dump(mode="json")}

    def execution_latest(self) -> dict[str, Any]:
        latest_order = self.latest_order()
        latest_fill = self.latest_fill()
        latest_reconciliation = self._latest_scoped_reconciliation()
        recovery = self.recovery_view()
        return {
            "mode": self.system_mode(),
            "execution": self.runtime.execution_adapter.readiness(),
            "latest_order": latest_order.model_dump(mode="json") if latest_order is not None else None,
            "latest_fill": latest_fill.model_dump(mode="json") if latest_fill is not None else None,
            "latest_reconciliation": latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None,
            "recent_failures": self.execution_errors()["errors"],
            "recovery": recovery,
        }

    def execution_errors(self) -> dict[str, Any]:
        persisted = [
            item.payload
            for item in self.runtime.event_store.recent_by_topic(topics.EXECUTION_ERROR_SUMMARIES, limit=20)
            if self._is_current_runtime_timestamp(item.payload.get("observed_at"))
        ]
        if persisted:
            return {"errors": persisted}
        errors = []
        for order in order_states_for_scope(
            self.runtime.execution_repo,
            self.state_scope,
            statuses=("FAILED", "REJECTED", "BLOCKED"),
            limit=20,
        ):
            if not self._is_current_runtime_timestamp(order.last_update_ts or order.created_at):
                continue
            errors.append(
                {
                    "timestamp": order.last_update_ts or order.created_at,
                    "subsystem": "execution_engine",
                    "severity": "error" if order.status == "FAILED" else "warning",
                    "message": order.execution_error or order.cancel_reason or order.status,
                    "decision_id": order.decision_id,
                    "order_id": order.client_order_id,
                    "status": order.status,
                }
            )
        return {"errors": errors}

    def reconciliation_latest(self) -> dict[str, Any]:
        report = self._latest_scoped_reconciliation()
        latest_validation = self.runtime.event_store.latest(topics.RECONCILIATION_VALIDATIONS)
        return {
            "reconciliation": report.model_dump(mode="json") if report is not None else None,
            "mismatch_summary": self._reconciliation_mismatch_summary(report),
            "exchange_bills_summary": self._exchange_bills_summary(),
            "latest_validation": latest_validation.payload if latest_validation is not None else None,
            "recovery": self.recovery_view(),
        }

    def reconciliation_recent(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        history = list(reversed(reconciliation_reports_for_scope(self.runtime.reconciliation_repo, self.state_scope)))
        return self._paginate_rows(
            history,
            limit=limit,
            offset=offset,
            key="reconciliations",
            serializer=lambda report: report.model_dump(mode="json"),
        ) | {"exchange_bills_summary": self._exchange_bills_summary()}

    def reconciliation_mismatches(self, *, limit: int = 20) -> dict[str, Any]:
        reports = [
            report
            for report in reconciliation_reports_for_scope(self.runtime.reconciliation_repo, self.state_scope, limit=limit * 4)
            if report.severity != "CLEAN"
        ][-limit:]
        return {
            "mismatches": [self._reconciliation_mismatch_summary(report) for report in reports],
            "exchange_bills_summary": self._exchange_bills_summary(),
        }

    def reconciliation_detail(self, reconciliation_id: str) -> dict[str, Any]:
        report = next(
            (
                item
                for item in reconciliation_reports_for_scope(self.runtime.reconciliation_repo, self.state_scope)
                if item.reconciliation_id == reconciliation_id
            ),
            None,
        )
        if report is None:
            raise KeyError(f"reconciliation_not_found:{reconciliation_id}")
        return {
            "reconciliation": report.model_dump(mode="json"),
            "mismatch_summary": self._reconciliation_mismatch_summary(report),
            "exchange_bills_summary": self._exchange_bills_summary(),
            "exchange_bills_explanations": report.exchange_bills_explanations,
        }

    def audit_latest(self) -> dict[str, Any]:
        latest = max(self.runtime.audit_repo.all(), key=lambda item: item.created_at, default=None)
        return {"audit": latest.model_dump(mode="json") if latest is not None else None}

    def audit_detail(self, decision_id: str) -> dict[str, Any]:
        detail = self.decision_view(decision_id)
        context = detail["decision_context"] or {}
        return {
            "audit": detail["audit"],
            "history_length": len(self.runtime.audit_repo.history(decision_id)),
            "baseline_switches": self._baseline_switch_history(
                as_of_ts=context.get("as_of_ts"),
                limit=10,
            ),
            "linked_events": {
                "decision_context": detail["decision_context"],
                "baseline_assessment": detail["baseline_assessment"],
                "ai_decision_brief": detail["ai_decision_brief"],
                "ai_assessment": detail["ai_assessment"],
                "ai_takeover_decision": detail["ai_takeover_decision"],
                "ai_shadow_decisions": detail["ai_shadow_decisions"],
                "ai_shadow_evaluations": detail["ai_shadow_evaluations"],
                "position_target": detail["position_target"],
                "policy_decision": detail["policy_decision"],
                "risk_decision": detail["risk_decision"],
                "execution_plan": detail["execution_plan"],
                "order_intents": detail["order_intents"],
                "order_updates": detail["order_updates"],
                "fills": detail["fills"],
                "portfolio_snapshot": detail["portfolio_snapshot"],
                "reconciliations": detail["reconciliations"],
            },
        }

    def replay_status(self) -> dict[str, Any]:
        persisted = self.runtime.event_store.recent_by_topic(topics.REPLAY_VALIDATIONS, limit=10)
        latest = persisted[-1].payload if persisted else (
            self.runtime.replay_validation_history[-1] if self.runtime.replay_validation_history else None
        )
        recent = [item.payload for item in persisted] if persisted else list(self.runtime.replay_validation_history[-10:])
        return {
            "supported": True,
            "healthy": latest is None or latest["divergence_count"] == 0,
            "last_validation": latest,
            "recent_validations": recent,
            "baseline_switches": self._baseline_switch_history(limit=10),
        }

    def replay_validate(self, *, decision_id: str) -> dict[str, Any]:
        engine = ReplayEngine(
            event_store=self.runtime.event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=self.runtime.settings.initial_usdt_balance,
                snapshot_builder=self.runtime.portfolio_service.snapshot_builder,
            ),
            audit_repo=self.runtime.audit_repo,
            portfolio_repo=self.runtime.portfolio_repo,
            scope=self.state_scope,
        )
        result = engine.replay(decision_id=decision_id)
        detail = self.decision_view(decision_id)
        baseline = detail.get("baseline_assessment") or {}
        context = detail.get("decision_context") or {}
        profile_state = self.strategy_profiles.snapshot().get("activation", {})
        summary = self._replay_summary(
            result,
            symbol=context.get("symbol"),
            regime=baseline.get("regime"),
            active_profile_id=profile_state.get("active_profile_id"),
        )
        self._append_event(
            topic=topics.REPLAY_VALIDATIONS,
            key=decision_id or "all",
            payload_model=ReplayValidationSummary(**summary),
        )
        self.runtime.replay_validation_history.append(summary)
        self.runtime.replay_validation_history[:] = self.runtime.replay_validation_history[-20:]
        return summary

    def replay_recent_validations(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        persisted = self.runtime.event_store.by_topic(topics.REPLAY_VALIDATIONS)
        if persisted:
            rows = [item.payload for item in reversed(persisted)]
        else:
            rows = list(reversed(self.runtime.replay_validation_history))
        return self._paginate_rows(rows, limit=limit, offset=offset, key="validations")

    async def validate_reconciliation(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        report = await self.runtime.reconciliation_service.validate_now(reason=reason)
        summary = ReconciliationValidationSummary(
            trigger=reason,
            reconciliation_id=report.reconciliation_id,
            decision_id=report.decision_id,
            severity=report.severity,
            halt_required=report.halt_required,
            exchange_comparison_enabled=report.exchange_comparison_enabled,
            mismatch_reasons=list(report.mismatch_reasons),
            safety_impacts=list(report.safety_impacts),
            validated_at=utc_now(),
        )
        self._append_event(
            topic=topics.RECONCILIATION_VALIDATIONS,
            key=report.decision_id or "portfolio",
            payload_model=summary,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="reconciliation_validate",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="completed",
                decision_id=report.decision_id,
                recovery_state_before=self.recovery_view()["recovery_state"],
                recovery_state_after=(
                    "resume_blocked"
                    if report.halt_required
                    else "review_required"
                    if report.review_required
                    else self.recovery_view()["recovery_state"]
                ),
                reconciliation_id=report.reconciliation_id,
            ),
        )
        self._update_recovery_status_for_report(report)
        self._persist_blocker_snapshot(
            source="reconciliation_validate",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "reconciliation": report.model_dump(mode="json"),
            "validation": summary.model_dump(mode="json"),
        }

    async def cancel_order(
        self,
        *,
        client_order_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        recovery_before = self.recovery_view()["recovery_state"]
        state = await self.runtime.order_manager.cancel_order(client_order_id)
        self._invalidate_cache()
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="cancel_order",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status=state.status,
                decision_id=state.decision_id,
                order_id=state.client_order_id,
                recovery_state_before=recovery_before,
                recovery_state_after=self.recovery_view()["recovery_state"],
                details={"final_order_status": state.status},
            ),
        )
        return {"order": state.model_dump(mode="json")}

    async def resolve_stuck_submission(
        self,
        *,
        client_order_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        order = next(
            (item for item in self._scoped_order_states() if item.client_order_id == client_order_id),
            None,
        )
        if order is None:
            raise KeyError(f"order_not_found:{client_order_id}")

        fills = self._scoped_fills_for_order(client_order_id)
        recovery_before = self.recovery_view()["recovery_state"]
        exchange_snapshot = await self._refresh_exchange_snapshot_for_resolution()
        resolution = self._stuck_submission_resolution(
            order=order,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
        )
        if not resolution["eligible"]:
            raise ValueError(f"stuck_submission_resolution_blocked:{resolution['reason_code']}")

        now = utc_now()
        resolved_state = order.model_copy(
            update={
                "status": "FAILED",
                "last_update_ts": now,
                "execution_error": "operator_resolved_stuck_submission_after_restart",
                "cancel_reason": reason,
            }
        )
        persisted = self.runtime.execution_repo.save_order_state(resolved_state)
        if self.runtime.audit_repo.get(persisted.decision_id) is not None:
            await publish_model(
                bus=self.runtime.bus,
                topic=topics.ORDER_UPDATES,
                key=persisted.symbol,
                payload_model=persisted,
                source_component="operator_api",
            )
        else:
            self._append_event(
                topic=topics.ORDER_UPDATES,
                key=persisted.symbol,
                payload_model=persisted,
            )
        await publish_model(
            bus=self.runtime.bus,
            topic=topics.EXECUTION_ERROR_SUMMARIES,
            key=persisted.symbol,
            payload_model=ExecutionErrorSummary(
                subsystem="operator_recovery",
                severity="warning",
                message="Operator resolved a stuck pre-restart submission as FAILED.",
                decision_id=persisted.decision_id,
                intent_id=persisted.intent_id,
                order_id=persisted.client_order_id,
                status=persisted.status,
                observed_at=now,
            ),
            source_component="operator_api",
        )
        report = await self.runtime.reconciliation_service.validate_now(
            reason=f"resolve_stuck_submission:{client_order_id}"
        )
        self._update_recovery_status_for_report(report)
        self._invalidate_cache()
        recovery_after = self.recovery_view()["recovery_state"]
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="resolve_stuck_submission",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="resolved_as_failed",
                decision_id=persisted.decision_id,
                order_id=persisted.client_order_id,
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                reconciliation_id=report.reconciliation_id,
                details={
                    "previous_order_status": order.status,
                    "final_order_status": persisted.status,
                    "resolution": resolution,
                },
            ),
        )
        self._persist_blocker_snapshot(
            source="resolve_stuck_submission",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        log_event(
            self.logger,
            "resolve_stuck_submission",
            level="warning",
            order_id=persisted.client_order_id,
            previous_status=order.status,
            final_status=persisted.status,
            reason=reason,
        )
        return {
            "order": persisted.model_dump(mode="json"),
            "resolution": resolution,
            "reconciliation": report.model_dump(mode="json"),
            "recovery": self.recovery_view(),
        }

    async def rebaseline(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        if not self.runtime.settings.account_read_enabled or self.runtime.settings.account_backend != "okx":
            raise ValueError("rebaseline_requires_okx_account_read")
        if not self.runtime.recovery_policy.operator_rebaseline_supported:
            raise ValueError("rebaseline_not_supported_for_runtime_profile")

        recovery_before = self.recovery_view()["recovery_state"]
        previous_baseline_event = latest_topic_event_for_scope(
            self.runtime.event_store,
            topics.ACCOUNT_BASELINES,
            self.state_scope,
        )
        previous_baseline_ref = previous_baseline_event.event_id if previous_baseline_event is not None else None

        self.runtime.kill_switch.halt(reason="operator_rebaseline_pending")
        pending_status = self.runtime.recovery_status.model_copy(
            update={"recovery_state": "rebaseline_pending", "recovery_action": "operator_rebaseline_pending"}
        )
        self.runtime.recovery_status = self.recovery_posture.finalize_status(base_status=pending_status)

        exchange_snapshot = await self.runtime.account_service.refresh(force=True)
        if exchange_snapshot is None:
            raise ValueError("rebaseline_requires_account_snapshot")

        action_record = OperatorActionRecord(
            action="rebaseline",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
            status="completed",
            recovery_state_before=recovery_before,
            recovery_state_after="rebaseline_pending",
        )
        action_envelope = self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=action_record,
        )
        baseline_importer = AccountBaselineImportService(event_store=self.runtime.event_store)
        imported = baseline_importer.rebaseline_snapshot(
            exchange_snapshot=exchange_snapshot,
            portfolio_state=self.runtime.portfolio_service.state,
            product_type=self.state_scope.product_type,
            margin_mode=self.state_scope.margin_mode,
            allowed_symbols=self.state_scope.allowed_symbols,
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=action_envelope.event_id,
            trigger_reason=reason,
        )
        await self.runtime.portfolio_service.bootstrap_snapshot(snapshot_origin="operator_rebaseline")
        report = await self.runtime.reconciliation_service.validate_now(reason="operator_rebaseline")

        recovery_state = (
            "resume_blocked"
            if imported.snapshot.requires_operator_review or report.halt_required
            else "review_required"
            if report.review_required
            else "rebaseline_completed"
        )
        updated_status = self.runtime.recovery_status.model_copy(
            update={
                "status": imported.snapshot.baseline_status,
                "recovery_state": recovery_state,
                "safe_startup": False,
                "recovery_action": (
                    "operator_rebaseline_completed"
                    if recovery_state == "rebaseline_completed"
                    else "operator_rebaseline_requires_review"
                    if recovery_state == "review_required"
                    else "operator_rebaseline_resume_blocked"
                ),
                "baseline_imported": True,
                "baseline_status": imported.snapshot.baseline_status,
                "baseline_imported_at": imported.snapshot.imported_at,
                "baseline_event_ref": imported.event_id,
                "baseline_source": imported.snapshot.account_source,
                "baseline_safe_for_automatic_continuation": imported.snapshot.safe_for_automatic_continuation,
                "baseline_requires_operator_review": imported.snapshot.requires_operator_review,
                "baseline_balance_count": imported.snapshot.balance_count,
                "baseline_position_count": imported.snapshot.position_count,
                "baseline_open_order_count": imported.snapshot.open_order_count,
                "baseline_fill_count": imported.snapshot.fill_count,
                "last_rebaseline_at": imported.snapshot.imported_at,
                "last_rebaseline_event_ref": imported.event_id,
                "last_rebaseline_action_ref": action_envelope.event_id,
                "notes": self.runtime.recovery_status.notes
                + [
                    "operator_rebaseline_confirmed",
                    f"baseline_switch:{previous_baseline_ref or 'none'}->{imported.event_id}",
                ],
            }
        )
        self.runtime.recovery_status = self.recovery_posture.finalize_status(
            base_status=updated_status,
            latest_reconciliation=report,
        )
        self._invalidate_cache()
        resume_eligible = self.runtime.recovery_status.resume_eligible
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=action_record.model_copy(
                update={
                    "status": recovery_state,
                    "recovery_state_after": recovery_state,
                    "baseline_event_ref": imported.event_id,
                    "reconciliation_id": report.reconciliation_id,
                    "details": {
                        "previous_baseline_ref": previous_baseline_ref,
                        "resume_eligible": resume_eligible,
                    },
                }
            ),
        )
        self._persist_blocker_snapshot(
            source="operator_rebaseline",
            runtime_state="halted",
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "status": recovery_state,
            "halted": True,
            "reason": reason,
            "baseline": imported.snapshot.model_dump(mode="json"),
            "baseline_event_ref": imported.event_id,
            "reconciliation": report.model_dump(mode="json"),
            "recovery": self.recovery_view(),
        }

    def halt(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        was_halted = self.runtime.kill_switch.halted
        recovery_before = self.recovery_view()["recovery_state"]
        self.runtime.kill_switch.halt(reason=reason)
        log_event(self.logger, "operator_halt", level="warning", reason=reason, already_halted=was_halted)
        status = "already_halted" if was_halted else "halted"
        updated_status = self.runtime.recovery_status.model_copy(
            update={
                "recovery_state": (
                    "resume_blocked"
                    if self.runtime.recovery_status.recovery_state == "normal_operation"
                    else self.runtime.recovery_status.recovery_state
                ),
                "last_resume_status": None,
                "last_resume_reason": None,
            }
        )
        self.runtime.recovery_status = self.recovery_posture.finalize_status(base_status=updated_status)
        self._invalidate_cache()
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="halt",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status=status,
                recovery_state_before=recovery_before,
                recovery_state_after=self.recovery_view()["recovery_state"],
            ),
        )
        self._persist_blocker_snapshot(
            source="operator_halt",
            runtime_state="halted",
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {"status": status, "halted": True, "reason": reason}

    async def resume(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        was_halted = self.runtime.kill_switch.halted
        recovery_before = self.recovery_view()["recovery_state"]
        if self.runtime.settings.account_backend == "okx" and self.runtime.settings.account_read_enabled:
            await self.runtime.account_service.refresh(force=True)
        report = await self.runtime.reconciliation_service.validate_now(reason=f"resume_check:{reason}")
        self._update_recovery_status_for_report(report)
        resume_check = self.recovery_posture.resume_check(include_kill_switch=False, latest_reconciliation=report)
        runnable = resume_check.runnable
        if runnable:
            self.runtime.kill_switch.resume()
            status = "already_resumed" if not was_halted else "resumed"
            recovery_after = "normal_operation"
            updated_status = self.runtime.recovery_status.model_copy(
                update={
                    "recovery_state": recovery_after,
                    "recovery_action": "operator_resume_completed",
                    "last_resume_status": status,
                    "last_resume_reason": reason,
                    "resume_blocked_reasons": [],
                }
            )
            self.runtime.recovery_status = self.recovery_posture.finalize_status(
                base_status=updated_status,
                latest_reconciliation=report,
            )
        else:
            self.runtime.kill_switch.halt(reason="resume_blocked")
            status = "resume_blocked"
            recovery_after = (
                "review_required"
                if "operator_rebaseline_required" in resume_check.blockers
                else "resume_blocked"
            )
            updated_status = self.runtime.recovery_status.model_copy(
                update={
                    "recovery_state": recovery_after,
                    "recovery_action": "operator_resume_blocked",
                    "last_resume_status": status,
                    "last_resume_reason": reason,
                    "resume_blocked_reasons": list(resume_check.blockers),
                }
            )
            self.runtime.recovery_status = self.recovery_posture.finalize_status(
                base_status=updated_status,
                latest_reconciliation=report,
            )
        self._invalidate_cache()
        mode_state = self.system_mode()
        blockers = self.blockers()
        log_event(
            self.logger,
            "operator_resume",
            level="info",
            reason=reason,
            was_halted=was_halted,
            blockers=[item["blocker"] for item in blockers],
            status=status,
        )
        action_envelope = self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="resume",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status=status,
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                reconciliation_id=report.reconciliation_id,
                details={"runnable": runnable, "blockers": list(resume_check.blockers)},
            ),
        )
        self.runtime.recovery_status = self.runtime.recovery_status.model_copy(
            update={"last_resume_action_ref": action_envelope.event_id}
        )
        self._persist_blocker_snapshot(
            source="operator_resume",
            runtime_state="blocked" if mode_state["execution_blocked"] else "healthy",
            mode_snapshot=mode_state,
            blockers=blockers,
        )
        return {
            "status": status,
            "halted": self.runtime.kill_switch.halted,
            "reason": reason,
            "runnable": runnable,
            "blockers": blockers,
            "recovery": self.recovery_view(),
            "reconciliation": report.model_dump(mode="json"),
        }

    def _latest_topic_summary(self, topic: str) -> dict[str, Any]:
        envelope = self.runtime.event_store.latest(topic)
        if envelope is None:
            return {"decision_id": None, "payload": None}
        decision_id = envelope.payload.get("decision_id")
        return {
            "decision_id": decision_id if isinstance(decision_id, str) else None,
            "payload": self.payload(envelope),
        }

    def _recent_topic_summaries(self, topic: str, *, limit: int) -> list[dict[str, Any]]:
        return [self.payload(item) for item in reversed(self.runtime.event_store.recent_by_topic(topic, limit=limit))]

    @staticmethod
    def _reconciliation_mismatch_summary(report) -> dict[str, Any] | None:
        if report is None:
            return None
        return {
            "reconciliation_id": report.reconciliation_id,
            "severity": report.severity,
            "review_required": report.review_required,
            "halt_required": report.halt_required,
            "mismatch_categories": report.mismatch_categories,
            "mismatch_reasons": report.mismatch_reasons,
            "safety_impacts": report.safety_impacts,
            "recommended_operator_action": report.recommended_operator_action,
            "exchange_comparison_enabled": report.exchange_comparison_enabled,
        }

    def _exchange_bills_summary(self) -> dict[str, Any] | None:
        report = self._latest_scoped_reconciliation()
        if report is not None and report.exchange_bills_summary:
            return report.exchange_bills_summary
        summary_getter = getattr(self.runtime.account_service, "recent_bills_summary", None)
        if not callable(summary_getter):
            return None
        summary = summary_getter()
        return summary if isinstance(summary, dict) else None

    def _replay_summary(
        self,
        result: ReplayResult,
        *,
        symbol: str | None = None,
        regime: str | None = None,
        active_profile_id: str | None = None,
    ) -> dict[str, Any]:
        replayed_event_count = max(result.replayed_event_count, 1)
        portfolio_issue_count = len(result.portfolio_issues)
        decision_chain_issue_count = len(result.decision_chain_issues)
        execution_chain_issue_count = len(result.execution_chain_issues)
        audit_issue_count = len(result.audit_issues)
        baseline_switch_issue_count = len(result.baseline_switch_issues)
        total_issues = (
            portfolio_issue_count
            + decision_chain_issue_count
            + execution_chain_issue_count
            + audit_issue_count
            + baseline_switch_issue_count
        )
        return {
            "validated_at": utc_now(),
            "decision_id": result.selected_decision_id,
            "symbol": symbol,
            "regime": regime,
            "active_profile_id": active_profile_id,
            "product_type": self.runtime.settings.trading_product_type,
            "margin_mode": self.runtime.settings.margin_mode,
            "allowed_symbols": tuple(self.runtime.settings.allowed_symbols),
            "replayed_event_count": result.replayed_event_count,
            "stored_snapshot_count": result.stored_snapshot_count,
            "divergence_count": result.divergence_count,
            "portfolio_issues": result.portfolio_issues,
            "portfolio_issue_count": portfolio_issue_count,
            "decision_chain_issues": result.decision_chain_issues,
            "decision_chain_issue_count": decision_chain_issue_count,
            "execution_chain_issues": result.execution_chain_issues,
            "execution_chain_issue_count": execution_chain_issue_count,
            "audit_issues": result.audit_issues,
            "audit_issue_count": audit_issue_count,
            "baseline_switch_count": result.baseline_switch_count,
            "baseline_switch_issues": result.baseline_switch_issues,
            "baseline_switch_issue_count": baseline_switch_issue_count,
            "divergence_density": round(result.divergence_count / replayed_event_count, 6),
            "chain_health_score": round(max(0.0, 1.0 - (total_issues / replayed_event_count)), 6),
            "healthy": result.divergence_count == 0,
        }

    def _append_event(self, *, topic: str, key: str, payload_model: Any) -> EventEnvelope:
        envelope = build_envelope(
            topic=topic,
            key=key,
            payload_model=payload_model,
            source_component="operator_api",
        )
        self.runtime.event_store.append(envelope)
        return envelope

    def _scoped_fills_for_order(self, client_order_id: str):
        return [
            item
            for item in self.runtime.execution_repo.fills_for_order(client_order_id)
            if item.product_type == self.state_scope.product_type
            and item.margin_mode == self.state_scope.margin_mode
            and self.state_scope.symbol_allowed(item.symbol)
        ]

    async def _refresh_exchange_snapshot_for_resolution(self):
        if self.runtime.settings.account_backend != "okx" or not self.runtime.settings.account_read_enabled:
            return None
        return await self.runtime.account_service.refresh(force=True)

    def _stuck_submission_resolution(
        self,
        *,
        order: OrderState,
        fills: list[Any] | None = None,
        exchange_snapshot=None,
    ) -> dict[str, Any]:
        local_fills = list(fills or [])
        last_update = order.last_update_ts or order.created_at
        runtime_started_at = self._current_runtime_started_at()
        runtime_restarted_after_order = last_update is not None and last_update < runtime_started_at
        latest_reconciliation = self.runtime.reconciliation_repo.latest_for_scope(scope=self.state_scope)
        exchange_order_present: bool | None = None
        exchange_fill_present: bool | None = None
        private_ws_order_present: bool | None = None
        private_ws_fill_present: bool | None = None
        reason_code: str | None = None

        private_order_lookup = getattr(self.runtime.account_service, "latest_private_order_row", None)
        if callable(private_order_lookup):
            private_ws_order_present = (
                private_order_lookup(symbol=order.symbol, order_id=order.exchange_order_id, client_order_id=order.client_order_id)
                is not None
            )
        private_fill_lookup = getattr(self.runtime.account_service, "latest_private_order_fills", None)
        if callable(private_fill_lookup):
            private_ws_fill_present = bool(
                private_fill_lookup(symbol=order.symbol, order_id=order.exchange_order_id, client_order_id=order.client_order_id)
            )

        if order.venue != "OKX":
            reason_code = "venue_not_exchange_coupled"
        elif order.status not in self._STUCK_SUBMISSION_STATUSES:
            reason_code = "order_not_in_pre_submit_state"
        elif order.exchange_order_id is not None:
            reason_code = "exchange_order_id_present"
        elif local_fills:
            reason_code = "local_fills_present"
        elif not runtime_restarted_after_order:
            reason_code = "order_belongs_to_current_runtime"
        elif private_ws_order_present:
            reason_code = "exchange_order_seen_via_private_ws"
        elif private_ws_fill_present:
            reason_code = "exchange_fill_seen_via_private_ws"
        elif self.runtime.settings.account_backend != "okx" or not self.runtime.settings.account_read_enabled:
            reason_code = "exchange_confirmation_unavailable"
        elif exchange_snapshot is None:
            reason_code = "exchange_snapshot_unavailable"
        else:
            exchange_order_present = any(
                item.client_order_id == order.client_order_id
                or (
                    order.exchange_order_id is not None
                    and item.exchange_order_id == order.exchange_order_id
                )
                for item in exchange_snapshot.open_orders
            )
            exchange_fill_present = any(
                item.client_order_id == order.client_order_id
                or (
                    order.exchange_order_id is not None
                    and item.exchange_order_id == order.exchange_order_id
                )
                for item in exchange_snapshot.fills
            )
            if exchange_order_present:
                reason_code = "exchange_order_still_open"
            elif exchange_fill_present:
                reason_code = "exchange_fill_detected"
            elif latest_reconciliation is not None and (
                latest_reconciliation.halt_required or latest_reconciliation.review_required
            ):
                reason_code = "latest_reconciliation_not_clean"

        eligible = reason_code is None
        summary = (
            "Eligible for operator resolution: the order predates the current runtime, has no exchange order id, and is absent from the latest exchange snapshot."
            if eligible
            else self._stuck_submission_resolution_summary(reason_code)
        )
        return {
            "eligible": eligible,
            "summary": summary,
            "reason_code": reason_code,
            "order_status": order.status,
            "last_local_update_ts": last_update,
            "runtime_started_at": runtime_started_at,
            "runtime_restarted_after_order": runtime_restarted_after_order,
            "local_fill_count": len(local_fills),
            "exchange_snapshot_fetched_at": exchange_snapshot.fetched_at if exchange_snapshot is not None else None,
            "latest_reconciliation_id": (
                latest_reconciliation.reconciliation_id if latest_reconciliation is not None else None
            ),
            "latest_reconciliation_severity": (
                latest_reconciliation.severity if latest_reconciliation is not None else None
            ),
            "exchange_order_present": exchange_order_present,
            "exchange_fill_present": exchange_fill_present,
            "private_ws_order_present": private_ws_order_present,
            "private_ws_fill_present": private_ws_fill_present,
        }

    @staticmethod
    def _stuck_submission_resolution_summary(reason_code: str | None) -> str:
        messages = {
            "venue_not_exchange_coupled": "This order is not exchange-coupled, so stuck submission recovery is not applicable.",
            "order_not_in_pre_submit_state": "Only pre-submit orders can use stuck submission recovery.",
            "exchange_order_id_present": "This order already has an exchange order id. Use normal exchange refresh or cancel flows instead.",
            "local_fills_present": "Local fills already exist for this order, so manual stuck submission resolution is unsafe.",
            "order_belongs_to_current_runtime": "This order belongs to the current runtime and may still be progressing normally.",
            "exchange_order_seen_via_private_ws": "A recent private websocket order confirmation exists for this order, so it must not be force-resolved locally.",
            "exchange_fill_seen_via_private_ws": "A recent private websocket fill confirmation exists for this order, so manual stuck submission resolution is unsafe.",
            "latest_reconciliation_not_clean": "The latest reconciliation still requires review or halt. Resolve that state before force-resolving submissions.",
            "exchange_confirmation_unavailable": "Exchange confirmation is unavailable, so the runtime cannot safely resolve this submission.",
            "exchange_snapshot_unavailable": "No fresh exchange snapshot is available to confirm the order is absent.",
            "exchange_order_still_open": "The order is still visible on the exchange, so it must not be force-resolved locally.",
            "exchange_fill_detected": "Exchange fills exist for this order, so manual stuck submission resolution is unsafe.",
        }
        return messages.get(reason_code, "This order is not eligible for stuck submission resolution.")

    def _update_recovery_status_for_report(self, report) -> None:
        updated_status = self.runtime.recovery_status.model_copy(
            update={
                "latest_reconciliation_id": report.reconciliation_id,
                "latest_reconciliation_severity": report.severity,
                "recovered_reconciliation_available": True,
            }
        )
        self.runtime.recovery_status = self.recovery_posture.finalize_status(
            base_status=updated_status,
            latest_reconciliation=report,
        )

    def _baseline_switch_history(
        self,
        *,
        as_of_ts: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if as_of_ts is None:
            events = self.runtime.event_store.recent_by_topic(topics.ACCOUNT_BASELINES, limit=limit)
        else:
            events = self.runtime.event_store.by_topic(topics.ACCOUNT_BASELINES)
        if as_of_ts is not None:
            events = [event for event in events if event.payload.get("imported_at") <= as_of_ts]
        rows = []
        for event in events[-limit:]:
            payload = dict(event.payload)
            payload["_event_id"] = event.event_id
            rows.append(payload)
        return rows

    def _persist_blocker_snapshot(
        self,
        *,
        source: str,
        runtime_state: str,
        mode_snapshot: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> None:
        latest = self.runtime.event_store.latest(topics.BLOCKER_SNAPSHOTS)
        candidate = BlockerSnapshotRecord(
            source=source,
            runtime_state=runtime_state,
            operating_state=mode_snapshot["operating_state"],
            mode=mode_snapshot["mode"],
            halted=bool(mode_snapshot["halted"]),
            execution_blocked=bool(mode_snapshot["execution_blocked"]),
            submit_blocked=bool(mode_snapshot["submit_blocked"]),
            blockers=blockers,
        )
        if latest is not None:
            payload = latest.payload
            if (
                payload.get("runtime_state") == candidate.runtime_state
                and payload.get("operating_state") == candidate.operating_state
                and payload.get("halted") == candidate.halted
                and payload.get("execution_blocked") == candidate.execution_blocked
                and payload.get("submit_blocked") == candidate.submit_blocked
                and payload.get("blockers") == candidate.blockers
            ):
                return
        self._append_event(topic=topics.BLOCKER_SNAPSHOTS, key="system", payload_model=candidate)

    @staticmethod
    def _blocker_entry(blocker: str, *, subsystem: str, submit_only: bool | None = None) -> dict[str, Any]:
        submit_only_value = submit_only if submit_only is not None else blocker in {
            "guarded_execution_dry_run",
            "live_submit_disabled",
            "local_demo_no_exchange_submission",
            "real_market_paper_uses_local_paper_execution",
            "real_money_live_not_supported",
            "guarded_live_blocked_by_default",
        }
        affects_execution = not submit_only_value
        recommended_action = "Inspect subsystem status and operator logs before resuming execution."
        if blocker == "local_demo_no_exchange_submission":
            recommended_action = "No action required. Local demo mode intentionally never submits exchange orders."
        elif blocker == "real_market_paper_uses_local_paper_execution":
            recommended_action = "No action required. Real-market paper mode intentionally uses local paper fills."
        elif blocker in {"guarded_execution_dry_run", "live_submit_disabled"}:
            recommended_action = "Enable the guarded simulated submit flags only if you intend to test demo exchange submission."
        elif blocker == "real_money_live_not_supported":
            recommended_action = "Do not attempt real-money live trading in this repository."
        elif blocker == "operator_rebaseline_required":
            recommended_action = "Review the exchange/local divergence and accept the current exchange state as a new baseline only if it is expected."
        elif blocker == "rebaseline_in_progress":
            recommended_action = "Wait for the explicit operator re-baseline action to complete before attempting to resume execution."
        elif blocker == "resume_blocked":
            recommended_action = "Inspect reconciliation, freshness, and recovery state before resuming execution."
        return {
            "blocker": blocker,
            "subsystem": subsystem,
            "affects_execution": affects_execution,
            "affects_account_synchronization": subsystem == "account_state",
            "submit_only": submit_only_value,
            "recommended_action": recommended_action,
        }

    @staticmethod
    def _operator_user_view(
        user: OperatorUserRecord,
        *,
        actor_identity: str | None = None,
        last_admin_protected: bool | None = None,
    ) -> dict[str, Any]:
        payload = user.model_dump(mode="json", exclude={"password_hash"})
        payload["is_current_session_user"] = actor_identity is not None and user.username == actor_identity
        payload["protected_last_admin"] = bool(last_admin_protected and user.enabled and user.role == "admin")
        return payload
