from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aats.events import topics
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import EventEnvelope
from aats.schemas.decision import DecisionContext, DecisionOutcome, PositionTarget
from aats.schemas.execution import ExecutionPlan, FillEvent, OrderIntent, OrderState
from aats.schemas.exchange import AccountBaselineSnapshot
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot, is_baseline_snapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import HealthSnapshot
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, EPSILON_DECIMAL_9, is_effectively_zero, quantize_decimal, to_decimal
from aats.services.portfolio_service.position_keys import position_key_for_snapshot_position
from aats.services.portfolio_service.positions import PortfolioState
from aats.storage.base import AuditRepository, EventStore, PortfolioRepository
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.runtime_scope import (
    RuntimeStateScope,
    latest_snapshot_for_scope,
    topic_events_for_scope,
)


@dataclass(slots=True)
class ReplayResult:
    replayed_event_count: int
    stored_snapshot_count: int
    divergence_count: int
    selected_decision_id: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    divergences: list[str] = field(default_factory=list)
    portfolio_issues: list[str] = field(default_factory=list)
    decision_chain_issues: list[str] = field(default_factory=list)
    execution_chain_issues: list[str] = field(default_factory=list)
    audit_issues: list[str] = field(default_factory=list)
    baseline_switch_count: int = 0
    baseline_switch_issues: list[str] = field(default_factory=list)
    final_reconstructed_snapshot: PortfolioSnapshot | None = None
    final_stored_snapshot: PortfolioSnapshot | None = None
    decision_chains: dict[str, DecisionAuditRecord] = field(default_factory=dict)


