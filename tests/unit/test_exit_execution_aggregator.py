from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent, OrderState
from aats.services.execution_engine.exit_intent_aggregator import (
    EXIT_EXECUTION_BLOCKER_KINDS,
    EXIT_EXECUTION_MISSING_CHILD_REFS_KIND,
    MISSING_CHILD_REFS_RESUME_ISSUE_KIND,
    RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND,
    child_exit_order_ref_from_order_state,
    create_exit_execution_intent_from_order_intent,
    exit_execution_review_items,
    record_resume_issue,
    refresh_exit_execution_intents,
    recompute_exit_execution_intent,
    request_cancel_exit_execution_intent,
)
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.exit_execution_repo import InMemoryExitExecutionRepository


def _make_parent(*, quantity: str = "5") -> object:
    return create_exit_execution_intent_from_order_intent(
        OrderIntent(
            intent_id="intent_exit_parent",
            execution_chain_id="chain_exit_parent",
            decision_id="decision_exit_parent",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal(quantity),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            pos_side="long",
            position_mode="long_short_mode",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            idempotency_key="intent_exit_parent",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
        )
    )


def _make_order_state(
    *,
    client_order_id: str,
    status: str,
    requested_qty: str = "5",
    filled_qty: str = "0",
    remaining_qty: str | None = None,
    execution_error: str | None = None,
    cancellation_requested: bool = False,
) -> OrderState:
    now = utc_now()
    remaining = Decimal(remaining_qty) if remaining_qty is not None else max(
        Decimal(requested_qty) - Decimal(filled_qty),
        Decimal("0"),
    )
    return OrderState(
        decision_id="decision_exit_parent",
        execution_chain_id="chain_exit_parent",
        intent_id=f"intent_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        client_order_id=client_order_id,
        venue="OKX",
        exchange_order_id=f"ord_{client_order_id}" if execution_error is None else None,
        status=status,  # type: ignore[arg-type]
        exchange_status="live" if status in {"SUBMITTED", "PARTIALLY_FILLED", "CANCEL_PENDING"} else "filled",
        submitted_ts=now - timedelta(seconds=10),
        last_update_ts=now - timedelta(seconds=5),
        cancellation_requested_ts=(now - timedelta(seconds=5)) if cancellation_requested else None,
        requested_qty=Decimal(requested_qty),
        filled_qty=Decimal(filled_qty),
        remaining_qty=remaining,
        average_fill_price=Decimal("80000") if Decimal(filled_qty) > 0 else None,
        fees=Decimal("0"),
        reduce_only=True,
        close_only=True,
        product_type="derivatives",
        margin_mode="cross",
        position_mode="long_short_mode",
        pos_side="long",
        execution_action="exit",
        leg_action="close",
        position_intent="close_long",
        execution_error=execution_error,
    )


