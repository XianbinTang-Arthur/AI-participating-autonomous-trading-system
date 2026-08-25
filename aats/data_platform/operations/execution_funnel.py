"""Fail-closed evidence for the derivatives simulation execution funnel.

The evaluator consumes already-read database rows.  It deliberately keeps
database access and credential handling in the CLI layer, which makes the
financial/control-flow rules deterministic and unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from aats.events import topics


EXECUTION_FUNNEL_SCHEMA_VERSION = "simulation_execution_funnel_v1"
EXECUTION_FUNNEL_TOPICS = (
    topics.PORTFOLIO_ALLOCATION_DECISIONS,
    topics.POSITION_TARGETS,
    topics.POLICY_DECISIONS,
    topics.RISK_DECISIONS,
    topics.EXECUTION_PLANS,
    topics.ORDER_INTENTS,
    topics.ORDER_UPDATES,
    topics.FILL_EVENTS,
)
SIZING_REJECTION_REASONS = frozenset(
    {
        "max_pending_notional_per_symbol_exceeded",
        "max_total_open_notional_exceeded",
        "max_gross_notional_per_symbol_exceeded",
        "risk_max_long_notional_exceeded",
        "risk_max_short_notional_exceeded",
        "risk_max_gross_notional_exceeded",
        "risk_max_net_notional_exceeded",
    }
)
_EPSILON = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class SimulationDeploymentIdentity:
    profile: str
    deployed_commit: str
    runtime_readiness_generation: str
    generated_at: datetime
    deployment_evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class FunnelDecisionObservation:
    decision_id: str
    target_event_id: str
    target_created_at: datetime
    current_position_qty: Decimal
    target_position_qty: Decimal
    delta_position_qty: Decimal
    target_notional: Decimal
    new_risk: bool
    stages: Mapping[str, bool]
    risk_approved: bool | None
    risk_rejection_reasons: tuple[str, ...]
    order_count: int
    fill_count: int
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["target_created_at"] = self.target_created_at.astimezone(UTC).isoformat()
        for name in (
            "current_position_qty",
            "target_position_qty",
            "delta_position_qty",
            "target_notional",
        ):
            payload[name] = str(payload[name])
        payload["stages"] = dict(self.stages)
        payload["risk_rejection_reasons"] = list(self.risk_rejection_reasons)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


@dataclass(frozen=True, slots=True)
class SimulationExecutionFunnelEvidence:
    evaluated_at: datetime
    deployment: SimulationDeploymentIdentity
    window_start: datetime
    window_end: datetime
    symbol: str
    max_new_risk_notional: Decimal
    min_nonzero_targets: int
    settle_delay_seconds: int
    event_counts: Mapping[str, int]
    order_count: int
    fill_count: int
    order_state_counts: Mapping[str, int]
    mature_nonzero_target_count: int
    immature_nonzero_target_count: int
    oversized_new_risk_target_count: int
    sizing_rejection_count: int
    risk_rejection_reason_counts: Mapping[str, int]
    decisions: tuple[FunnelDecisionObservation, ...]
    status: str
    passed: bool
    reason_codes: tuple[str, ...]
    evidence_fingerprint: str
    schema_version: str = EXECUTION_FUNNEL_SCHEMA_VERSION
    production_ready: bool = False
    trading_ready: bool = False
    authorization_boundary: str = (
        "simulation evidence only; no runtime mutation, order submission, "
        "live deployment or funds authorization"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "deployment": {
                "profile": self.deployment.profile,
                "deployed_commit": self.deployment.deployed_commit,
                "runtime_readiness_generation": (
                    self.deployment.runtime_readiness_generation
                ),
                "generated_at": self.deployment.generated_at.astimezone(UTC).isoformat(),
                "deployment_evidence_fingerprint": (
                    self.deployment.deployment_evidence_fingerprint
                ),
            },
            "window_start": self.window_start.astimezone(UTC).isoformat(),
            "window_end": self.window_end.astimezone(UTC).isoformat(),
            "symbol": self.symbol,
            "max_new_risk_notional": str(self.max_new_risk_notional),
            "min_nonzero_targets": self.min_nonzero_targets,
            "settle_delay_seconds": self.settle_delay_seconds,
            "event_counts": dict(self.event_counts),
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "order_state_counts": dict(self.order_state_counts),
            "mature_nonzero_target_count": self.mature_nonzero_target_count,
            "immature_nonzero_target_count": self.immature_nonzero_target_count,
            "oversized_new_risk_target_count": self.oversized_new_risk_target_count,
            "sizing_rejection_count": self.sizing_rejection_count,
            "risk_rejection_reason_counts": dict(self.risk_rejection_reason_counts),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "status": self.status,
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "evidence_fingerprint": self.evidence_fingerprint,
            "production_ready": self.production_ready,
            "trading_ready": self.trading_ready,
            "authorization_boundary": self.authorization_boundary,
        }


def parse_simulation_deployment_identity(
    payload: Mapping[str, Any],
    *,
    evidence_fingerprint: str,
) -> SimulationDeploymentIdentity:
    """Validate and reduce a standard simulation deployment evidence packet."""

    if payload.get("profile") != "derivatives":
        raise ValueError("deployment_profile_must_be_derivatives")
    if payload.get("status") != "simulation_stack_healthy":
        raise ValueError("deployment_status_not_simulation_stack_healthy")
    if payload.get("production_ready") is not False:
        raise ValueError("deployment_production_ready_must_be_false")
    if payload.get("trading_ready") is not False:
        raise ValueError("deployment_trading_ready_must_be_false")
    deployed_commit = _required_text(payload.get("deployed_commit"), "deployed_commit")
    generation = _required_text(
        payload.get("runtime_readiness_generation"),
        "runtime_readiness_generation",
    )
    generated_at = _aware_datetime(payload.get("generated_at"), "generated_at")
    normalized_fingerprint = _required_text(
        evidence_fingerprint,
        "deployment_evidence_fingerprint",
    )
    if len(normalized_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_fingerprint.lower()
    ):
        raise ValueError("deployment_evidence_fingerprint_invalid")
    return SimulationDeploymentIdentity(
        profile="derivatives",
        deployed_commit=deployed_commit,
        runtime_readiness_generation=generation,
        generated_at=generated_at,
        deployment_evidence_fingerprint=normalized_fingerprint.lower(),
    )


def evaluate_simulation_execution_funnel(
    *,
    deployment: SimulationDeploymentIdentity,
    window_end: datetime,
    symbol: str,
    max_new_risk_notional: Decimal,
    min_nonzero_targets: int,
    settle_delay_seconds: int,
    event_rows: Sequence[Mapping[str, Any]],
    order_rows: Sequence[Mapping[str, Any]],
    fill_rows: Sequence[Mapping[str, Any]],
    evaluated_at: datetime | None = None,
) -> SimulationExecutionFunnelEvidence:
    """Evaluate a bounded, deployment-scoped simulation event window."""

    normalized_window_end = _aware_datetime(window_end, "window_end")
    normalized_evaluated_at = _aware_datetime(
        evaluated_at or datetime.now(UTC),
        "evaluated_at",
    )
    if normalized_window_end <= deployment.generated_at:
        raise ValueError("execution_funnel_window_must_follow_deployment")
    if normalized_window_end > normalized_evaluated_at + timedelta(seconds=5):
        raise ValueError("execution_funnel_window_end_in_future")
    normalized_symbol = _required_text(symbol, "symbol")
    normalized_cap = _required_decimal(
        max_new_risk_notional,
        "max_new_risk_notional",
    )
    if normalized_cap <= _EPSILON:
        raise ValueError("max_new_risk_notional_must_be_positive")
    if min_nonzero_targets <= 0:
        raise ValueError("min_nonzero_targets_must_be_positive")
    if settle_delay_seconds < 0:
        raise ValueError("settle_delay_seconds_must_be_non_negative")

    topic_counts: Counter[str] = Counter()
    events_by_decision: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    target_rows: list[Mapping[str, Any]] = []
    for row in event_rows:
        topic = _required_text(row.get("topic"), "event_topic")
        if topic not in EXECUTION_FUNNEL_TOPICS:
            raise ValueError("unexpected_execution_funnel_topic")
        topic_counts[topic] += 1
        decision_id = str(row.get("decision_id") or "").strip()
        if decision_id:
            events_by_decision[decision_id][topic].append(row)
        if topic == topics.POSITION_TARGETS:
            target_rows.append(row)

    orders_by_decision: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    order_ids: set[str] = set()
    order_state_counts: Counter[str] = Counter()
    for row in order_rows:
        order_id = _required_text(row.get("order_id"), "order_id")
        decision_id = _required_text(row.get("decision_id"), "order_decision_id")
        order_ids.add(order_id)
        orders_by_decision[decision_id].append(row)
        order_state_counts[_required_text(row.get("state"), "order_state")] += 1

    fills_by_decision: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    global_reasons: set[str] = set()
    for row in fill_rows:
        order_id = _required_text(row.get("order_id"), "fill_order_id")
        decision_id = _required_text(row.get("decision_id"), "fill_decision_id")
        fills_by_decision[decision_id].append(row)
        if order_id not in order_ids:
            global_reasons.add("orphan_fill_without_window_order")

    mature_cutoff = normalized_window_end - timedelta(seconds=settle_delay_seconds)
    observations: list[FunnelDecisionObservation] = []
    immature_nonzero_count = 0
    oversized_count = 0
    sizing_rejection_count = 0
    rejection_reason_counts: Counter[str] = Counter()
    observed_target_decisions: set[str] = set()

    for target_row in target_rows:
        target_payload = _payload_mapping(target_row)
        delta_qty = _required_decimal(
            target_payload.get("delta_position_qty"),
            "delta_position_qty",
        )
        if abs(delta_qty) <= _EPSILON:
            continue
        decision_id = _required_text(target_row.get("decision_id"), "target_decision_id")
        if decision_id in observed_target_decisions:
            global_reasons.add("duplicate_nonzero_target_for_decision")
            continue
        observed_target_decisions.add(decision_id)
        target_created_at = _aware_datetime(
            target_row.get("created_at"),
            "target_created_at",
        )
        if target_created_at > mature_cutoff:
            immature_nonzero_count += 1
            continue
        event_id = _required_text(target_row.get("event_id"), "target_event_id")
        current_qty = _required_decimal(
            target_payload.get("current_position_qty"),
            "current_position_qty",
        )
        target_qty = _required_decimal(
            target_payload.get("target_position_qty"),
            "target_position_qty",
        )
        target_notional = abs(
            _required_decimal(target_payload.get("target_notional"), "target_notional")
        )
        new_risk = _is_new_risk(current_qty=current_qty, target_qty=target_qty)
        by_topic = events_by_decision[decision_id]
        stage_presence = {
            "allocation": bool(by_topic.get(topics.PORTFOLIO_ALLOCATION_DECISIONS)),
            "target": True,
            "policy": bool(by_topic.get(topics.POLICY_DECISIONS)),
            "risk": bool(by_topic.get(topics.RISK_DECISIONS)),
            "plan": bool(by_topic.get(topics.EXECUTION_PLANS)),
            "intent": bool(by_topic.get(topics.ORDER_INTENTS)),
            "order": bool(orders_by_decision.get(decision_id)),
            "fill": bool(fills_by_decision.get(decision_id)),
        }
        decision_reasons: set[str] = set()
        for stage in ("allocation", "policy", "risk"):
            if not stage_presence[stage]:
                decision_reasons.add(f"{stage}_stage_missing")

        risk_approved: bool | None = None
        risk_reasons: tuple[str, ...] = ()
        risk_rows = by_topic.get(topics.RISK_DECISIONS, [])
        if risk_rows:
            risk_payload = _payload_mapping(risk_rows[-1])
            risk_approved = _required_bool(risk_payload.get("approved"), "risk_approved")
            risk_reasons = _reason_codes(risk_payload.get("rejection_reasons"))
            rejection_reason_counts.update(risk_reasons)
            if not risk_approved and SIZING_REJECTION_REASONS.intersection(risk_reasons):
                sizing_rejection_count += 1
                decision_reasons.add("sizing_risk_rejection_observed")
            if risk_approved:
                for stage in ("plan", "intent", "order"):
                    if not stage_presence[stage]:
                        decision_reasons.add(f"approved_risk_{stage}_stage_missing")
            elif stage_presence["order"]:
                decision_reasons.add("order_observed_after_risk_rejection")

        if new_risk and target_notional > normalized_cap + _EPSILON:
            oversized_count += 1
            decision_reasons.add("new_risk_target_notional_above_cap")

        observations.append(
            FunnelDecisionObservation(
                decision_id=decision_id,
                target_event_id=event_id,
                target_created_at=target_created_at,
                current_position_qty=current_qty,
                target_position_qty=target_qty,
                delta_position_qty=delta_qty,
                target_notional=target_notional,
                new_risk=new_risk,
                stages=stage_presence,
                risk_approved=risk_approved,
                risk_rejection_reasons=risk_reasons,
                order_count=len(orders_by_decision.get(decision_id, [])),
                fill_count=len(fills_by_decision.get(decision_id, [])),
                reason_codes=tuple(sorted(decision_reasons)),
            )
        )
        global_reasons.update(decision_reasons)

    observations.sort(key=lambda item: (item.target_created_at, item.decision_id))
    if not observations:
        global_reasons.add("nonzero_target_observation_missing")
    if len(observations) < min_nonzero_targets:
        global_reasons.add("minimum_nonzero_target_observation_not_met")

    ordered_reasons = tuple(sorted(global_reasons))
    observation_reasons = {
        "nonzero_target_observation_missing",
        "minimum_nonzero_target_observation_not_met",
    }
    hard_reasons = set(ordered_reasons) - observation_reasons
    if hard_reasons:
        status = "FAIL"
    elif observation_reasons.intersection(ordered_reasons):
        status = "UNKNOWN"
    else:
        status = "PASS"
    passed = status == "PASS"

    normalized_event_counts = {
        topic: int(topic_counts.get(topic, 0)) for topic in EXECUTION_FUNNEL_TOPICS
    }
    normalized_order_states = dict(sorted(order_state_counts.items()))
    normalized_rejection_counts = dict(sorted(rejection_reason_counts.items()))
    fingerprint_payload = {
        "schema_version": EXECUTION_FUNNEL_SCHEMA_VERSION,
        "deployment": {
            "profile": deployment.profile,
            "deployed_commit": deployment.deployed_commit,
            "runtime_readiness_generation": deployment.runtime_readiness_generation,
            "generated_at": deployment.generated_at.astimezone(UTC).isoformat(),
            "deployment_evidence_fingerprint": deployment.deployment_evidence_fingerprint,
        },
        "window_start": deployment.generated_at.astimezone(UTC).isoformat(),
        "window_end": normalized_window_end.astimezone(UTC).isoformat(),
        "symbol": normalized_symbol,
        "max_new_risk_notional": str(normalized_cap),
        "min_nonzero_targets": min_nonzero_targets,
        "settle_delay_seconds": settle_delay_seconds,
        "event_counts": normalized_event_counts,
        "order_count": len(order_rows),
        "fill_count": len(fill_rows),
        "order_state_counts": normalized_order_states,
        "mature_nonzero_target_count": len(observations),
        "immature_nonzero_target_count": immature_nonzero_count,
        "oversized_new_risk_target_count": oversized_count,
        "sizing_rejection_count": sizing_rejection_count,
        "risk_rejection_reason_counts": normalized_rejection_counts,
        "decisions": [observation.to_dict() for observation in observations],
        "status": status,
        "passed": passed,
        "reason_codes": list(ordered_reasons),
        "production_ready": False,
        "trading_ready": False,
    }
    fingerprint = "funnel_" + hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SimulationExecutionFunnelEvidence(
        evaluated_at=normalized_evaluated_at,
        deployment=deployment,
        window_start=deployment.generated_at,
        window_end=normalized_window_end,
        symbol=normalized_symbol,
        max_new_risk_notional=normalized_cap,
        min_nonzero_targets=min_nonzero_targets,
        settle_delay_seconds=settle_delay_seconds,
        event_counts=normalized_event_counts,
        order_count=len(order_rows),
        fill_count=len(fill_rows),
        order_state_counts=normalized_order_states,
        mature_nonzero_target_count=len(observations),
        immature_nonzero_target_count=immature_nonzero_count,
        oversized_new_risk_target_count=oversized_count,
        sizing_rejection_count=sizing_rejection_count,
        risk_rejection_reason_counts=normalized_rejection_counts,
        decisions=tuple(observations),
        status=status,
        passed=passed,
        reason_codes=ordered_reasons,
        evidence_fingerprint=fingerprint,
    )


def _payload_mapping(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        raise ValueError("event_payload_must_be_mapping")
    return payload


def _reason_codes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("risk_rejection_reasons_must_be_list")
    return tuple(sorted({_required_text(item, "risk_rejection_reason") for item in value}))


def _is_new_risk(*, current_qty: Decimal, target_qty: Decimal) -> bool:
    if abs(target_qty) <= _EPSILON:
        return False
    if abs(current_qty) <= _EPSILON:
        return True
    if current_qty * target_qty < Decimal("0"):
        return True
    return abs(target_qty) > abs(current_qty) + _EPSILON


def _required_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name}_required")
    return normalized


def _required_decimal(value: Any, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}_invalid") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return normalized


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name}_must_be_bool")
    return value


def _aware_datetime(value: Any, field_name: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return parsed.astimezone(UTC)
