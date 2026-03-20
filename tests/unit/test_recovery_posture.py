from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import RecoveryStatus
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator


class TestRecoveryPostureEvaluator(unittest.IsolatedAsyncioTestCase):
    async def test_paper_local_finalize_keeps_recovery_lightweight(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        final = evaluator.finalize_status()

        self.assertEqual(final.recovery_state, "normal_operation")
        self.assertTrue(final.resume_eligible)
        self.assertTrue(final.safe_to_trade)
        self.assertFalse(final.rebaseline_available)
        self.assertEqual(final.resume_blocked_reasons, [])

    async def test_exchange_simulated_finalize_marks_review_required(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "config_profile": "guarded_simulated_submit_dry_run",
                    "mode": "guarded_live",
                    "market_data_backend": "demo",
                    "execution_backend": "okx",
                    "account_backend": "okx",
                    "account_read_enabled": True,
                    "okx_simulated_trading": True,
                    "live_submit_enabled": False,
                    "guarded_execution_dry_run": True,
                    "bootstrap_portfolio_from_exchange": True,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        evaluator = RecoveryPostureEvaluator(runtime)
        report = ReconciliationReport(
            reconciliation_id="recon_test",
            as_of_ts=utc_now(),
            exchange_comparison_enabled=True,
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={},
            mismatch_categories=["fills"],
            mismatch_reasons=["unexpected_on_exchange_fill"],
            safety_impacts=["operator_review_required"],
            severity="WARN",
            review_required=True,
            recommended_operator_action="rebaseline",
            halt_required=False,
        )
        base_status = RecoveryStatus(status="review", recovery_state="normal_operation")

        final = evaluator.finalize_status(base_status=base_status, latest_reconciliation=report)

        self.assertEqual(final.recovery_state, "review_required")
        self.assertFalse(final.safe_to_trade)
        self.assertFalse(final.resume_eligible)
        self.assertTrue(final.rebaseline_available)
        self.assertTrue(final.recovered_reconciliation_available)
        self.assertEqual(final.latest_reconciliation_id, "recon_test")
        self.assertEqual(final.latest_reconciliation_severity, "WARN")
        self.assertIn("operator_rebaseline_required", final.resume_blocked_reasons)

    async def test_manual_halt_stays_resume_eligible_when_no_other_blockers(self) -> None:
        runtime = await build_runtime(
            AATSSettings.model_validate(
                {
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "memory",
                    "event_persistence_mode": "strict",
                }
            )
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=runtime.settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.kill_switch.halt(reason="operator_test_halt")
        evaluator = RecoveryPostureEvaluator(runtime)

        final = evaluator.finalize_status()

        self.assertEqual(final.recovery_state, "manually_halted")
        self.assertTrue(final.resume_eligible)
        self.assertFalse(final.safe_to_trade)
        self.assertEqual(final.resume_blocked_reasons, [])


if __name__ == "__main__":
    unittest.main()