class ReplayEngine:
    _SNAPSHOT_DERIVED_FIELD_TOLERANCE = Decimal("1e-7")

    _SCOPED_TOPICS: tuple[str, ...] = (
        topics.MARKET_SNAPSHOTS,
        topics.FEATURE_SNAPSHOTS,
        topics.HEALTH_SNAPSHOTS,
        topics.ACCOUNT_BASELINES,
        topics.DECISION_CONTEXTS,
        topics.BASELINE_ASSESSMENTS,
        topics.AI_DECISION_BRIEFS,
        topics.AI_ASSESSMENTS,
        topics.AI_SHADOW_DECISIONS,
        topics.AI_SHADOW_EVALUATIONS,
        topics.AI_DEGRADATION_EVENTS,
        topics.POSITION_TARGETS,
        topics.DECISION_OUTCOMES,
        topics.POLICY_DECISIONS,
        topics.RISK_DECISIONS,
        topics.EXECUTION_PLANS,
        topics.ORDER_INTENTS,
        topics.ORDER_UPDATES,
        topics.FILL_EVENTS,
        topics.PORTFOLIO_SNAPSHOTS,
        topics.RECONCILIATION_REPORTS,
        topics.AUDIT_RECORDS,
    )

    def __init__(
        self,
        *,
        event_store: EventStore,
        reconstruction_service: PortfolioReconstructionService,
        audit_repo: AuditRepository | None = None,
        portfolio_repo: PortfolioRepository | None = None,
        scope: RuntimeStateScope | None = None,
    ) -> None:
        self.event_store = event_store
        self.reconstruction_service = reconstruction_service
        self.audit_repo = audit_repo
        self.portfolio_repo = portfolio_repo
        self.scope = scope

    def replay(
        self,
        *,
        decision_id: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ReplayResult:
        selected_decision_id = decision_id
        latest_prices: dict[str, Decimal] = {}
        processed_fills: list[FillEvent] = []
        decision_chains: dict[str, DecisionAuditRecord] = {}
        decision_ids: set[str] = set()
        portfolio_issues: list[str] = []
        decision_chain_issues: list[str] = []
        execution_chain_issues: list[str] = []
        audit_issues: list[str] = []
        baseline_switch_issues: list[str] = []
        baseline_switches: list[AccountBaselineSnapshot] = []
        stored_snapshot_count = 0
        final_stored_snapshot: PortfolioSnapshot | None = None
        baseline_snapshot: PortfolioSnapshot | None = None
        events = self._select_events(decision_id=decision_id, start_at=start_at, end_at=end_at)
        events_by_id = {event.event_id: event for event in events}
        events_by_decision: dict[str, list[EventEnvelope]] = defaultdict(list)
        order_intents_by_intent_id: dict[str, OrderIntent] = {}
        order_states_by_intent_id: dict[str, OrderState] = {}
        fill_events_by_fill_id: dict[str, FillEvent] = {}
        fill_event_refs_by_fill_id: dict[str, list[str]] = defaultdict(list)
        order_state_updates_by_client_order_id: dict[str, list[OrderState]] = defaultdict(list)
        audit_event_ids_by_decision: dict[str, str] = {}

        for envelope in events:
            envelope_decision_id = envelope.payload.get("decision_id")
            if isinstance(envelope_decision_id, str):
                decision_ids.add(envelope_decision_id)
                events_by_decision[envelope_decision_id].append(envelope)

            if envelope.topic == topics.MARKET_SNAPSHOTS:
                snapshot = MarketSnapshot.model_validate(envelope.payload)
                latest_prices[snapshot.symbol] = snapshot.last_price
                continue

            if envelope.topic == topics.ORDER_INTENTS:
                intent = OrderIntent.model_validate(envelope.payload)
                order_intents_by_intent_id[intent.intent_id] = intent
                continue

            if envelope.topic == topics.ORDER_UPDATES:
                order_state = OrderState.model_validate(envelope.payload)
                order_states_by_intent_id[order_state.intent_id] = order_state
                order_state_updates_by_client_order_id[order_state.client_order_id].append(order_state)
                continue

            if envelope.topic == topics.FILL_EVENTS:
                fill = FillEvent.model_validate(envelope.payload)
                processed_fills.append(fill)
                fill_events_by_fill_id.setdefault(fill.fill_id, fill)
                fill_event_refs_by_fill_id[fill.fill_id].append(envelope.event_id)
                continue

            if envelope.topic == topics.AUDIT_RECORDS:
                record = DecisionAuditRecord.model_validate(envelope.payload)
                decision_chains[record.decision_id] = record
                audit_event_ids_by_decision[record.decision_id] = envelope.event_id
                continue

            if envelope.topic == topics.ACCOUNT_BASELINES:
                baseline_switches.append(AccountBaselineSnapshot.model_validate(envelope.payload))
                continue

            if envelope.topic == topics.PORTFOLIO_SNAPSHOTS:
                stored_snapshot_count += 1
                stored_snapshot = PortfolioSnapshot.model_validate(envelope.payload)
                final_stored_snapshot = stored_snapshot

                if is_baseline_snapshot(stored_snapshot):
                    baseline_snapshot = stored_snapshot
                    continue

                reconstructed_snapshot = self._rebuild_snapshot(
                    fills=processed_fills,
                    baseline_snapshot=baseline_snapshot,
                    price_provider=self._snapshot_price_provider(
                        stored_snapshot=stored_snapshot,
                        latest_prices=latest_prices,
                    ),
                )
                mismatch = self._snapshot_mismatch(
                    stored_snapshot=stored_snapshot,
                    reconstructed_snapshot=reconstructed_snapshot,
                )
                if mismatch:
                    portfolio_issues.append(
                        f"portfolio_snapshot_event={envelope.event_id} mismatch={mismatch}"
                    )

        final_reconstructed_snapshot = None
        if stored_snapshot_count > 0 or processed_fills:
            final_price_provider = (
                self._snapshot_price_provider(
                    stored_snapshot=final_stored_snapshot,
                    latest_prices=latest_prices,
                )
                if final_stored_snapshot is not None
                else lambda symbol: latest_prices.get(symbol, Decimal("0"))
            )
            final_reconstructed_snapshot = self._rebuild_snapshot(
                fills=processed_fills,
                baseline_snapshot=baseline_snapshot,
                price_provider=final_price_provider,
            )

        execution_chain_issues.extend(
            self._validate_execution_chain(
                order_intents_by_intent_id=order_intents_by_intent_id,
                order_states_by_intent_id=order_states_by_intent_id,
                fill_events_by_fill_id=fill_events_by_fill_id,
                fill_event_refs_by_fill_id=fill_event_refs_by_fill_id,
                order_state_updates_by_client_order_id=order_state_updates_by_client_order_id,
            )
        )
        decision_chain_issues.extend(
            self._validate_decision_chains(
                decision_ids=decision_ids,
                decision_chains=decision_chains,
                events_by_id=events_by_id,
                events_by_decision=events_by_decision,
                order_states_by_intent_id=order_states_by_intent_id,
            )
        )
        audit_issues.extend(
            self._validate_audit_integrity(
                decision_chains=decision_chains,
                audit_event_ids_by_decision=audit_event_ids_by_decision,
                events_by_id=events_by_id,
            )
        )
        baseline_switch_issues.extend(
            self._validate_baseline_switches(
                baseline_switches=baseline_switches,
                events_by_id=events_by_id,
            )
        )

        if self.portfolio_repo is not None and final_stored_snapshot is not None:
            repo_latest = (
                latest_snapshot_for_scope(self.portfolio_repo, self.scope)
                if self.scope is not None
                else self.portfolio_repo.latest()
            )
            if repo_latest is None:
                portfolio_issues.append("portfolio_repository_missing_latest_snapshot")
            elif self._snapshot_mismatch(
                stored_snapshot=repo_latest,
                reconstructed_snapshot=final_stored_snapshot,
            ):
                portfolio_issues.append("portfolio_repository_latest_snapshot_mismatch")
            final_stored_snapshot = repo_latest

        if self.audit_repo is not None:
            audit_issues.extend(
                self._validate_audit_repository(
                    decision_chains=decision_chains,
                    audit_event_ids_by_decision=audit_event_ids_by_decision,
                    selected_decision_ids=set(decision_chains),
                )
            )

        divergences = [
            *portfolio_issues,
            *decision_chain_issues,
            *execution_chain_issues,
            *audit_issues,
            *baseline_switch_issues,
        ]
        return ReplayResult(
            selected_decision_id=selected_decision_id,
            start_at=start_at,
            end_at=end_at,
            replayed_event_count=len(events),
            stored_snapshot_count=stored_snapshot_count,
            divergence_count=len(divergences),
            divergences=divergences,
            portfolio_issues=portfolio_issues,
            decision_chain_issues=decision_chain_issues,
            execution_chain_issues=execution_chain_issues,
            audit_issues=audit_issues,
            baseline_switch_count=len(baseline_switches),
            baseline_switch_issues=baseline_switch_issues,
            final_reconstructed_snapshot=final_reconstructed_snapshot,
            final_stored_snapshot=final_stored_snapshot,
            decision_chains=decision_chains,
        )

    def _rebuild_snapshot(
        self,
        *,
        fills: list[FillEvent],
        baseline_snapshot: PortfolioSnapshot | None,
        price_provider,
    ) -> PortfolioSnapshot:
        if baseline_snapshot is None:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=price_provider,
            )

        state = PortfolioState(initial_usdt_balance=self.reconstruction_service.initial_usdt_balance)
        state.load_portfolio_snapshot(baseline_snapshot)
        baseline_ts = baseline_snapshot.snapshot_ts
        for fill in sorted(fills, key=lambda item: (item.ingestion_timestamp, item.fill_id)):
            if fill.ingestion_timestamp >= baseline_ts:
                state.apply_fill(fill)
        return self.reconstruction_service.snapshot_builder.build(
            state=state,
            price_provider=price_provider,
        )

    @staticmethod
    def _validate_baseline_switches(
        *,
        baseline_switches: list[AccountBaselineSnapshot],
        events_by_id: dict[str, EventEnvelope],
    ) -> list[str]:
        issues: list[str] = []
        seen_ids: set[str] = set()
        for baseline in baseline_switches:
            if baseline.baseline_id in seen_ids:
                issues.append(f"baseline_switch_duplicate baseline_id={baseline.baseline_id}")
            seen_ids.add(baseline.baseline_id)
            if baseline.previous_baseline_ref is not None and baseline.previous_baseline_ref not in events_by_id:
                issues.append(
                    f"baseline_switch_missing_previous_ref baseline_id={baseline.baseline_id} previous_baseline_ref={baseline.previous_baseline_ref}"
                )
            if baseline.baseline_kind == "operator_rebaseline" and baseline.operator_action_ref is None:
                issues.append(f"baseline_switch_missing_operator_action baseline_id={baseline.baseline_id}")
            if baseline.operator_action_ref is not None and baseline.operator_action_ref not in events_by_id:
                issues.append(
                    f"baseline_switch_missing_operator_action_ref baseline_id={baseline.baseline_id} operator_action_ref={baseline.operator_action_ref}"
                )
        return issues

    @staticmethod
    def _snapshot_price_provider(
        *,
        stored_snapshot: PortfolioSnapshot | None,
        latest_prices: dict[str, Decimal],
    ):
        snapshot_marks: dict[str, Decimal] = {}
        if stored_snapshot is not None:
            for position in stored_snapshot.positions:
                if abs(position.position_qty) > EPSILON_DECIMAL_12:
                    snapshot_marks[position.symbol] = (
                        position.position_notional / position.position_qty
                    )
                elif position.avg_entry_price > 0:
                    snapshot_marks[position.symbol] = position.avg_entry_price

        def provider(symbol: str) -> Decimal:
            if symbol in snapshot_marks:
                return snapshot_marks[symbol]
            return latest_prices.get(symbol, Decimal("0"))

        return provider

    def _select_events(
        self,
        *,
        decision_id: str | None,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> list[EventEnvelope]:
        if decision_id is not None:
            events = self.event_store.by_decision(decision_id)
            referenced_event_ids: set[str] = set()
            for event in events:
                if event.topic != topics.DECISION_CONTEXTS:
                    continue
                context = DecisionContext.model_validate(event.payload)
                referenced_event_ids.update(
                    {
                        context.market_snapshot_ref,
                        context.feature_snapshot_ref,
                        context.portfolio_snapshot_ref,
                        context.health_snapshot_ref,
                    }
                )
            if self.audit_repo is not None:
                record = self.audit_repo.get(decision_id)
                if record is not None:
                    referenced_event_ids.update(
                        ref
                        for ref in (
                            record.decision_context_ref,
                            record.baseline_assessment_ref,
                            record.ai_decision_brief_ref,
                            record.ai_market_assessment_ref,
                            record.position_target_ref,
                            record.policy_decision_ref,
                            record.risk_decision_ref,
                            record.execution_plan_ref,
                            record.portfolio_delta_ref,
                            *record.order_intent_refs,
                            *record.order_state_refs,
                            *record.fill_event_refs,
                            *record.ai_shadow_decision_refs,
                            *record.ai_shadow_evaluation_refs,
                            *record.reconciliation_refs,
                        )
                        if ref is not None
                    )
            referenced_events = [
                referenced
                for ref in referenced_event_ids
                for referenced in [self.event_store.get(ref)]
                if referenced is not None
            ]
            filtered_events = events + [
                event for event in referenced_events if event.event_id not in {item.event_id for item in events}
            ]
            filtered_events = [
                event
                for event in filtered_events
                if (start_at is None or event.event_timestamp >= start_at)
                and (end_at is None or event.event_timestamp <= end_at)
            ]
            return sorted(
                filtered_events,
                key=lambda item: (item.event_timestamp, item.created_at, item.event_id),
            )
        if self.scope is not None:
            events = [
                event
                for topic in self._SCOPED_TOPICS
                for event in topic_events_for_scope(self.event_store, topic, self.scope)
                if (start_at is None or event.event_timestamp >= start_at)
                and (end_at is None or event.event_timestamp <= end_at)
            ]
            return sorted(
                events,
                key=lambda item: (item.event_timestamp, item.created_at, item.event_id),
            )
        if start_at is not None or end_at is not None:
            return self.event_store.between(start_at=start_at, end_at=end_at)
        return self.event_store.all()

    def _validate_execution_chain(
        self,
        *,
        order_intents_by_intent_id: dict[str, OrderIntent],
        order_states_by_intent_id: dict[str, OrderState],
        fill_events_by_fill_id: dict[str, FillEvent],
        fill_event_refs_by_fill_id: dict[str, list[str]],
        order_state_updates_by_client_order_id: dict[str, list[OrderState]],
    ) -> list[str]:
        issues: list[str] = []
        state_machine = OrderStateMachine()
        fills_by_intent_id: dict[str, list[FillEvent]] = defaultdict(list)
        for fill in fill_events_by_fill_id.values():
            fills_by_intent_id[fill.intent_id].append(fill)

        for intent_id, intent in sorted(order_intents_by_intent_id.items()):
            order_state = order_states_by_intent_id.get(intent_id)
            if order_state is None:
                issues.append(
                    f"execution_chain_missing_order_state decision_id={intent.decision_id} intent_id={intent_id}"
                )
                continue
            if order_state.decision_id != intent.decision_id:
                issues.append(
                    "execution_chain_order_state_decision_mismatch "
                    f"intent_id={intent_id} order_state_decision_id={order_state.decision_id} "
                    f"intent_decision_id={intent.decision_id}"
                )
                continue
            issues.extend(
                self._execution_semantic_mismatches(
                    left=intent,
                    right=order_state,
                    context=f"intent_id={intent_id}",
                    label="order_state",
                )
            )
            fill_events = sorted(
                fills_by_intent_id.get(intent_id, []),
                key=lambda item: (item.ingestion_timestamp, item.fill_id),
            )
            cumulative_fill_qty = sum((fill.fill_qty for fill in fill_events), start=Decimal("0"))
            if cumulative_fill_qty - order_state.requested_qty > EPSILON_DECIMAL_12:
                issues.append(
                    f"execution_chain_overfilled_order intent_id={intent_id} cumulative_fill_qty={cumulative_fill_qty} requested_qty={order_state.requested_qty}"
                )
            if abs(cumulative_fill_qty - order_state.filled_qty) > EPSILON_DECIMAL_12:
                issues.append(
                    f"execution_chain_fill_quantity_mismatch intent_id={intent_id} cumulative_fill_qty={cumulative_fill_qty} order_state_filled_qty={order_state.filled_qty}"
                )
            expected_remaining = max(order_state.requested_qty - cumulative_fill_qty, Decimal("0"))
            if abs(order_state.remaining_qty - expected_remaining) > EPSILON_DECIMAL_12:
                issues.append(
                    f"execution_chain_remaining_quantity_mismatch intent_id={intent_id} remaining_qty={order_state.remaining_qty} expected_remaining={expected_remaining}"
                )
            if order_state.status == "FILLED" and abs(cumulative_fill_qty - order_state.requested_qty) > EPSILON_DECIMAL_12:
                issues.append(
                    f"execution_chain_filled_without_complete_fill_quantity intent_id={intent_id}"
                )
            if order_state.status in {"SUBMITTED", "CREATED", "SUBMITTING"} and cumulative_fill_qty > EPSILON_DECIMAL_12:
                issues.append(
                    f"execution_chain_open_state_with_fill_quantity intent_id={intent_id} status={order_state.status}"
                )

        for intent_id, order_state in sorted(order_states_by_intent_id.items()):
            intent = order_intents_by_intent_id.get(intent_id)
            if intent is None:
                issues.append(
                    f"execution_chain_orphan_order_state decision_id={order_state.decision_id} intent_id={intent_id}"
                )

        for fill_id, fill in sorted(fill_events_by_fill_id.items()):
            intent = order_intents_by_intent_id.get(fill.intent_id)
            if intent is None:
                issues.append(
                    f"execution_chain_orphan_fill decision_id={fill.decision_id} fill_id={fill_id} intent_id={fill.intent_id}"
                )
            elif intent.decision_id != fill.decision_id:
                issues.append(
                    "execution_chain_fill_decision_mismatch "
                    f"fill_id={fill_id} fill_decision_id={fill.decision_id} intent_decision_id={intent.decision_id}"
                )
            else:
                issues.extend(
                    self._execution_semantic_mismatches(
                        left=intent,
                        right=fill,
                        context=f"fill_id={fill_id}",
                        label="fill_event",
                    )
                )

            order_state = order_states_by_intent_id.get(fill.intent_id)
            if order_state is None:
                issues.append(
                    f"execution_chain_missing_order_state_for_fill fill_id={fill_id} intent_id={fill.intent_id}"
                )
                continue
            if order_state.client_order_id != fill.client_order_id:
                issues.append(
                    "execution_chain_client_order_mismatch "
                    f"fill_id={fill_id} intent_id={fill.intent_id} "
                    f"fill_client_order_id={fill.client_order_id} order_state_client_order_id={order_state.client_order_id}"
                )
            if order_state.decision_id != fill.decision_id:
                issues.append(
                    "execution_chain_fill_order_state_decision_mismatch "
                    f"fill_id={fill_id} fill_decision_id={fill.decision_id} order_state_decision_id={order_state.decision_id}"
                )
            issues.extend(
                self._execution_semantic_mismatches(
                    left=order_state,
                    right=fill,
                    context=f"fill_id={fill_id}",
                    label="fill_order_state",
                )
            )

        for fill_id, event_refs in sorted(fill_event_refs_by_fill_id.items()):
            if len(event_refs) > 1:
                issues.append(
                    f"execution_chain_duplicate_fill_events fill_id={fill_id} event_refs={event_refs}"
                )
        for client_order_id, states in sorted(order_state_updates_by_client_order_id.items()):
            ordered_states = sorted(
                states,
                key=lambda item: (item.last_update_ts or item.created_at, item.created_at, item.status),
            )
            for issue in state_machine.validate_path(ordered_states):
                issues.append(f"execution_chain_invalid_state_transition client_order_id={client_order_id} {issue}")
        return issues

    @staticmethod
    def _execution_semantic_mismatches(
        *,
        left,
        right,
        context: str,
        label: str,
    ) -> list[str]:
        issues: list[str] = []
        fields = (
            "reduce_only",
            "close_only",
            "td_mode",
            "position_mode",
            "pos_side",
            "reduce_only_reason",
            "close_only_reason",
            "instrument_family",
            "settle_currency",
            "execution_action",
            "position_intent",
            "margin_mode",
            "product_type",
        )
        for field_name in fields:
            left_value = ReplayEngine._normalized_semantic_value(getattr(left, field_name, None))
            right_value = ReplayEngine._normalized_semantic_value(getattr(right, field_name, None))
            if left_value != right_value:
                issues.append(
                    f"execution_chain_{label}_semantic_mismatch {context} field={field_name} left={left_value} right={right_value}"
                )
        return issues

    @staticmethod
    def _normalized_semantic_value(value):
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    def _validate_decision_chains(
        self,
        *,
        decision_ids: set[str],
        decision_chains: dict[str, DecisionAuditRecord],
        events_by_id: dict[str, EventEnvelope],
        events_by_decision: dict[str, list[EventEnvelope]],
        order_states_by_intent_id: dict[str, OrderState],
    ) -> list[str]:
        issues: list[str] = []
        for decision_id in sorted(decision_ids):
            record = decision_chains.get(decision_id)
            if record is None:
                issues.append(f"decision_chain_missing_audit_record decision_id={decision_id}")
                continue

            context = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.decision_context_ref,
                ref_name="decision_context_ref",
                expected_topic=topics.DECISION_CONTEXTS,
                required=True,
            )
            parsed_context = (
                None
                if context is None
                else self._validate_context_health_link(
                    issues=issues,
                    decision_id=decision_id,
                    context_event=context,
                    events_by_id=events_by_id,
                )
            )
            self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.baseline_assessment_ref,
                ref_name="baseline_assessment_ref",
                expected_topic=topics.BASELINE_ASSESSMENTS,
                required=True,
            )
            self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.ai_decision_brief_ref,
                ref_name="ai_decision_brief_ref",
                expected_topic=topics.AI_DECISION_BRIEFS,
                required=False,
            )
            self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.ai_market_assessment_ref,
                ref_name="ai_market_assessment_ref",
                expected_topic=topics.AI_ASSESSMENTS,
                required=False,
            )
            self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.position_target_ref,
                ref_name="position_target_ref",
                expected_topic=topics.POSITION_TARGETS,
                required=True,
            )
            self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.decision_outcome_ref,
                ref_name="decision_outcome_ref",
                expected_topic=topics.DECISION_OUTCOMES,
                required=False,
            )

            for ref in record.ai_shadow_decision_refs:
                self._validate_ref(
                    issues=issues,
                    decision_id=decision_id,
                    events_by_id=events_by_id,
                    ref=ref,
                    ref_name="ai_shadow_decision_refs",
                    expected_topic=topics.AI_SHADOW_DECISIONS,
                    required=True,
                )
            for ref in record.ai_shadow_evaluation_refs:
                self._validate_ref(
                    issues=issues,
                    decision_id=decision_id,
                    events_by_id=events_by_id,
                    ref=ref,
                    ref_name="ai_shadow_evaluation_refs",
                    expected_topic=topics.AI_SHADOW_EVALUATIONS,
                    required=True,
                )

            target_event = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.position_target_ref,
                ref_name="position_target_ref",
                expected_topic=topics.POSITION_TARGETS,
                required=True,
            )
            policy_event = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.policy_decision_ref,
                ref_name="policy_decision_ref",
                expected_topic=topics.POLICY_DECISIONS,
                required=True,
            )
            risk_event = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.risk_decision_ref,
                ref_name="risk_decision_ref",
                expected_topic=topics.RISK_DECISIONS,
                required=True,
            )
            execution_plan_event = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.execution_plan_ref,
                ref_name="execution_plan_ref",
                expected_topic=topics.EXECUTION_PLANS,
                required=bool(record.order_intent_refs),
            )

            target = (
                PositionTarget.model_validate(target_event.payload)
                if target_event is not None
                else None
            )
            outcome_event = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.decision_outcome_ref,
                ref_name="decision_outcome_ref",
                expected_topic=topics.DECISION_OUTCOMES,
                required=False,
            )
            final_outcome = (
                DecisionOutcome.model_validate(outcome_event.payload)
                if outcome_event is not None
                else None
            )
            policy = (
                PolicyDecision.model_validate(policy_event.payload)
                if policy_event is not None
                else None
            )
            risk = RiskDecision.model_validate(risk_event.payload) if risk_event is not None else None
            execution_plan = (
                ExecutionPlan.model_validate(execution_plan_event.payload)
                if execution_plan_event is not None
                else None
            )

            intent_ids: set[str] = set()
            for ref in record.order_intent_refs:
                event = self._validate_ref(
                    issues=issues,
                    decision_id=decision_id,
                    events_by_id=events_by_id,
                    ref=ref,
                    ref_name="order_intent_refs",
                    expected_topic=topics.ORDER_INTENTS,
                    required=True,
                )
                if event is None:
                    continue
                intent = OrderIntent.model_validate(event.payload)
                intent_ids.add(intent.intent_id)

            order_state_intent_ids: set[str] = set()
            for ref in record.order_state_refs:
                event = self._validate_ref(
                    issues=issues,
                    decision_id=decision_id,
                    events_by_id=events_by_id,
                    ref=ref,
                    ref_name="order_state_refs",
                    expected_topic=topics.ORDER_UPDATES,
                    required=True,
                )
                if event is None:
                    continue
                order_state = OrderState.model_validate(event.payload)
                order_state_intent_ids.add(order_state.intent_id)
                if order_state.intent_id not in intent_ids:
                    issues.append(
                        "decision_chain_order_state_not_linked_to_audited_intent "
                        f"decision_id={decision_id} intent_id={order_state.intent_id}"
                    )

            fill_ids: set[str] = set()
            for ref in record.fill_event_refs:
                event = self._validate_ref(
                    issues=issues,
                    decision_id=decision_id,
                    events_by_id=events_by_id,
                    ref=ref,
                    ref_name="fill_event_refs",
                    expected_topic=topics.FILL_EVENTS,
                    required=True,
                )
                if event is None:
                    continue
                fill = FillEvent.model_validate(event.payload)
                fill_ids.add(fill.fill_id)
                if fill.intent_id not in intent_ids:
                    issues.append(
                        f"decision_chain_fill_not_linked_to_audited_intent decision_id={decision_id} fill_id={fill.fill_id}"
                    )
                order_state = order_states_by_intent_id.get(fill.intent_id)
                if order_state is None:
                    issues.append(
                        f"decision_chain_missing_order_state decision_id={decision_id} fill_id={fill.fill_id}"
                    )

            snapshot_required = bool(record.fill_event_refs)
            portfolio_event = self._validate_ref(
                issues=issues,
                decision_id=decision_id,
                events_by_id=events_by_id,
                ref=record.portfolio_delta_ref,
                ref_name="portfolio_delta_ref",
                expected_topic=topics.PORTFOLIO_SNAPSHOTS,
                required=snapshot_required,
            )
            if portfolio_event is not None:
                snapshot = PortfolioSnapshot.model_validate(portfolio_event.payload)
                if snapshot.decision_id != decision_id:
                    issues.append(
                        "decision_chain_snapshot_decision_mismatch "
                        f"decision_id={decision_id} snapshot_decision_id={snapshot.decision_id}"
                    )
                if snapshot.source_intent_id is not None and snapshot.source_intent_id not in intent_ids:
                    issues.append(
                        "decision_chain_snapshot_intent_mismatch "
                        f"decision_id={decision_id} source_intent_id={snapshot.source_intent_id}"
                    )
                if snapshot.source_fill_id is not None and snapshot.source_fill_id not in fill_ids:
                    issues.append(
                        "decision_chain_snapshot_fill_mismatch "
                        f"decision_id={decision_id} source_fill_id={snapshot.source_fill_id}"
                    )

            if record.portfolio_delta_ref is not None and not record.reconciliation_refs:
                issues.append(
                    f"decision_chain_missing_reconciliation_ref decision_id={decision_id}"
                )

            for ref in record.reconciliation_refs:
                event = self._validate_ref(
                    issues=issues,
                    decision_id=decision_id,
                    events_by_id=events_by_id,
                    ref=ref,
                    ref_name="reconciliation_refs",
                    expected_topic=topics.RECONCILIATION_REPORTS,
                    required=True,
                )
                if event is None:
                    continue
                report = ReconciliationReport.model_validate(event.payload)
                if report.decision_id != decision_id:
                    issues.append(
                        "decision_chain_reconciliation_decision_mismatch "
                        f"decision_id={decision_id} reconciliation_decision_id={report.decision_id}"
                    )
                if report.portfolio_snapshot_ref != record.portfolio_delta_ref:
                    issues.append(
                        "decision_chain_reconciliation_snapshot_mismatch "
                        f"decision_id={decision_id} reconciliation_id={report.reconciliation_id} "
                        f"portfolio_snapshot_ref={report.portfolio_snapshot_ref} "
                        f"audit_portfolio_snapshot_ref={record.portfolio_delta_ref}"
                    )

            if policy is not None and not policy.allowed and record.order_intent_refs:
                issues.append(
                    f"decision_chain_policy_blocked_but_intent_emitted decision_id={decision_id}"
                )
            if final_outcome is not None and not final_outcome.finalized:
                issues.append(
                    f"decision_chain_outcome_not_finalized decision_id={decision_id}"
                )
            if risk is not None and (not risk.approved or risk.halt_required) and record.order_intent_refs:
                issues.append(
                    f"decision_chain_risk_blocked_but_intent_emitted decision_id={decision_id}"
                )
            if (
                target is not None
                and abs(to_decimal(target.delta_position_qty)) < EPSILON_DECIMAL_12
                and record.order_intent_refs
            ):
                issues.append(
                    f"decision_chain_zero_delta_but_intent_emitted decision_id={decision_id}"
                )
            if record.order_intent_refs and not record.fill_event_refs:
                issues.append(f"decision_chain_missing_fill_ref decision_id={decision_id}")
            if record.order_intent_refs:
                if execution_plan is None:
                    issues.append(f"decision_chain_missing_execution_plan decision_id={decision_id}")
                else:
                    if parsed_context is not None and execution_plan.symbol != parsed_context.symbol:
                        issues.append(
                            "decision_chain_execution_plan_symbol_mismatch "
                            f"decision_id={decision_id} execution_plan_symbol={execution_plan.symbol} "
                            f"context_symbol={parsed_context.symbol}"
                        )
                    if (
                        risk is not None
                        and abs(
                            execution_plan.approved_target_position_qty - to_decimal(risk.capped_target_position_qty)
                        ) > EPSILON_DECIMAL_12
                    ):
                        issues.append(
                            "decision_chain_execution_plan_risk_mismatch "
                            f"decision_id={decision_id} approved_target_position_qty={execution_plan.approved_target_position_qty} "
                            f"risk_capped_target_position_qty={risk.capped_target_position_qty}"
                        )
                if not record.order_state_refs:
                    issues.append(f"decision_chain_missing_order_state_ref decision_id={decision_id}")
                elif not order_state_intent_ids.issuperset(intent_ids):
                    issues.append(
                        f"decision_chain_missing_order_state_for_intent decision_id={decision_id}"
                    )

        return issues

    def _validate_context_health_link(
        self,
        *,
        issues: list[str],
        decision_id: str,
        context_event: EventEnvelope,
        events_by_id: dict[str, EventEnvelope],
    ) -> DecisionContext | None:
        context = DecisionContext.model_validate(context_event.payload)
        health_event = events_by_id.get(context.health_snapshot_ref)
        if health_event is None:
            issues.append(
                "decision_chain_missing_health_snapshot "
                f"decision_id={decision_id} health_snapshot_ref={context.health_snapshot_ref}"
            )
            return context
        if health_event.topic != topics.HEALTH_SNAPSHOTS:
            issues.append(
                "decision_chain_wrong_health_snapshot_topic "
                f"decision_id={decision_id} health_snapshot_ref={context.health_snapshot_ref} "
                f"actual_topic={health_event.topic}"
            )
            return context
        health_snapshot = HealthSnapshot.model_validate(health_event.payload)
        if health_snapshot.decision_id != decision_id:
            issues.append(
                "decision_chain_health_snapshot_decision_mismatch "
                f"decision_id={decision_id} health_snapshot_decision_id={health_snapshot.decision_id}"
            )
        return context

    def _validate_audit_integrity(
        self,
        *,
        decision_chains: dict[str, DecisionAuditRecord],
        audit_event_ids_by_decision: dict[str, str],
        events_by_id: dict[str, EventEnvelope],
    ) -> list[str]:
        issues: list[str] = []
        for decision_id, record in sorted(decision_chains.items()):
            audit_event_id = audit_event_ids_by_decision.get(decision_id)
            if audit_event_id is None:
                issues.append(f"audit_event_missing decision_id={decision_id}")
                continue
            if audit_event_id not in events_by_id:
                issues.append(f"audit_event_not_persisted decision_id={decision_id} audit_event_id={audit_event_id}")

            for ref_name, ref_value in (
                ("decision_context_ref", record.decision_context_ref),
                ("baseline_assessment_ref", record.baseline_assessment_ref),
                ("ai_decision_brief_ref", record.ai_decision_brief_ref),
                ("ai_market_assessment_ref", record.ai_market_assessment_ref),
                ("position_target_ref", record.position_target_ref),
                ("policy_decision_ref", record.policy_decision_ref),
                ("risk_decision_ref", record.risk_decision_ref),
                ("execution_plan_ref", record.execution_plan_ref),
                ("portfolio_delta_ref", record.portfolio_delta_ref),
            ):
                if ref_value is not None and ref_value not in events_by_id:
                    issues.append(
                        f"audit_ref_missing decision_id={decision_id} ref_name={ref_name} ref={ref_value}"
                    )
            for ref_name, refs in (
                ("order_intent_refs", record.order_intent_refs),
                ("order_state_refs", record.order_state_refs),
                ("fill_event_refs", record.fill_event_refs),
                ("ai_shadow_decision_refs", record.ai_shadow_decision_refs),
                ("ai_shadow_evaluation_refs", record.ai_shadow_evaluation_refs),
                ("reconciliation_refs", record.reconciliation_refs),
            ):
                for ref_value in refs:
                    if ref_value not in events_by_id:
                        issues.append(
                            f"audit_ref_missing decision_id={decision_id} ref_name={ref_name} ref={ref_value}"
                        )
        return issues

    def _validate_audit_repository(
        self,
        *,
        decision_chains: dict[str, DecisionAuditRecord],
        audit_event_ids_by_decision: dict[str, str],
        selected_decision_ids: set[str] | None = None,
    ) -> list[str]:
        issues: list[str] = []
        if self.audit_repo is None:
            return issues

        selected_ids = set(selected_decision_ids or set())
        records = self.audit_repo.all()
        if selected_ids:
            records = [record for record in records if record.decision_id in selected_ids]

        for record in records:
            streamed = decision_chains.get(record.decision_id)
            if streamed is None:
                issues.append(f"audit_repository_missing_streamed_record decision_id={record.decision_id}")
                continue
            if streamed.model_dump(mode="json") != record.model_dump(mode="json"):
                issues.append(f"audit_repository_latest_record_mismatch decision_id={record.decision_id}")
            if record.decision_id not in audit_event_ids_by_decision:
                issues.append(f"audit_repository_missing_audit_event decision_id={record.decision_id}")
        return issues

    @staticmethod
    def _validate_ref(
        *,
        issues: list[str],
        decision_id: str,
        events_by_id: dict[str, EventEnvelope],
        ref: str | None,
        ref_name: str,
        expected_topic: str,
        required: bool,
    ):
        if ref is None:
            if required:
                issues.append(f"decision_chain_missing_ref decision_id={decision_id} ref_name={ref_name}")
            return None

        event = events_by_id.get(ref)
        if event is None:
            issues.append(f"decision_chain_missing_event decision_id={decision_id} ref_name={ref_name} ref={ref}")
            return None
        if event.topic != expected_topic:
            issues.append(
                "decision_chain_wrong_topic "
                f"decision_id={decision_id} ref_name={ref_name} ref={ref} "
                f"expected_topic={expected_topic} actual_topic={event.topic}"
            )
            return None

        payload_decision_id = event.payload.get("decision_id")
        if isinstance(payload_decision_id, str) and payload_decision_id != decision_id:
            issues.append(
                "decision_chain_wrong_decision_link "
                f"decision_id={decision_id} ref_name={ref_name} ref={ref} "
                f"payload_decision_id={payload_decision_id}"
            )
        return event

    @staticmethod
    def _snapshot_signature(snapshot: PortfolioSnapshot) -> dict[str, object]:
        return {
            "balances": {
                currency: ReplayEngine._normalize_decimal(value)
                for currency, value in snapshot.balances.items()
            },
            "positions": {
                position_key_for_snapshot_position(position): ReplayEngine._normalize_decimal(position.position_qty)
                for position in snapshot.positions
            },
            "cost_basis": {
                symbol: ReplayEngine._normalize_decimal(value)
                for symbol, value in snapshot.cost_basis.items()
            },
            "realized_pnl": ReplayEngine._normalize_decimal(snapshot.realized_pnl),
            "unrealized_pnl": ReplayEngine._normalize_decimal(snapshot.unrealized_pnl),
            "total_equity": ReplayEngine._normalize_decimal(snapshot.total_equity),
            "gross_exposure": ReplayEngine._normalize_decimal(snapshot.gross_exposure),
            "net_exposure": ReplayEngine._normalize_decimal(snapshot.net_exposure),
        }

    @staticmethod
    def _snapshot_mismatch(
        *,
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
    ) -> dict[str, object]:
        mismatch: dict[str, object] = {}
        stored_signature = ReplayEngine._snapshot_signature(stored_snapshot)
        reconstructed_signature = ReplayEngine._snapshot_signature(reconstructed_snapshot)
        if not ReplayEngine._decimal_map_matches(
            stored_snapshot.balances,
            reconstructed_snapshot.balances,
            tolerance=EPSILON_DECIMAL_9,
        ):
            mismatch["balances"] = {
                "stored": stored_snapshot.balances,
                "reconstructed": reconstructed_snapshot.balances,
            }
        if not ReplayEngine._decimal_map_matches(
            stored_snapshot.cost_basis,
            reconstructed_snapshot.cost_basis,
            tolerance=EPSILON_DECIMAL_9,
        ):
            mismatch["cost_basis"] = {
                "stored": stored_snapshot.cost_basis,
                "reconstructed": reconstructed_snapshot.cost_basis,
            }

        stored_positions = stored_signature["positions"]
        replayed_positions = reconstructed_signature["positions"]
        if not ReplayEngine._decimal_map_matches(
            stored_positions,
            replayed_positions,
            tolerance=EPSILON_DECIMAL_9,
        ):
            mismatch["positions"] = {
                "stored": stored_positions,
                "reconstructed": replayed_positions,
            }

        numeric_fields = ("realized_pnl", "unrealized_pnl", "total_equity", "gross_exposure", "net_exposure")
        for field_name in numeric_fields:
            if abs(
                ReplayEngine._normalize_decimal(stored_signature[field_name])
                - ReplayEngine._normalize_decimal(reconstructed_signature[field_name])
            ) > ReplayEngine._SNAPSHOT_DERIVED_FIELD_TOLERANCE:
                mismatch[field_name] = {
                    "stored": getattr(stored_snapshot, field_name),
                    "reconstructed": getattr(reconstructed_snapshot, field_name),
                }

        return mismatch

    @staticmethod
    def _normalize_decimal(value: Decimal | float | int) -> Decimal:
        decimal_value = quantize_decimal(value)
        if is_effectively_zero(decimal_value):
            return Decimal("0")
        return decimal_value.normalize()

    @staticmethod
    def _decimal_map_matches(
        left: dict[str, Decimal | float | int],
        right: dict[str, Decimal | float | int],
        *,
        tolerance: Decimal,
    ) -> bool:
        if set(left) != set(right):
            return False
        for key in left:
            if abs(ReplayEngine._normalize_decimal(left[key]) - ReplayEngine._normalize_decimal(right[key])) > tolerance:
                return False
        return True
