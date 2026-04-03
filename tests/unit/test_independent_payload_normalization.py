from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import unittest

from aats.schemas.common import utc_now
from aats.services.strategy_engines.independent.payload_normalization import (
    normalize_independent_family_execution_summary,
    normalize_independent_payload,
    normalize_independent_replay_snapshot_payload,
    normalize_independent_runtime_state_payload,
)


class TestIndependentPayloadNormalization(unittest.TestCase):
    def test_normalize_family_execution_summary_normalizes_legacy_runtime_states_and_replay_snapshots(self) -> None:
        normalized = normalize_independent_family_execution_summary(
            family_execution_summary={
                "family": "independent",
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": "0",
                        "target_qty": "0",
                        "state": "blocked",
                        "book_state": "cooldown",
                        "book_action": "blocked",
                        "prior_book_state": "cooldown",
                        "blocked_reasons": ["independent_long_book_score_stability_below_threshold"],
                    },
                    {
                        "leg": "short",
                        "current_qty": "0.01",
                        "target_qty": "0.01",
                        "state": "blocked",
                        "book_state": "suspended",
                        "book_action": "blocked",
                        "prior_book_state": "suspended",
                        "blocked_reasons": ["independent_short_book_trial_guard_active"],
                    },
                ],
                "long_replay_snapshot": {
                    "leg": "long",
                    "state": "blocked",
                    "book_state": "cooldown",
                    "book_action": "blocked",
                    "prior_book_state": "cooldown",
                },
                "short_replay_snapshot": {
                    "leg": "short",
                    "state": "blocked",
                    "book_state": "suspended",
                    "book_action": "blocked",
                    "prior_book_state": "suspended",
                },
            }
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        long_state = normalized["book_runtime_states"][0]
        short_state = normalized["book_runtime_states"][1]
        self.assertEqual(long_state["book_state"], "flat")
        self.assertIsNone(long_state["guard_state"])
        self.assertEqual(long_state["prior_book_state"], "flat")
        self.assertIsNone(long_state["prior_guard_state"])
        self.assertEqual(short_state["book_state"], "holding")
        self.assertEqual(short_state["guard_state"], "suspended")
        self.assertEqual(short_state["prior_book_state"], "holding")
        self.assertEqual(short_state["prior_guard_state"], "suspended")
        self.assertEqual(normalized["long_replay_snapshot"]["book_state"], "flat")
        self.assertIsNone(normalized["long_replay_snapshot"]["guard_state"])
        self.assertEqual(normalized["long_replay_snapshot"]["prior_book_state"], "flat")
        self.assertIsNone(normalized["long_replay_snapshot"].get("prior_guard_state"))
        self.assertEqual(normalized["short_replay_snapshot"]["book_state"], "holding")
        self.assertEqual(normalized["short_replay_snapshot"]["guard_state"], "suspended")
        self.assertEqual(normalized["short_replay_snapshot"]["prior_book_state"], "holding")
        self.assertEqual(normalized["short_replay_snapshot"].get("prior_guard_state"), "suspended")

    def test_normalize_payload_recurses_into_decision_outcome(self) -> None:
        normalized = normalize_independent_payload(
            payload={
                "strategy_family": "independent",
                "book_runtime_states": [
                    {
                        "leg": "long",
                        "current_qty": Decimal("0"),
                        "target_qty": Decimal("0"),
                        "state": "blocked",
                        "book_state": "cooldown",
                        "book_action": "blocked",
                        "prior_book_state": "cooldown",
                    }
                ],
                "decision_outcome": {
                    "selected_strategy_family": "independent",
                    "family_execution_summary": {
                        "family": "independent",
                        "book_runtime_states": [
                            {
                                "leg": "short",
                                "current_qty": Decimal("0.01"),
                                "target_qty": Decimal("0.01"),
                                "state": "blocked",
                                "book_state": "suspended",
                                "book_action": "blocked",
                                "prior_book_state": "suspended",
                                "blocked_reasons": ["independent_short_book_trial_guard_active"],
                            }
                        ],
                    },
                },
            }
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["book_runtime_states"][0]["book_state"], "flat")
        nested_outcome = normalized["decision_outcome"]
        self.assertEqual(
            nested_outcome["family_execution_summary"]["book_runtime_states"][0]["book_state"],
            "holding",
        )
        self.assertEqual(
            nested_outcome["family_execution_summary"]["book_runtime_states"][0]["guard_state"],
            "suspended",
        )

    def test_normalize_runtime_state_payload_drops_expired_guard_horizon_by_default(self) -> None:
        normalized = normalize_independent_runtime_state_payload(
            runtime_state={
                "leg": "long",
                "current_qty": "0",
                "target_qty": "0",
                "state": "blocked",
                "book_state": "cooldown",
                "book_action": "blocked",
                "prior_book_state": "cooldown",
                "cooldown_until": utc_now() - timedelta(days=1),
            }
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["book_state"], "flat")
        self.assertIsNone(normalized["guard_state"])
        self.assertEqual(normalized["prior_book_state"], "flat")
        self.assertIsNone(normalized["prior_guard_state"])

    def test_normalize_replay_snapshot_clears_stale_prior_guard_state(self) -> None:
        normalized = normalize_independent_replay_snapshot_payload(
            replay_snapshot={
                "leg": "long",
                "state": "blocked",
                "book_state": "cooldown",
                "book_action": "blocked",
                "prior_book_state": "cooldown",
                "prior_guard_state": "cooldown",
            },
            runtime_state={
                "leg": "long",
                "current_qty": "0",
                "target_qty": "0",
                "state": "blocked",
                "book_state": "cooldown",
                "book_action": "blocked",
                "prior_book_state": "cooldown",
                "prior_guard_state": "cooldown",
                "cooldown_until": utc_now() - timedelta(days=1),
            },
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["book_state"], "flat")
        self.assertEqual(normalized["prior_book_state"], "flat")
        self.assertIsNone(normalized["guard_state"])
        self.assertIsNone(normalized["prior_guard_state"])


if __name__ == "__main__":
    unittest.main()
