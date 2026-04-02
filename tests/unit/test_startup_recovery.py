from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.execution_engine.exit_intent_aggregator import create_exit_execution_intent_from_order_state
from aats.schemas.system import RecoveryStatus
from aats.services.recovery_control.startup_recovery import (
    apply_startup_exit_execution_review_overlay,
    persist_startup_exit_execution_state_snapshot,
    startup_refresh_exit_execution_truth,
)
from aats.services.runtime_scope import runtime_state_scope
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.exit_execution_repo import InMemoryExitExecutionRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository


def _risk_reducing_filled_state(*, client_order_id: str, execution_chain_id: str) -> OrderState:
    now = utc_now()
    return OrderState(
        decision_id=f"decision_{client_order_id}",
        execution_chain_id=execution_chain_id,
        intent_id=f"intent_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        client_order_id=client_order_id,
        venue="OKX",
        exchange_order_id=f"ord_{client_order_id}",
        status="FILLED",
        submission_mode="guarded_live_submit",
        exchange_status="filled",
        submitted_ts=now,
        last_update_ts=now,
        requested_qty=Decimal("2"),
        filled_qty=Decimal("2"),
        remaining_qty=Decimal("0"),
        average_fill_price=Decimal("80000"),
        fees=Decimal("0"),
        reduce_only=True,
        close_only=True,
        position_mode="long_short_mode",
        pos_side="long",
        product_type="derivatives",
        margin_mode="cross",
        exposure_side="long",
        execution_action="exit",
        leg_action="close",
        position_intent="close_long",
        submission_payload={},
    )


class _BrokenExitExecutionRepository(InMemoryExitExecutionRepository):
    def list_exit_execution_intents(self) -> list:  # type: ignore[override]
        raise RuntimeError("boom")


