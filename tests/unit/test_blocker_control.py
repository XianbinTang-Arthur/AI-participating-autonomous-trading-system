from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.services.blocker_control.service import BlockerControlService


class TestBlockerControlSummary(unittest.TestCase):
    def test_next_step_summary_explains_review_without_primary_blocker(self) -> None:
        summary = BlockerControlService._next_step_summary(  # type: ignore[attr-defined]
            None,
            [],
            recovery={
                "safe_to_trade": False,
                "review_required": True,
                "resume_eligible": False,
                "halted": True,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=None,
        )

        self.assertIn("人工确认流程", summary)

    def test_next_step_summary_explains_observational_drift_without_primary_blocker(self) -> None:
        summary = BlockerControlService._next_step_summary(  # type: ignore[attr-defined]
            None,
            [],
            recovery={
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=SimpleNamespace(observational_only=True),
        )

        self.assertIn("轻度动态漂移", summary)


if __name__ == "__main__":
    unittest.main()
