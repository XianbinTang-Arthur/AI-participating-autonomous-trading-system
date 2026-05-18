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
    ExecutionLedgerRecoveryService,
    apply_startup_exit_execution_review_overlay,
    persist_startup_exit_execution_state_snapshot,
    startup_refresh_exit_execution_truth,
)
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.recovery_control.reconciliation_classifier import RecoveryReconciliationClassifier
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


class _EmptyPortfolioRepository:
    def history(self) -> list:
        return []


class _OpenOrderRepository:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def count_orders(self) -> int:
        return len(self.rows)

    def open_orders(self) -> list[dict]:
        return list(self.rows)


class _ScopeAwareOpenOrderRepository:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.list_calls: list[dict] = []
        self.count_calls: list[dict] = []

    def count_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        open_only: bool = False,
    ) -> int:
        self.count_calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
                "open_only": open_only,
            }
        )
        return len(self.rows)

    def list_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        limit: int | None = None,
        offset: int = 0,
        open_only: bool = False,
    ) -> list[dict]:
        self.list_calls.append(
            {
                "product_type": product_type,
                "margin_mode": margin_mode,
                "symbols": symbols,
                "limit": limit,
                "offset": offset,
                "open_only": open_only,
            }
        )
        return list(self.rows)

    def count_orders(self) -> int:
        raise AssertionError("startup recovery must not use unscoped count_orders")

    def open_orders(self) -> list[dict]:
        raise AssertionError("startup recovery must not use unscoped open_orders")


class _CommandRepository:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    def get_by_idempotency_key(self, key: str) -> dict | None:
        return self.rows.get(key)

    def command_counts(self, *, sent_stale_before=None) -> dict[str, int]:
        counts = {
            "pending_total": 0,
            "pending_submit": 0,
            "pending_cancel": 0,
            "sent_stale_total": 0,
            "sent_stale_submit": 0,
            "sent_stale_cancel": 0,
        }
        for row in self.rows.values():
            state = str(row.get("state") or "").upper()
            command_type = str(row.get("command_type") or "").strip().lower()
            if state in {"PENDING", "CLAIMED"}:
                counts["pending_total"] += 1
                if command_type == "submit":
                    counts["pending_submit"] += 1
                elif command_type == "cancel":
                    counts["pending_cancel"] += 1
            elif state == "SENT":
                counts["sent_stale_total"] += 1
                if command_type == "submit":
                    counts["sent_stale_submit"] += 1
                elif command_type == "cancel":
                    counts["sent_stale_cancel"] += 1
        return counts