class TestStartupRecovery(unittest.TestCase):
    def test_startup_refresh_updates_stale_parent_exit_execution_projection(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        scope = runtime_state_scope(settings)
        execution_repo = InMemoryExecutionRepository()
        exit_execution_repo = InMemoryExitExecutionRepository()
        order_state = _risk_reducing_filled_state(
            client_order_id="startup_refresh_parent_1",
            execution_chain_id="chain_startup_refresh_parent_1",
        )
        execution_repo.save_order_state(order_state)
        stale_parent = create_exit_execution_intent_from_order_state(order_state).model_copy(
            update={
                "aggregate_status": "WORKING",
                "aggregated_filled_quantity": Decimal("0"),
                "remaining_dispatchable_quantity": Decimal("2"),
                "remaining_unresolved_quantity": Decimal("2"),
            }
        )
        exit_execution_repo.save_exit_execution_intent(stale_parent)

        refreshed, notes = startup_refresh_exit_execution_truth(
            settings=settings,
            execution_repo=execution_repo,
            exit_execution_repo=exit_execution_repo,
            scope=scope,
        )

        self.assertEqual(len(refreshed), 1)
        self.assertEqual(notes, ["startup_exit_execution_parent_refresh_count:1"])
        parent = exit_execution_repo.get_exit_execution_intent(stale_parent.parent_intent_id)
        self.assertIsNotNone(parent)
        assert parent is not None
        self.assertEqual(parent.aggregate_status, "COMPLETED")
        self.assertEqual(parent.aggregated_filled_quantity, Decimal("2"))
        self.assertEqual(parent.remaining_dispatchable_quantity, Decimal("0"))
        self.assertEqual(parent.remaining_unresolved_quantity, Decimal("0"))
        self.assertEqual(parent.child_order_ids, [order_state.client_order_id])

    def test_startup_refresh_records_failure_note_when_parent_refresh_raises(self) -> None:
        settings = AATSSettings.model_validate({})
        scope = runtime_state_scope(settings)
        execution_repo = InMemoryExecutionRepository()
        broken_repo = _BrokenExitExecutionRepository()

        refreshed, notes = startup_refresh_exit_execution_truth(
            settings=settings,
            execution_repo=execution_repo,
            exit_execution_repo=broken_repo,
            scope=scope,
        )

        self.assertEqual(refreshed, [])
        self.assertEqual(notes, ["startup_exit_execution_parent_refresh_failed:RuntimeError"])

    def test_startup_review_overlay_marks_recovery_status_review_required(self) -> None:
        now = utc_now()
        parent = create_exit_execution_intent_from_order_state(
            _risk_reducing_filled_state(
                client_order_id="startup_overlay_parent_1",
                execution_chain_id="chain_startup_overlay_parent_1",
            )
        ).model_copy(
            update={
                "target_exit_quantity": Decimal("5"),
                "aggregate_status": "PARTIALLY_FILLED",
                "aggregated_filled_quantity": Decimal("2"),
                "remaining_dispatchable_quantity": Decimal("3"),
                "remaining_unresolved_quantity": Decimal("3"),
                "metadata": {
                    "dispatch_template": {
                        "execution_chain_id": "chain_startup_overlay_parent_1",
                        "symbol": "BTC-USDT-SWAP",
                    },
                    "resume_issue": {
                        "kind": "resume_limit_lookup_failed",
                        "operator_review_required": True,
                        "updated_at": now.isoformat(),
                    },
                },
            }
        )
        base_status = RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True)

        overlaid = apply_startup_exit_execution_review_overlay(
            base_status=base_status,
            parent_intents=[parent],
        )

        self.assertEqual(overlaid.recovery_state, "review_required")
        self.assertTrue(overlaid.review_required)
        self.assertFalse(overlaid.safe_startup)
        self.assertFalse(overlaid.safe_to_trade)
        self.assertFalse(overlaid.resume_eligible)
        self.assertTrue(overlaid.rebaseline_available)
        self.assertIn("exit_execution_resume_limit_lookup_failed", overlaid.resume_blocked_reasons)
        self.assertIn(
            "startup_exit_execution_review_required_count:1",
            overlaid.notes,
        )
        self.assertEqual(
            overlaid.unknown_state_details[0]["kind"],
            "exit_execution_resume_limit_lookup_failed",
        )

    def test_startup_review_overlay_marks_truth_pending_parent_as_resume_blocked(self) -> None:
        parent = create_exit_execution_intent_from_order_state(
            _risk_reducing_filled_state(
                client_order_id="startup_overlay_truth_pending_parent_1",
                execution_chain_id="chain_startup_overlay_truth_pending_parent_1",
            )
        ).model_copy(
            update={
                "target_exit_quantity": Decimal("5"),
                "aggregate_status": "WORKING",
                "aggregated_filled_quantity": Decimal("2"),
                "open_child_unknown_quantity": Decimal("1"),
                "remaining_dispatchable_quantity": Decimal("2"),
                "remaining_unresolved_quantity": Decimal("3"),
                "reconciliation_state": "truth_pending",
                "metadata": {
                    "dispatch_template": {
                        "execution_chain_id": "chain_startup_overlay_truth_pending_parent_1",
                        "symbol": "BTC-USDT-SWAP",
                    }
                },
            }
        )
        base_status = RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True)

        overlaid = apply_startup_exit_execution_review_overlay(
            base_status=base_status,
            parent_intents=[parent],
        )

        self.assertEqual(overlaid.recovery_state, "resume_blocked")
        self.assertFalse(overlaid.review_required)
        self.assertFalse(overlaid.safe_startup)
        self.assertFalse(overlaid.safe_to_trade)
        self.assertFalse(overlaid.resume_eligible)
        self.assertFalse(overlaid.rebaseline_available)
        self.assertIn("exit_execution_truth_pending", overlaid.resume_blocked_reasons)
        self.assertIn(
            "startup_exit_execution_overlay_count:1",
            overlaid.notes,
        )
        self.assertEqual(
            overlaid.unknown_state_details[0]["kind"],
            "exit_execution_truth_pending",
        )

    def test_startup_review_snapshot_persists_auditable_state_snapshot(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        scope = runtime_state_scope(settings)
        now = utc_now()
        parent = create_exit_execution_intent_from_order_state(
            _risk_reducing_filled_state(
                client_order_id="startup_snapshot_parent_1",
                execution_chain_id="chain_startup_snapshot_parent_1",
            )
        ).model_copy(
            update={
                "target_exit_quantity": Decimal("5"),
                "aggregate_status": "PARTIALLY_FILLED",
                "aggregated_filled_quantity": Decimal("2"),
                "remaining_dispatchable_quantity": Decimal("3"),
                "remaining_unresolved_quantity": Decimal("3"),
                "instrument_type": "derivatives",
                "metadata": {
                    "dispatch_template": {
                        "execution_chain_id": "chain_startup_snapshot_parent_1",
                        "symbol": "BTC-USDT-SWAP",
                    },
                    "resume_issue": {
                        "kind": "resume_limit_lookup_failed",
                        "operator_review_required": True,
                        "updated_at": now.isoformat(),
                    },
                },
            }
        )
        base_status = apply_startup_exit_execution_review_overlay(
            base_status=RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True),
            parent_intents=[parent],
        )
        reconciliation_repo = InMemoryReconciliationRepository()
        reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_startup_snapshot_parent_review",
                as_of_ts=now,
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=["BTC-USDT-SWAP"],
                exchange_comparison_enabled=False,
                order_diff={"reconstructed": {}, "exchange": {}},
                fill_diff={"replayed": {}, "exchange": {}},
                balance_diff={"reconstructed": {}, "exchange": {}},
                position_diff={
                    "stored": {},
                    "reconstructed": {},
                    "reconstructed_mismatches": {},
                    "exchange": {},
                    "exchange_mismatches": {},
                },
                severity="CLEAN",
                mismatch_categories=[],
                mismatch_reasons=[],
            )
        )

        notes = persist_startup_exit_execution_state_snapshot(
            reconciliation_repo=reconciliation_repo,
            scope=scope,
            status=base_status,
            parent_intents=[parent],
        )

        self.assertEqual(notes, ["startup_exit_execution_review_snapshot_saved"])
        snapshot = reconciliation_repo.latest_state_snapshot_for_scope(scope=scope)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.recovery_state, "review_required")
        self.assertTrue(snapshot.review_required)
        self.assertFalse(snapshot.resume_eligible)
        self.assertEqual(snapshot.details_json["source"], "startup_exit_execution_review")
        self.assertEqual(snapshot.details_json["review_item_count"], 1)
        self.assertEqual(
            snapshot.details_json["review_items"][0]["kind"],
            "exit_execution_resume_limit_lookup_failed",
        )
