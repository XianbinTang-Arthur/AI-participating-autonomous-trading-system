"""Authoritative split-runtime JetStream consumer ownership.

The derivatives four-process topology creates one durable per ``(role, topic)``.
This manifest is intentionally exact: deployment cutover evidence uses it to
distinguish current application consumers from genuinely unowned broker state;
an end-to-end assembly test proves that the runtime wiring matches it exactly.

When a new cross-process subscription is added or removed, update this file in
the same change as the bootstrap wiring and its topology tests.  Do not widen a
role to every critical topic; that would make stale or foreign durables appear
owned and would destroy the release gate's value.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

from aats.events import topics


_DECISION_TOPICS = frozenset(
    {
        topics.MARKET_SNAPSHOTS,
        topics.FEATURE_SNAPSHOTS,
        topics.ACCOUNT_SNAPSHOTS,
        topics.PORTFOLIO_SNAPSHOTS,
        topics.STRATEGY_COORDINATOR_SNAPSHOTS,
        topics.KILL_SWITCH_STATE,
        topics.GUARD_SIGNAL_UPDATES,
        topics.AI_COMMAND_REQUESTS,
        topics.FILL_EVENTS,
        topics.OBLIGATION_UPDATES,
        topics.ORDER_UPDATES,
        topics.ORDER_INTENTS,
        topics.EXECUTION_PLANS,
        topics.STRATEGY_EXECUTION_BUNDLES,
        topics.PORTFOLIO_ALLOCATION_DECISIONS,
        topics.POSITION_TARGETS,
        topics.STRATEGY_SLEEVE_INTENTS,
        topics.POLICY_DECISIONS,
        topics.RECONCILIATION_REPORTS,
        topics.RISK_DECISIONS,
        topics.AI_ASSESSMENTS,
        topics.AI_DECISION_BRIEFS,
        topics.AI_SHADOW_DECISIONS,
        topics.AI_SHADOW_EVALUATIONS,
        topics.BASELINE_ASSESSMENTS,
        topics.DECISION_CONTEXTS,
        topics.DECISION_OUTCOMES,
    }
)

_EXECUTION_TOPICS = frozenset(
    {
        topics.MARKET_SNAPSHOTS,
        topics.FEATURE_SNAPSHOTS,
        topics.ACCOUNT_SNAPSHOTS,
        topics.PORTFOLIO_SNAPSHOTS,
        topics.KILL_SWITCH_STATE,
        topics.OPERATOR_COMMAND_REQUESTS,
        topics.FILL_EVENTS,
        topics.OBLIGATION_UPDATES,
        topics.ORDER_UPDATES,
        topics.ORDER_INTENTS,
        topics.PROCESSING_FAILURES,
    }
)

_GATEWAY_TOPICS = frozenset(
    {
        topics.MARKET_SNAPSHOTS,
        topics.FEATURE_SNAPSHOTS,
        topics.ACCOUNT_SNAPSHOTS,
        topics.PORTFOLIO_SNAPSHOTS,
        topics.STRATEGY_COORDINATOR_SNAPSHOTS,
        topics.KILL_SWITCH_STATE,
        topics.GUARD_SIGNAL_UPDATES,
        topics.OPERATOR_COMMAND_RESPONSES,
        topics.AI_COMMAND_RESPONSES,
        topics.FILL_EVENTS,
        topics.OBLIGATION_UPDATES,
        topics.ORDER_UPDATES,
        topics.ORDER_INTENTS,
        topics.EXECUTION_PLANS,
        topics.POSITION_TARGETS,
        topics.POLICY_DECISIONS,
        topics.RISK_DECISIONS,
        topics.AI_ASSESSMENTS,
        topics.AI_DECISION_BRIEFS,
        topics.AI_DEGRADATION_EVENTS,
        topics.AI_PERFORMANCE_REPORTS,
        topics.AI_SHADOW_DECISIONS,
        topics.AI_SHADOW_EVALUATIONS,
        topics.BASELINE_ASSESSMENTS,
        topics.DECISION_CONTEXTS,
        topics.DECISION_OUTCOMES,
        topics.STRATEGY_PROFILE_ACTIVATIONS,
        topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
        topics.STRATEGY_PROFILE_RECOMMENDATIONS,
        topics.STRATEGY_PROFILE_REJECTIONS,
        topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
    }
)

_MARKET_TOPICS = frozenset(
    {
        topics.MARKET_SNAPSHOTS,
        topics.FEATURE_SNAPSHOTS,
        topics.ACCOUNT_SNAPSHOTS,
        topics.PORTFOLIO_SNAPSHOTS,
        topics.KILL_SWITCH_STATE,
        topics.FILL_EVENTS,
        topics.OBLIGATION_UPDATES,
        topics.ORDER_UPDATES,
    }
)


SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE: Final[Mapping[str, frozenset[str]]] = (
    MappingProxyType(
        {
            "gateway": _GATEWAY_TOPICS,
            "market": _MARKET_TOPICS,
            "decision": _DECISION_TOPICS,
            "execution": _EXECUTION_TOPICS,
        }
    )
)


def split_runtime_consumer_topics(role: str) -> frozenset[str]:
    """Return the exact declared JetStream topics for one split role."""

    try:
        return SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE[role]
    except KeyError as exc:
        raise ValueError("unknown_split_runtime_consumer_role") from exc