class TestStartupRecovery(unittest.TestCase):
    def test_phase4_created_order_missing_submit_command_overrides_generic_open_order_action(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
            }
        )
        order_repo = _OpenOrderRepository(
            [
                {
                    "order_id": "cl_missing_submit",
                    "client_order_id": "cl_missing_submit",
                    "intent_id": "intent_missing_submit",
                    "state": "CREATED",
                    "venue_order_id": None,
                    "product_type": "spot",
                    "margin_mode": "cash",
                    "symbol": "BTC-USDT",
                }
            ]
        )
        service = ExecutionLedgerRecoveryService(
            settings=settings,
            base_recovery_service=None,  # type: ignore[arg-type]
            reconciliation_repo=InMemoryReconciliationRepository(),
            portfolio_repo=_EmptyPortfolioRepository(),
            kill_switch=KillSwitch(),
            reconciliation_classifier=RecoveryReconciliationClassifier(),
            execution_order_repo=order_repo,
            execution_command_repo=_CommandRepository({}),
        )

        status = service._phase4_status(
            base_status=RecoveryStatus(
                status="recovered_halted",
                recovery_state="resume_blocked",
                safe_startup=False,
                recovery_action="halted_open_orders_require_review",
            ),
            latest_reconciliation=None,
        )

        self.assertTrue(status.halted)
        self.assertEqual(status.recovery_action, "halted_created_orders_missing_submit_commands")
        self.assertIn("created_orders_missing_submit_commands", status.resume_blocked_reasons)
        self.assertIn("created_orders_missing_submit_commands:1", status.notes)

    def test_phase4_execution_counts_use_scope_aware_execution_order_repo(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        order_repo = _ScopeAwareOpenOrderRepository(
            [
                {
                    "order_id": "cl_derivatives_missing_submit",
                    "client_order_id": "cl_derivatives_missing_submit",
                    "intent_id": "intent_derivatives_missing_submit",
                    "state": "CREATED",
                    "venue_order_id": None,
                    "product_type": "derivatives",
                    "margin_mode": "cross",
                    "symbol": "BTC-USDT-SWAP",
                }
            ]
        )
        service = ExecutionLedgerRecoveryService(
            settings=settings,
            base_recovery_service=None,  # type: ignore[arg-type]
            reconciliation_repo=InMemoryReconciliationRepository(),
            portfolio_repo=_EmptyPortfolioRepository(),
            kill_switch=KillSwitch(),
            reconciliation_classifier=RecoveryReconciliationClassifier(),
            execution_order_repo=order_repo,
            execution_command_repo=_CommandRepository({}),
        )

        status = service._phase4_status(
            base_status=RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True),
            latest_reconciliation=None,
        )

        self.assertTrue(status.halted)
        self.assertEqual(status.recovered_order_count, 1)
        self.assertEqual(status.open_order_count, 1)
        self.assertEqual(
            order_repo.count_calls,
            [
                {
                    "product_type": "derivatives",
                    "margin_mode": "cross",
                    "symbols": ("BTC-USDT-SWAP",),
                    "open_only": False,
                }
            ],
        )
        self.assertEqual(
            order_repo.list_calls,
            [
                {
                    "product_type": "derivatives",
                    "margin_mode": "cross",
                    "symbols": ("BTC-USDT-SWAP",),
                    "limit": None,
                    "offset": 0,
                    "open_only": True,
                }
            ],
        )

    def test_phase4_classifies_claimed_submit_as_exchange_reconcile_blocker(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        order_repo = _OpenOrderRepository(
            [
                {
                    "order_id": "cl_claimed_submit",
                    "client_order_id": "cl_claimed_submit",
                    "intent_id": "intent_claimed_submit",
                    "state": "SUBMITTING",
                    "venue_order_id": None,
                    "product_type": "derivatives",
                    "margin_mode": "cross",
                    "symbol": "BTC-USDT-SWAP",
                }
            ]
        )
        command_repo = _CommandRepository(
            {
                "submit:cl_claimed_submit": {
                    "command_id": "cmd_claimed_submit",
                    "order_id": "cl_claimed_submit",
                    "command_type": "submit",
                    "idempotency_key": "submit:cl_claimed_submit",
                    "state": "CLAIMED",
                }
            }
        )
        service = ExecutionLedgerRecoveryService(
            settings=settings,
            base_recovery_service=None,  # type: ignore[arg-type]
            reconciliation_repo=InMemoryReconciliationRepository(),
            portfolio_repo=_EmptyPortfolioRepository(),
            kill_switch=KillSwitch(),
            reconciliation_classifier=RecoveryReconciliationClassifier(),
            execution_order_repo=order_repo,
            execution_command_repo=command_repo,
        )

        status = service._phase4_status(
            base_status=RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True),
            latest_reconciliation=None,
        )

        self.assertTrue(status.halted)
        self.assertEqual(status.pending_command_count, 0)
        self.assertEqual(status.pending_submit_command_count, 0)
        self.assertEqual(status.claimed_submit_command_count, 1)
        self.assertEqual(status.recovery_action, "halted_claimed_submit_commands_require_exchange_reconciliation")
        self.assertIn(
            "claimed_submit_commands_require_exchange_reconciliation",
            status.resume_blocked_reasons,
        )
        self.assertIn("claimed_submit_commands_require_exchange_reconciliation:1", status.notes)

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
        self.assertEqual(
            notes,
            [
                "startup_exit_execution_parent_refresh_failed:RuntimeError",
                "startup_exit_execution_parent_refresh_stage:refresh_exit_execution_intents",
                "startup_exit_execution_parent_refresh_scope:spot/cash/BTC-USDT/allowed_symbols=1",
                "startup_exit_execution_parent_refresh_message:boom",
            ],
        )

    def test_startup_review_overlay_promotes_parent_refresh_failure_into_resume_block(self) -> None:
        overlaid = apply_startup_exit_execution_review_overlay(
            base_status=RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True),
            parent_intents=[],
            refresh_notes=["startup_exit_execution_parent_refresh_failed:RuntimeError"],
        )

        self.assertEqual(overlaid.recovery_state, "review_required")
        self.assertTrue(overlaid.review_required)
        self.assertFalse(overlaid.safe_startup)
        self.assertFalse(overlaid.safe_to_trade)
        self.assertFalse(overlaid.resume_eligible)
        self.assertIn("startup_exit_execution_parent_refresh_failed", overlaid.resume_blocked_reasons)
        self.assertEqual(
            overlaid.unknown_state_details[0]["kind"],
            "startup_exit_execution_parent_refresh_failed",
        )
        self.assertEqual(
            overlaid.unknown_state_details[0]["refresh_stage"],
            "refresh_exit_execution_intents",
        )
        self.assertEqual(
            overlaid.unknown_state_details[0]["scope_summary"],
            "unknown_scope",
        )
        self.assertEqual(
            overlaid.unknown_state_details[0]["exception_message"],
            "no_exception_message",
        )
        self.assertIn(
            "startup_exit_execution_overlay_count:1",
            overlaid.notes,
        )

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

    def test_startup_review_snapshot_persists_refresh_failure_overlay_without_parent_intents(self) -> None:
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
        reconciliation_repo = InMemoryReconciliationRepository()
        reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_startup_snapshot_parent_refresh_failure",
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
                severity="REVIEW_REQUIRED",
                mismatch_categories=[],
                mismatch_reasons=[],
            )
        )
        status = apply_startup_exit_execution_review_overlay(
            base_status=RecoveryStatus(status="recovered", recovery_state="normal_operation", safe_startup=True),
            parent_intents=[],
            refresh_notes=[
                "startup_exit_execution_parent_refresh_failed:RuntimeError",
                "startup_exit_execution_parent_refresh_stage:refresh_exit_execution_intents",
                "startup_exit_execution_parent_refresh_scope:derivatives/cross/BTC-USDT-SWAP/allowed_symbols=1",
                "startup_exit_execution_parent_refresh_message:boom",
            ],
        )

        notes = persist_startup_exit_execution_state_snapshot(
            reconciliation_repo=reconciliation_repo,
            scope=scope,
            status=status,
            parent_intents=[],
            refresh_notes=[
                "startup_exit_execution_parent_refresh_failed:RuntimeError",
                "startup_exit_execution_parent_refresh_stage:refresh_exit_execution_intents",
                "startup_exit_execution_parent_refresh_scope:derivatives/cross/BTC-USDT-SWAP/allowed_symbols=1",
                "startup_exit_execution_parent_refresh_message:boom",
            ],
        )

        self.assertEqual(notes, ["startup_exit_execution_review_snapshot_saved"])
        snapshot = reconciliation_repo.latest_state_snapshot_for_scope(scope=scope)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.details_json["source"], "startup_exit_execution_review")
        self.assertEqual(snapshot.details_json["review_items"][0]["kind"], "startup_exit_execution_parent_refresh_failed")
        self.assertEqual(
            snapshot.details_json["review_items"][0]["refresh_stage"],
            "refresh_exit_execution_intents",
        )
        self.assertEqual(
            snapshot.details_json["review_items"][0]["scope_summary"],
            "derivatives/cross/BTC-USDT-SWAP/allowed_symbols=1",
        )
        self.assertEqual(
            snapshot.details_json["review_items"][0]["exception_message"],
            "boom",
        )
