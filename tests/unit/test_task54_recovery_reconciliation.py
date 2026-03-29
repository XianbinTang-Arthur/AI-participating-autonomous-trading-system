from __future__ import annotations

import unittest

from aats.schemas.common import utc_now
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.recovery_control import RecoveryReconciliationClassifier


class TestTask54RecoveryReconciliation(unittest.TestCase):
    def test_classifier_marks_local_projection_gap_as_auto_repairable(self) -> None:
        classifier = RecoveryReconciliationClassifier()
        report = ReconciliationReport(
            reconciliation_id="recon_projection_only",
            as_of_ts=utc_now(),
            exchange_comparison_enabled=False,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {"USDT": {"stored": "1", "reconstructed": "2"}}, "exchange": {}},
            position_diff={
                "stored": {},
                "reconstructed": {},
                "reconstructed_mismatches": {},
                "exchange": {},
                "exchange_mismatches": {},
            },
            mismatch_categories=["unsafe_unknown_state"],
            mismatch_reasons=["stored_balance_differs_from_replayed_balance"],
            safety_impacts=[],
            severity="SOFT_MISMATCH",
        )

        annotated = classifier.annotate(report)

        self.assertEqual(annotated.recovery_classification, "projection_rebuild_required")
        self.assertTrue(annotated.auto_repairable)
        self.assertFalse(annotated.resume_blocking)
        self.assertFalse(annotated.review_required)
        self.assertFalse(annotated.halt_required)

    def test_classifier_marks_exchange_soft_mismatch_as_non_blocking_continue(self) -> None:
        classifier = RecoveryReconciliationClassifier()
        report = ReconciliationReport(
            reconciliation_id="recon_exchange_soft",
            as_of_ts=utc_now(),
            exchange_comparison_enabled=True,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {"unexpected_on_exchange": ["fill_external"]}},
            balance_diff={"reconstructed": {}, "exchange": {"USDT": {"stored": "1000", "exchange": "900"}}},
            position_diff={
                "stored": {},
                "reconstructed": {},
                "reconstructed_mismatches": {},
                "exchange": {},
                "exchange_mismatches": {},
            },
            mismatch_categories=["external_manual_activity_detected"],
            mismatch_reasons=["local_balance_differs_from_exchange_balance"],
            safety_impacts=["exchange_account_state_differs_from_local_state"],
            severity="SOFT_MISMATCH",
        )

        annotated = classifier.annotate(report)

        self.assertEqual(annotated.recovery_classification, "soft_divergence_continue")
        self.assertFalse(annotated.auto_repairable)
        self.assertFalse(annotated.resume_blocking)
        self.assertFalse(annotated.review_required)
        self.assertFalse(annotated.halt_required)
        self.assertEqual(annotated.recommended_operator_action, "investigate_state_divergence")

    def test_classifier_marks_unknown_derivatives_position_as_manual_review_even_when_only_reduce_is_active(self) -> None:
        classifier = RecoveryReconciliationClassifier()
        report = ReconciliationReport(
            reconciliation_id="recon_derivatives_only_reduce",
            as_of_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
            exchange_comparison_enabled=True,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {}, "exchange": {"USDT": {"stored": "1000", "exchange": "950"}}},
            position_diff={
                "stored": {},
                "reconstructed": {},
                "reconstructed_mismatches": {},
                "exchange": {"BTC-USDT-SWAP": "0.01"},
                "exchange_mismatches": {"BTC-USDT-SWAP": {"stored": "0", "exchange": "0.01"}},
            },
            mismatch_categories=[
                "external_manual_activity_detected",
                "local_balance_divergence",
                "local_position_divergence",
                "derivatives_exchange_position_without_local_execution_chain",
            ],
            mismatch_reasons=[
                "local_balance_differs_from_exchange_balance",
                "local_position_differs_from_exchange_position",
                "derivatives_exchange_position_not_replayed_locally",
            ],
            safety_impacts=[
                "exchange_account_state_differs_from_local_state",
                "derivatives_only_reduce_until_position_reconciled",
            ],
            severity="REVIEW_REQUIRED",
            review_required=True,
            only_reduce_required=True,
            only_reduce_reasons=["derivatives_exchange_position_without_local_execution_chain"],
            recommended_operator_action="go_close_position_on_exchange",
        )

        annotated = classifier.annotate(report)

        self.assertEqual(annotated.recovery_classification, "manual_review_required")
        self.assertFalse(annotated.auto_repairable)
        self.assertTrue(annotated.resume_blocking)
        self.assertTrue(annotated.review_required)
        self.assertFalse(annotated.halt_required)
        self.assertEqual(annotated.recommended_operator_action, "go_close_position_on_exchange")


if __name__ == "__main__":
    unittest.main()
