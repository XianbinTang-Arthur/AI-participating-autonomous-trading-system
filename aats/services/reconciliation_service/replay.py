from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from aats.events import topics
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import EventEnvelope
from aats.schemas.decision import DecisionContext, PositionTarget
from aats.schemas.execution import ExecutionPlan, FillEvent, OrderIntent, OrderState
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import HealthSnapshot
from aats.storage.base import AuditRepository, EventStore, PortfolioRepository
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService


@dataclass(slots=True)
class ReplayResult:
    replayed_event_count: int
    stored_snapshot_count: int
    divergence_count: int
    divergences: list[str] = field(default_factory=list)
    portfolio_issues: list[str] = field(default_factory=list)
    decision_chain_issues: list[str] = field(default_factory=list)
    execution_chain_issues: list[str] = field(default_factory=list)
    audit_issues: list[str] = field(default_factory=list)
    final_reconstructed_snapshot: PortfolioSnapshot | None = None
    final_stored_snapshot: PortfolioSnapshot | None = None
    decision_chains: dict[str, DecisionAuditRecord] = field(default_factory=dict)


class ReplayEngine:
    def __init__(
        self,
        *,
        event_store: EventStore,
        reconstruction_service: PortfolioReconstructionService,
        audit_repo: AuditRepository | None = None,
        portfolio_repo: PortfolioRepository | None = None,
    ) -> None:
        self.event_store = event_store
        self.reconstruction_service = reconstruction_service
        self.audit_repo = audit_repo
        self.portfolio_repo = portfolio_repo

    def replay(self) -> ReplayResult:
        latest_prices: dict[str, float] = {}
        processed_fills: list[FillEvent] = []
        decision_chains: dict[str, DecisionAuditRecord] = {}
        decision_ids: set[str] = set()
        portfolio_issues: list[str] = []
        decision_chain_issues: list[str] = []
        execution_chain_issues: list[str] = []
        audit_issues: list[str] = []
        stored_snapshot_count = 0
        final_stored_snapshot: PortfolioSnapshot | None = None
        events = self.event_store.all()
        events_by_id = {event.event_id: event for event in events}
        events_by_decision: dict[str, list[EventEnvelope]] = defaultdict(list)
        order_intents_by_intent_id: dict[str, OrderIntent] = {}
        order_states_by_intent_id: dict[str, OrderState] = {}
        fill_events_by_fill_id: dict[str, FillEvent] = {}
        fill_event_refs_by_fill_id: dict[str, list[str]] = defaultdict(list)
        audit_event_ids_by_decision: dict[str, str] = {}

        for envelope in events:
            decision_id = envelope.payload.get("decision_id")
            if isinstance(decision_id, str):
                decision_ids.add(decision_id)
                events_by_decision[decision_id].append(envelope)

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

            if envelope.topic == topics.PORTFOLIO_SNAPSHOTS:
                stored_snapshot_count += 1
                stored_snapshot = PortfolioSnapshot.model_validate(envelope.payload)
                final_stored_snapshot = stored_snapshot
                reconstructed_snapshot = self.reconstruction_service.rebuild_snapshot(
                    fills=processed_fills,
                    price_provider=lambda symbol: latest_prices.get(symbol, 0.0),
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
            final_reconstructed_snapshot = self.reconstruction_service.rebuild_snapshot(
                fills=processed_fills,
                price_provider=lambda symbol: latest_prices.get(symbol, 0.0),
            )

        execution_chain_issues.extend(
            self._validate_execution_chain(
                order_intents_by_intent_id=order_intents_by_intent_id,
                order_states_by_intent_id=order_states_by_intent_id,
                fill_events_by_fill_id=fill_events_by_fill_id,
                fill_event_refs_by_fill_id=fill_event_refs_by_fill_id,
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

        if self.portfolio_repo is not None and final_stored_snapshot is not None:
            repo_latest = self.portfolio_repo.latest()
            if repo_latest is None:
                portfolio_issues.append("portfolio_repository_missing_latest_snapshot")
            elif self._snapshot_signature(repo_latest) != self._snapshot_signature(final_stored_snapshot):
                portfolio_issues.append("portfolio_repository_latest_snapshot_mismatch")

        if self.audit_repo is not None:
            audit_issues.extend(
                self._validate_audit_repository(
                    decision_chains=decision_chains,
                    audit_event_ids_by_decision=audit_event_ids_by_decision,
                )
            )

        divergences = [
            *portfolio_issues,
            *decision_chain_issues,
            *execution_chain_issues,
            *audit_issues,
        ]
        return ReplayResult(
            replayed_event_count=len(events),
            stored_snapshot_count=stored_snapshot_count,
            divergence_count=len(divergences),
            divergences=divergences,
            portfolio_issues=portfolio_issues,
            decision_chain_issues=decision_chain_issues,
            execution_chain_issues=execution_chain_issues,
            audit_issues=audit_issues,
            final_reconstructed_snapshot=final_reconstructed_snapshot,
            final_stored_snapshot=final_stored_snapshot,
            decision_chains=decision_chains,
        )

    def _validate_execution_chain(
        self,
        *,
        order_intents_by_intent_id: dict[str, OrderIntent],
        order_states_by_intent_id: dict[str, OrderState],
        fill_events_by_fill_id: dict[str, FillEvent],
        fill_event_refs_by_fill_id: dict[str, list[str]],
    ) -> list[str]:
        issues: list[str] = []
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

        for fill_id, event_refs in sorted(fill_event_refs_by_fill_id.items()):
            if len(event_refs) > 1:
                issues.append(
                    f"execution_chain_duplicate_fill_events fill_id={fill_id} event_refs={event_refs}"
                )
        return issues

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
                ref=record.ai_market_assessment_ref,
                ref_name="ai_market_assessment_ref",
                expected_topic=topics.AI_ASSESSMENTS,
                required=False,
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

            target = (
                PositionTarget.model_validate(target_event.payload)
                if target_event is not None
                else None
            )
            policy = (
                PolicyDecision.model_validate(policy_event.payload)
                if policy_event is not None
                else None
            )
            risk = RiskDecision.model_validate(risk_event.payload) if risk_event is not None else None

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
            if risk is not None and (not risk.approved or risk.halt_required) and record.order_intent_refs:
                issues.append(
                    f"decision_chain_risk_blocked_but_intent_emitted decision_id={decision_id}"
                )
            if (
                target is not None
                and abs(target.delta_position_qty) < 1e-12
                and record.order_intent_refs
            ):
                issues.append(
                    f"decision_chain_zero_delta_but_intent_emitted decision_id={decision_id}"
                )
            if record.order_intent_refs and not record.fill_event_refs:
                issues.append(f"decision_chain_missing_fill_ref decision_id={decision_id}")
            if record.order_intent_refs:
                execution_plan_events = [
                    event
                    for event in events_by_decision.get(decision_id, [])
                    if event.topic == topics.EXECUTION_PLANS
                ]
                if not execution_plan_events:
                    issues.append(f"decision_chain_missing_execution_plan decision_id={decision_id}")
                else:
                    latest_plan = ExecutionPlan.model_validate(execution_plan_events[-1].payload)
                    if parsed_context is not None and latest_plan.symbol != parsed_context.symbol:
                        issues.append(
                            "decision_chain_execution_plan_symbol_mismatch "
                            f"decision_id={decision_id} execution_plan_symbol={latest_plan.symbol} "
                            f"context_symbol={parsed_context.symbol}"
                        )
                    if (
                        risk is not None
                        and abs(
                            latest_plan.approved_target_position_qty - risk.capped_target_position_qty
                        ) > 1e-12
                    ):
                        issues.append(
                            "decision_chain_execution_plan_risk_mismatch "
                            f"decision_id={decision_id} approved_target_position_qty={latest_plan.approved_target_position_qty} "
                            f"risk_capped_target_position_qty={risk.capped_target_position_qty}"
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
                ("ai_market_assessment_ref", record.ai_market_assessment_ref),
                ("position_target_ref", record.position_target_ref),
                ("policy_decision_ref", record.policy_decision_ref),
                ("risk_decision_ref", record.risk_decision_ref),
                ("portfolio_delta_ref", record.portfolio_delta_ref),
            ):
                if ref_value is not None and ref_value not in events_by_id:
                    issues.append(
                        f"audit_ref_missing decision_id={decision_id} ref_name={ref_name} ref={ref_value}"
                    )
            for ref_name, refs in (
                ("order_intent_refs", record.order_intent_refs),
                ("fill_event_refs", record.fill_event_refs),
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
    ) -> list[str]:
        issues: list[str] = []
        if self.audit_repo is None:
            return issues

        for record in self.audit_repo.all():
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
            "balances": snapshot.balances,
            "positions": {position.symbol: position.position_qty for position in snapshot.positions},
            "cost_basis": snapshot.cost_basis,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "total_equity": snapshot.total_equity,
            "gross_exposure": snapshot.gross_exposure,
            "net_exposure": snapshot.net_exposure,
        }

    @staticmethod
    def _snapshot_mismatch(
        *,
        stored_snapshot: PortfolioSnapshot,
        reconstructed_snapshot: PortfolioSnapshot,
    ) -> dict[str, object]:
        mismatch: dict[str, object] = {}
        if stored_snapshot.balances != reconstructed_snapshot.balances:
            mismatch["balances"] = {
                "stored": stored_snapshot.balances,
                "reconstructed": reconstructed_snapshot.balances,
            }
        if stored_snapshot.cost_basis != reconstructed_snapshot.cost_basis:
            mismatch["cost_basis"] = {
                "stored": stored_snapshot.cost_basis,
                "reconstructed": reconstructed_snapshot.cost_basis,
            }

        stored_positions = {position.symbol: position.position_qty for position in stored_snapshot.positions}
        replayed_positions = {
            position.symbol: position.position_qty for position in reconstructed_snapshot.positions
        }
        if stored_positions != replayed_positions:
            mismatch["positions"] = {
                "stored": stored_positions,
                "reconstructed": replayed_positions,
            }

        numeric_fields = (
            "realized_pnl",
            "unrealized_pnl",
            "total_equity",
            "gross_exposure",
            "net_exposure",
        )
        for field_name in numeric_fields:
            if abs(getattr(stored_snapshot, field_name) - getattr(reconstructed_snapshot, field_name)) > 1e-9:
                mismatch[field_name] = {
                    "stored": getattr(stored_snapshot, field_name),
                    "reconstructed": getattr(reconstructed_snapshot, field_name),
                }

        return mismatch
