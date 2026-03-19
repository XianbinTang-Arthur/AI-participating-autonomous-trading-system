from __future__ import annotations

import unittest

from aats.services.execution_engine.okx_bills import explain_okx_bills_for_reconciliation


class OKXBillsExplanationTests(unittest.TestCase):
    def test_trade_execution_with_open_order_divergence_suggests_exchange_cancel(self) -> None:
        explanations = explain_okx_bills_for_reconciliation(
            summary={
                "top_categories": [{"type": "2", "sub_type": "3", "currency": "USDT", "count": 1}],
            },
            mismatch_categories=["local_open_order_divergence", "exchange_bills_activity_available"],
            mismatch_reasons=["local_open_orders_diverge_from_exchange_open_orders"],
        )

        self.assertEqual(explanations[0]["operator_case"], "open_order_unsettled")
        self.assertEqual(explanations[0]["operator_action"], "go_cancel_on_exchange")

    def test_trade_execution_with_position_divergence_suggests_flattening_position(self) -> None:
        explanations = explain_okx_bills_for_reconciliation(
            summary={
                "top_categories": [{"type": "2", "sub_type": "4", "currency": "USDT", "count": 1}],
            },
            mismatch_categories=["local_position_divergence", "exchange_bills_activity_available"],
            mismatch_reasons=["local_position_differs_from_exchange_position"],
        )

        self.assertEqual(explanations[0]["operator_case"], "position_drift")
        self.assertEqual(explanations[0]["operator_action"], "go_close_position_on_exchange")

    def test_transfer_or_margin_movement_suggests_rebaseline(self) -> None:
        explanations = explain_okx_bills_for_reconciliation(
            summary={
                "top_categories": [{"type": "1", "sub_type": "201", "currency": "USDT", "count": 1}],
            },
            mismatch_categories=["external_manual_activity_detected", "local_balance_divergence"],
            mismatch_reasons=["local_balance_differs_from_exchange_balance"],
        )

        self.assertEqual(explanations[0]["operator_case"], "fund_transfer")
        self.assertEqual(explanations[0]["operator_action"], "confirm_and_rebaseline")

    def test_manual_trade_activity_suggests_confirm_and_rebaseline(self) -> None:
        explanations = explain_okx_bills_for_reconciliation(
            summary={
                "top_categories": [{"type": "2", "sub_type": "1", "currency": "USDT", "count": 1}],
            },
            mismatch_categories=["external_manual_activity_detected", "exchange_bills_activity_available"],
            mismatch_reasons=["recent_exchange_bills_may_explain_exchange_side_balance_activity"],
        )

        self.assertEqual(explanations[0]["operator_case"], "manual_activity")
        self.assertEqual(explanations[0]["operator_action"], "confirm_and_rebaseline")

    def test_funding_fee_suggests_observe_only(self) -> None:
        explanations = explain_okx_bills_for_reconciliation(
            summary={
                "top_categories": [{"type": "8", "sub_type": "173", "currency": "USDT", "count": 1}],
            },
            mismatch_categories=["external_manual_activity_detected", "local_balance_divergence"],
            mismatch_reasons=["local_balance_differs_from_exchange_balance"],
        )

        self.assertEqual(explanations[0]["operator_case"], "manual_activity")
        self.assertEqual(explanations[0]["operator_action"], "observe_only")


if __name__ == "__main__":
    unittest.main()