class TestExitExecutionAggregator(unittest.TestCase):
    def test_single_child_fill_completes_parent(self) -> None:
        parent = _make_parent(quantity="5")
        child = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=_make_order_state(
                client_order_id="child_fill_1",
                status="FILLED",
                requested_qty="5",
                filled_qty="5",
                remaining_qty="0",
            ),
        )

        aggregate = recompute_exit_execution_intent(parent_intent=parent, child_refs=[child])

        self.assertEqual(aggregate.aggregate_status, "COMPLETED")
        self.assertEqual(aggregate.aggregated_filled_quantity, Decimal("5"))
        self.assertEqual(aggregate.remaining_dispatchable_quantity, Decimal("0"))
        self.assertEqual(aggregate.remaining_unresolved_quantity, Decimal("0"))

    def test_unknown_child_occupies_dispatchable_quantity(self) -> None:
        parent = _make_parent(quantity="5")
        child = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=_make_order_state(
                client_order_id="child_unknown_1",
                status="SUBMITTED",
                requested_qty="2",
                filled_qty="0",
                remaining_qty="2",
                execution_error="submission_unknown_check_exchange:OKXRequestError",
            ),
            settings=AATSSettings.model_validate({"execution_unknown_submit_review_after_seconds": 300.0}),
        )

        aggregate = recompute_exit_execution_intent(parent_intent=parent, child_refs=[child])

        self.assertEqual(aggregate.aggregate_status, "WORKING")
        self.assertEqual(aggregate.open_child_unknown_quantity, Decimal("2"))
        self.assertEqual(aggregate.remaining_dispatchable_quantity, Decimal("3"))
        self.assertEqual(aggregate.reconciliation_state, "truth_pending")

    def test_mixed_child_states_produce_partially_filled_parent(self) -> None:
        parent = _make_parent(quantity="5")
        filled = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=_make_order_state(
                client_order_id="child_filled_1",
                status="FILLED",
                requested_qty="2",
                filled_qty="2",
                remaining_qty="0",
            ),
        )
        working = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=_make_order_state(
                client_order_id="child_working_1",
                status="SUBMITTED",
                requested_qty="2",
                filled_qty="0",
                remaining_qty="2",
            ),
        )

        aggregate = recompute_exit_execution_intent(parent_intent=parent, child_refs=[filled, working])

        self.assertEqual(aggregate.aggregate_status, "PARTIALLY_FILLED")
        self.assertEqual(aggregate.aggregated_filled_quantity, Decimal("2"))
        self.assertEqual(aggregate.open_child_working_quantity, Decimal("2"))
        self.assertEqual(aggregate.remaining_dispatchable_quantity, Decimal("1"))

    def test_cancel_requested_parent_with_live_child_stays_cancel_pending(self) -> None:
        parent = request_cancel_exit_execution_intent(_make_parent(quantity="5"))
        working = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=_make_order_state(
                client_order_id="child_cancel_pending_1",
                status="CANCEL_PENDING",
                requested_qty="5",
                filled_qty="1",
                remaining_qty="4",
                cancellation_requested=True,
            ),
        )

        aggregate = recompute_exit_execution_intent(parent_intent=parent, child_refs=[working])

        self.assertEqual(aggregate.aggregate_status, "CANCEL_PENDING")
        self.assertTrue(aggregate.cancel_requested)

    def test_child_risk_reducing_invariant_breach_escalates_review_required(self) -> None:
        parent = _make_parent(quantity="5")
        child = child_exit_order_ref_from_order_state(
            parent_intent_id=parent.parent_intent_id,
            order_state=_make_order_state(
                client_order_id="child_bad_1",
                status="SUBMITTED",
                requested_qty="5",
                filled_qty="0",
                remaining_qty="5",
            ),
        ).model_copy(update={"risk_reducing_invariant": False})

        aggregate = recompute_exit_execution_intent(parent_intent=parent, child_refs=[child])

        self.assertEqual(aggregate.aggregate_status, "REVIEW_REQUIRED")
        self.assertTrue(aggregate.operator_review_required)
        self.assertFalse(aggregate.risk_reducing_invariant)

    def test_truth_pending_parent_surfaces_structural_review_item(self) -> None:
        parent = _make_parent(quantity="5").model_copy(
            update={
                "aggregate_status": "WORKING",
                "reconciliation_state": "truth_pending",
                "open_child_unknown_quantity": Decimal("2"),
                "remaining_dispatchable_quantity": Decimal("3"),
                "remaining_unresolved_quantity": Decimal("5"),
                "metadata": {
                    "dispatch_template": {
                        "intent_id": "intent_exit_parent",
                        "execution_chain_id": "chain_exit_parent",
                        "symbol": "BTC-USDT-SWAP",
                    }
                },
            }
        )

        items = exit_execution_review_items([parent])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "exit_execution_truth_pending")
        self.assertFalse(items[0]["operator_review_required"])
        self.assertTrue(items[0]["blocks_resume"])
        self.assertEqual(items[0]["resume_block_reason"], "unknown_child_truth_pending")

    def test_refresh_exit_execution_intents_marks_childless_parent_with_resume_issue(self) -> None:
        settings = AATSSettings.model_validate({})
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        parent = _make_parent(quantity="5").model_copy(
            update={
                "aggregate_status": "WORKING",
                "remaining_dispatchable_quantity": Decimal("5"),
                "remaining_unresolved_quantity": Decimal("5"),
                "metadata": {
                    "dispatch_template": {
                        "intent_id": "intent_exit_parent",
                        "execution_chain_id": "chain_exit_parent",
                        "symbol": "BTC-USDT-SWAP",
                    }
                },
            }
        )
        exit_repo.save_exit_execution_intent(parent)

        refreshed = refresh_exit_execution_intents(
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            settings=settings,
        )

        self.assertEqual(len(refreshed), 1)
        updated_parent = exit_repo.get_exit_execution_intent(parent.parent_intent_id)
        self.assertIsNotNone(updated_parent)
        assert updated_parent is not None
        self.assertEqual(updated_parent.metadata["resume_issue"]["kind"], "missing_child_refs_for_parent")
        items = exit_execution_review_items([updated_parent])
        self.assertEqual(items[0]["kind"], "exit_execution_missing_child_refs_for_parent")
        self.assertEqual(items[0]["resume_block_reason"], "missing_child_refs_for_parent")

    def test_refresh_preserves_prior_resume_issue_kind_when_parent_becomes_childless(self) -> None:
        """Task 142：parent 先有 resume_limit_lookup_failed 再 childless 时，
        新 issue 必须在 prior_kind 里保留旧 kind，避免运维丢失原始诊断线索。"""
        settings = AATSSettings.model_validate({})
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        parent_bare = _make_parent(quantity="5")
        parent_with_prior_issue = record_resume_issue(
            parent_bare.model_copy(
                update={
                    "aggregate_status": "WORKING",
                    "remaining_dispatchable_quantity": Decimal("5"),
                    "remaining_unresolved_quantity": Decimal("5"),
                    "metadata": {
                        "dispatch_template": {
                            "intent_id": "intent_exit_parent",
                            "execution_chain_id": "chain_exit_parent",
                            "symbol": "BTC-USDT-SWAP",
                        }
                    },
                }
            ),
            kind=RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND,
            error="limit_lookup_unreachable",
        )
        self.assertEqual(
            parent_with_prior_issue.metadata["resume_issue"]["kind"],
            RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND,
        )
        self.assertNotIn("prior_kind", parent_with_prior_issue.metadata["resume_issue"])
        exit_repo.save_exit_execution_intent(parent_with_prior_issue)

        refresh_exit_execution_intents(
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            settings=settings,
        )

        updated_parent = exit_repo.get_exit_execution_intent(parent_with_prior_issue.parent_intent_id)
        assert updated_parent is not None
        issue = updated_parent.metadata["resume_issue"]
        self.assertEqual(issue["kind"], MISSING_CHILD_REFS_RESUME_ISSUE_KIND)
        # prior_kind 必须保留，防止原诊断静默丢失
        self.assertEqual(issue["prior_kind"], RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND)

    def test_refresh_is_idempotent_when_parent_already_has_missing_child_refs_issue(self) -> None:
        """Task 142：对已有同 kind issue 的 childless parent 再跑 refresh，
        resume_issue 的 updated_at 不漂移（_mark_parent_missing_child_refs
        的 early-return 分支保护幂等性）。"""
        settings = AATSSettings.model_validate({})
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        parent = _make_parent(quantity="5").model_copy(
            update={
                "aggregate_status": "WORKING",
                "remaining_dispatchable_quantity": Decimal("5"),
                "remaining_unresolved_quantity": Decimal("5"),
                "metadata": {
                    "dispatch_template": {
                        "intent_id": "intent_exit_parent",
                        "execution_chain_id": "chain_exit_parent",
                        "symbol": "BTC-USDT-SWAP",
                    }
                },
            }
        )
        exit_repo.save_exit_execution_intent(parent)

        refresh_exit_execution_intents(
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            settings=settings,
        )
        first_state = exit_repo.get_exit_execution_intent(parent.parent_intent_id)
        assert first_state is not None
        first_updated_at = first_state.metadata["resume_issue"]["updated_at"]

        refresh_exit_execution_intents(
            execution_repo=execution_repo,
            exit_execution_repo=exit_repo,
            settings=settings,
        )
        second_state = exit_repo.get_exit_execution_intent(parent.parent_intent_id)
        assert second_state is not None
        second_updated_at = second_state.metadata["resume_issue"]["updated_at"]

        self.assertEqual(first_updated_at, second_updated_at)


class TestExitExecutionKindConstants(unittest.TestCase):
    """Task 142 锚点：防止 aggregator 的 review kind 常量和
    recovery_posture 的 persistent blocker 名单漂移。"""

    def test_blocker_kinds_covered_by_recovery_posture_persistent_status_blockers(self) -> None:
        posture_blockers = RecoveryPostureEvaluator._PERSISTENT_STATUS_BLOCKERS
        self.assertTrue(EXIT_EXECUTION_BLOCKER_KINDS.issubset(posture_blockers))

    def test_kind_constant_strings_not_accidentally_renamed(self) -> None:
        """字符串是持久化到 DB metadata 的 kind，不得改名。"""
        self.assertEqual(EXIT_EXECUTION_MISSING_CHILD_REFS_KIND, "exit_execution_missing_child_refs_for_parent")
        self.assertEqual(MISSING_CHILD_REFS_RESUME_ISSUE_KIND, "missing_child_refs_for_parent")
        self.assertEqual(RESUME_LIMIT_LOOKUP_FAILED_RESUME_ISSUE_KIND, "resume_limit_lookup_failed")


if __name__ == "__main__":
    unittest.main()
