from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderObligation, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyBookRuntimeState,
    StrategyExecutionBundle,
    StrategyLegIntent,
    StrategySleeveIntent,
)
from aats.services.execution_engine.recovery import ExecutionRecoveryService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository
from aats.storage.strategy_runtime_repo import InMemoryStrategyRuntimeRepository
from aats.schemas.reconciliation import ReconciliationReport


class TestExecutionRecovery(unittest.TestCase):
    def test_recovery_tracks_structured_bundle_open_orders_without_halting(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        for client_order_id, family, sleeve_id in (
            ("cl_bundle_grid", "spot_grid", "sleeve_grid"),
            ("cl_bundle_dca", "dca", "sleeve_dca"),
        ):
            execution_repo.save_order_state(
                OrderState(
                    decision_id="decision_bundle_1",
                    intent_id=f"intent_{client_order_id}",
                    symbol="BTC-USDT",
                    client_order_id=client_order_id,
                    venue="OKX",
                    exchange_order_id=f"ord_{client_order_id}",
                    status="SUBMITTED",
                    submission_mode="guarded_live_submit",
                    submitted_ts=now,
                    last_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.0,
                    remaining_qty=0.001,
                    average_fill_price=None,
                    fees=0.0,
                    product_type="spot",
                    margin_mode="cash",
                    strategy_family=family,
                    strategy_sleeve_id=sleeve_id,
                    allocation_id="alloc_bundle_1",
                    strategy_bundle_id="bundle_spot_inventory",
                    strategy_leg_role="inventory",
                    submission_payload={},
                )
            )
        recovery = self._service(execution_repo=execution_repo)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertFalse(artifacts.status.halted)
        self.assertEqual(artifacts.status.recovery_state, "bundle_recovery")
        self.assertTrue(artifacts.status.bundle_recovery_required)
        self.assertEqual(artifacts.status.bundle_recovery_count, 1)
        self.assertEqual(artifacts.status.recoverable_bundle_count, 1)
        self.assertEqual(artifacts.status.open_order_count, 2)
        self.assertFalse(artifacts.status.safe_to_trade)
        self.assertTrue(artifacts.status.only_reduce_required)
        self.assertIn("strategy_bundle_recovery_in_progress", artifacts.status.only_reduce_reasons)
        self.assertEqual(artifacts.status.bundle_summaries[0].recovery_state, "structured_open_orders")

    def test_recovery_blocks_resume_when_overlay_bundle_is_persisted_as_review_required(self) -> None:
        strategy_runtime_repo = InMemoryStrategyRuntimeRepository()
        strategy_runtime_repo.save_execution_bundle(
            StrategyExecutionBundle(
                bundle_id="bundle_overlay_review",
                decision_id="decision_overlay_review",
                family="directional",
                participating_families=["directional"],
                strategy_sleeve_refs=["sleeve_directional_core", "sleeve_overlay_short"],
                allocation_id="alloc_overlay_review",
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=("BTC-USDT-SWAP",),
                route_action="override_target",
                bundle_type="hedge_protected",
                status="review_required",
                selected_symbol="BTC-USDT-SWAP",
                operator_summary="overlay bundle mixed terminal outcome",
                reason_codes=["strategy_bundle_review_required"],
                legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="directional",
                        role="primary",
                        strategy_sleeve_id="sleeve_directional_core",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        execution_compatible=True,
                        execution_mode="independent_long_book",
                    ),
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        side="sell",
                        position_mode="long_short_mode",
                        pos_side="short",
                        action="open",
                        family="directional",
                        role="hedge",
                        strategy_sleeve_id="sleeve_overlay_short",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("-0.01"),
                        delta_position_qty=Decimal("-0.01"),
                        execution_compatible=True,
                        execution_mode="opportunistic_overlay",
                        overlay_mode="opportunistic",
                    ),
                ],
            )
        )
        recovery = self._service(
            settings_override={
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP"],
            },
            strategy_runtime_repo=strategy_runtime_repo,
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertEqual(artifacts.status.recovery_state, "review_required")
        self.assertTrue(artifacts.status.bundle_recovery_required)
        self.assertTrue(artifacts.status.review_required)
        self.assertFalse(artifacts.status.safe_to_trade)
        self.assertFalse(artifacts.status.resume_eligible)
        self.assertIn("strategy_bundle_recovery_requires_review", artifacts.status.resume_blocked_reasons)
        self.assertEqual(artifacts.status.bundle_summaries[0].recovery_state, "review_required")
        self.assertFalse(artifacts.status.bundle_summaries[0].recoverable)
        self.assertIn("bundle_review_required", artifacts.status.bundle_summaries[0].reason_codes)
        self.assertEqual(len(artifacts.status.bundle_summaries[0].legs), 2)
        short_leg = next(
            leg
            for leg in artifacts.status.bundle_summaries[0].legs
            if leg.pos_side == "short"
        )
        self.assertEqual(short_leg.leg_action, "open")
        self.assertEqual(short_leg.strategy_execution_mode, "opportunistic_overlay")

    def test_recovery_halts_when_bundle_open_orders_missing_identity(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_bundle_missing_1",
                intent_id="intent_bundle_missing_1",
                symbol="BTC-USDT",
                client_order_id="cl_bundle_missing_1",
                venue="OKX",
                exchange_order_id="ord_bundle_missing_1",
                status="SUBMITTED",
                submission_mode="guarded_live_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                product_type="spot",
                margin_mode="cash",
                strategy_family="spot_grid",
                allocation_id="alloc_bundle_missing",
                strategy_bundle_id="bundle_missing_identity",
                strategy_leg_role="inventory",
                submission_payload={},
            )
        )
        kill_switch = KillSwitch()
        recovery = self._service(execution_repo=execution_repo, kill_switch=kill_switch)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertTrue(kill_switch.halted)
        self.assertTrue(artifacts.status.halted)
        self.assertTrue(artifacts.status.bundle_recovery_required)
        self.assertEqual(artifacts.status.recovery_state, "review_required")
        self.assertIn("strategy_bundle_recovery_requires_review", artifacts.status.resume_blocked_reasons)
        self.assertEqual(artifacts.status.recovery_action, "halted_open_orders_require_review")

    def test_recovery_releases_orphan_active_obligation_without_order_state(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_orphan_1",
                decision_id="decision_orphan_1",
                intent_id="intent_orphan_1",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=60.0,
                status="ACTIVE",
                product_type="spot",
                margin_mode="cash",
                last_update_ts=utc_now(),
            )
        )
        recovery = self._service(obligation_repo=obligation_repo)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        obligation = obligation_repo.get_obligation("cl_orphan_1")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "FAILED")
        self.assertEqual(obligation.released_amount, Decimal("60.0"))
        self.assertIn("released_orphan_obligations:1", artifacts.status.notes)

    def test_recovery_scopes_margin_backed_smart_arbitrage_spot_obligation_into_derivatives_runtime(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_margin_backed_orphan_1",
                decision_id="decision_margin_backed_orphan_1",
                intent_id="intent_margin_backed_orphan_1",
                symbol="BTC-USDT",
                side="sell",
                reserve_currency="BTC",
                reserved_amount=0.25,
                status="ACTIVE",
                product_type="spot",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_bundle_id="bundle_margin_backed_1",
                strategy_leg_role="hedge",
                last_update_ts=utc_now(),
            )
        )
        recovery = self._service(
            obligation_repo=obligation_repo,
            settings_override={
                "config_profile": "guarded_derivatives_dry_run",
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP", "BTC-USDT"],
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            },
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        obligation = obligation_repo.get_obligation("cl_margin_backed_orphan_1")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "FAILED")
        self.assertEqual(obligation.released_amount, Decimal("0.25"))
        self.assertIn("released_orphan_obligations:1", artifacts.status.notes)

    def test_recovery_finalizes_active_obligation_for_terminal_order_state(self) -> None:
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_canceled_1",
                intent_id="intent_canceled_1",
                symbol="BTC-USDT",
                client_order_id="cl_canceled_1",
                venue="OKX",
                exchange_order_id="ord_canceled_1",
                status="CANCELED",
                submission_mode="guarded_simulated_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=0.001,
                filled_qty=0.0,
                remaining_qty=0.001,
                average_fill_price=None,
                fees=0.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        obligation_repo = InMemoryExecutionObligationRepository()
        obligation_repo.save_obligation(
            OrderObligation(
                client_order_id="cl_canceled_1",
                decision_id="decision_canceled_1",
                intent_id="intent_canceled_1",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=60.0,
                status="ACTIVE",
                product_type="spot",
                margin_mode="cash",
                last_update_ts=now,
            )
        )
        recovery = self._service(execution_repo=execution_repo, obligation_repo=obligation_repo)

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        obligation = obligation_repo.get_obligation("cl_canceled_1")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation.status, "CANCELED")
        self.assertEqual(obligation.released_amount, Decimal("60.0"))
        self.assertIn("released_orphan_obligations:1", artifacts.status.notes)

    def test_bootstrap_recovery_validates_latest_snapshot_against_trusted_baseline(self) -> None:
        portfolio_repo = InMemoryPortfolioRepository()
        baseline_snapshot = PortfolioSnapshot(
            snapshot_ts=utc_now(),
            snapshot_origin="exchange_import",
            balances={"USDT": Decimal("1000")},
            positions=[],
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("1000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            product_type="spot",
            margin_mode="cash",
        )
        portfolio_repo.save_snapshot(baseline_snapshot)
        divergent_snapshot = baseline_snapshot.model_copy(
            update={
                "snapshot_ts": utc_now(),
                "snapshot_origin": "fill_derived",
                "balances": {"USDT": Decimal("900"), "BTC": Decimal("1")},
                "total_equity": Decimal("1000"),
            }
        )
        portfolio_repo.save_snapshot(divergent_snapshot)
        kill_switch = KillSwitch()
        recovery = self._service(
            portfolio_repo=portfolio_repo,
            kill_switch=kill_switch,
            bootstrap_portfolio_from_exchange=True,
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=1_000.0))

        # P0-B auto-heal: divergence is automatically resolved by adopting the
        # fill-derived reconstruction, so the system does NOT halt.
        self.assertFalse(kill_switch.halted)
        self.assertIn("auto_healed_portfolio_divergence", ":".join(artifacts.status.notes))
        self.assertIn("stored_snapshot_replaced_by_fill_reconstruction", artifacts.status.notes)

    def test_recovery_marks_unknown_derivatives_position_as_review_required(self) -> None:
        reconciliation_repo = InMemoryReconciliationRepository()
        reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_only_reduce_recovery",
                as_of_ts=utc_now(),
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=["BTC-USDT-SWAP"],
                exchange_comparison_enabled=True,
                order_diff={"reconstructed": {}, "exchange": {}},
                fill_diff={"replayed": {}, "exchange": {}},
                balance_diff={"reconstructed": {}, "exchange": {}},
                position_diff={
                    "stored": {},
                    "reconstructed": {},
                    "reconstructed_mismatches": {},
                    "exchange": {"BTC-USDT-SWAP": "0.02"},
                    "exchange_mismatches": {"BTC-USDT-SWAP": {"stored": "0", "exchange": "0.02"}},
                },
                mismatch_categories=["derivatives_exchange_position_without_local_execution_chain"],
                mismatch_reasons=["derivatives_exchange_position_not_replayed_locally"],
                safety_impacts=["derivatives_only_reduce_until_position_reconciled"],
                severity="REVIEW_REQUIRED",
                review_required=True,
                only_reduce_required=True,
                only_reduce_reasons=["derivatives_exchange_position_without_local_execution_chain"],
                recovery_classification="manual_review_required",
                recommended_operator_action="go_close_position_on_exchange",
            )
        )
        recovery = self._service(
            reconciliation_repo=reconciliation_repo,
            settings_override={
                "config_profile": "guarded_derivatives_dry_run",
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "margin_mode": "cross",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP"],
            },
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertEqual(artifacts.status.recovery_state, "review_required")
        self.assertFalse(artifacts.status.safe_to_trade)
        self.assertFalse(artifacts.status.resume_eligible)
        self.assertTrue(artifacts.status.review_required)
        self.assertTrue(artifacts.status.only_reduce_required)
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", artifacts.status.only_reduce_reasons)
        self.assertIn("operator_rebaseline_required", artifacts.status.resume_blocked_reasons)

    def test_recovery_marks_derivatives_only_reduce_as_not_resumable_even_without_review(self) -> None:
        reconciliation_repo = InMemoryReconciliationRepository()
        reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_margin_only_reduce_recovery",
                as_of_ts=utc_now(),
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=["BTC-USDT-SWAP"],
                exchange_comparison_enabled=True,
                order_diff={"reconstructed": {}, "exchange": {}},
                fill_diff={"replayed": {}, "exchange": {}},
                balance_diff={"reconstructed": {}, "exchange": {}},
                position_diff={"stored": {}, "reconstructed": {}, "reconstructed_mismatches": {}, "exchange": {}, "exchange_mismatches": {}},
                mismatch_categories=["derivatives_runtime_margin_guard"],
                mismatch_reasons=["derivatives_margin_usage_requires_only_reduce"],
                safety_impacts=["derivatives_only_reduce_until_position_reconciled"],
                severity="SOFT_MISMATCH",
                only_reduce_required=True,
                only_reduce_reasons=["derivatives_margin_usage_requires_only_reduce"],
                recovery_classification="derivatives_only_reduce",
                recommended_operator_action="go_close_position_on_exchange",
            )
        )
        recovery = self._service(
            reconciliation_repo=reconciliation_repo,
            settings_override={
                "config_profile": "guarded_derivatives_dry_run",
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "margin_mode": "cross",
                "trading_product_type": "derivatives",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP"],
            },
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertEqual(artifacts.status.recovery_state, "only_reduce")
        self.assertFalse(artifacts.status.safe_to_trade)
        self.assertFalse(artifacts.status.resume_eligible)
        self.assertTrue(artifacts.status.only_reduce_required)
        self.assertIn("derivatives_margin_usage_requires_only_reduce", artifacts.status.resume_blocked_reasons)

    def test_recovery_surfaces_independent_recovery_snapshots_from_runtime_and_open_orders(self) -> None:
        strategy_runtime_repo = InMemoryStrategyRuntimeRepository()
        long_runtime_state = StrategyBookRuntimeState(
            leg="long",
            execution_chain_id="independent:decision_independent_1:long:open",
            current_qty=Decimal("0"),
            target_qty=Decimal("0.02"),
            state="opening",
            score=0.81,
            score_raw=0.81,
            score_adjusted=0.81,
            book_state="probing",
            holding_phase="entry",
            health_state="ok",
            book_action="open",
            policy_reason="independent_entry_guarded_passive_first",
            expected_signal_edge_bps=6.0,
            expected_cost_bps=1.5,
            expected_net_edge_bps=4.5,
            liquidity_quality_score=0.82,
            execution_health_state="ok",
            execution_policy_urgency="low",
            reason_codes=["independent_long_book_signal_above_entry_threshold"],
            blocked_reasons=[],
        )
        short_runtime_state = StrategyBookRuntimeState(
            leg="short",
            current_qty=Decimal("0"),
            target_qty=Decimal("0"),
            state="inactive",
            score=0.11,
            score_raw=0.11,
            score_adjusted=0.11,
            book_state="flat",
            holding_phase=None,
            health_state="ok",
            book_action="inactive",
            reason_codes=["independent_short_book_signal_below_entry_threshold"],
            blocked_reasons=[],
        )
        strategy_runtime_repo.save_allocation_decision(
            PortfolioAllocationDecision(
                allocation_id="alloc_independent_1",
                decision_id="decision_independent_1",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                primary_family="independent",
                approved_families=["independent"],
                sleeve_intents=[
                    StrategySleeveIntent(
                        decision_id="decision_independent_1",
                        family="independent",
                        strategy_sleeve_id="sleeve_independent_long_short",
                        state="candidate",
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        margin_mode="cross",
                        inventory_policy="paired_inventory",
                        route_action="override_target",
                        family_action="open_independent_book",
                        selectable=True,
                        execution_compatible=True,
                        metrics={
                            "book_runtime_states": [
                                long_runtime_state.model_dump(mode="json"),
                                short_runtime_state.model_dump(mode="json"),
                            ],
                            "long_threshold_snapshot": {
                                "leg": "long",
                                "shadow_only": True,
                                "entry_threshold": 0.7,
                                "close_threshold": 0.3,
                                "scale_in_threshold": 0.9,
                                "adaptive_entry_threshold": 0.73,
                                "adaptive_close_threshold": 0.32,
                                "adaptive_scale_in_threshold": 0.94,
                                "adaptive_thesis_age_seconds": 1500.0,
                                "adaptive_de_risk_net_edge_bps": 2.4,
                                "capital_multiplier": 0.91,
                                "reason_codes": ["adaptive_shadow_confidence_adjusted"],
                            },
                            "long_health_snapshot": {
                                "leg": "long",
                                "health_state": "ok",
                                "halt_openings": False,
                                "only_reduce": False,
                                "suspended": False,
                                "warnings": [],
                                "blockers": [],
                            },
                            "long_replay_snapshot": {
                                "leg": "long",
                                "score": 0.81,
                                "state": "opening",
                                "book_state": "probing",
                                "holding_phase": "entry",
                                "health_state": "ok",
                                "book_action": "open",
                                "policy_reason": "independent_entry_guarded_passive_first",
                                "prior_book_state": "flat",
                                "transition_reconstructed": True,
                                "transition_source": "current_qty_inference",
                            },
                            "family_health_overall_state": "ok",
                            "family_health_blockers": [],
                            "long_execution_style_preference": "bounded_limit_ioc",
                            "long_order_type_preference": "limit",
                            "long_time_in_force_preference": "IOC",
                        },
                    )
                ],
                execution_legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        execution_chain_id="independent:decision_independent_1:long:open",
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="independent",
                        role="hedge",
                        strategy_sleeve_id="sleeve_independent_long_short",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        execution_compatible=True,
                        execution_mode="independent_long_book",
                    )
                ],
            )
        )
        execution_repo = InMemoryExecutionRepository()
        now = utc_now()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_independent_1",
                execution_chain_id="independent:decision_independent_1:long:open",
                execution_attempt_id="attempt_independent_1",
                intent_id="intent_independent_1",
                symbol="BTC-USDT-SWAP",
                client_order_id="cl_independent_recovery_1",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTED",
                submission_mode="guarded_simulated_submit",
                submitted_ts=now,
                last_update_ts=now,
                requested_qty=Decimal("0.02"),
                filled_qty=Decimal("0"),
                remaining_qty=Decimal("0.02"),
                average_fill_price=None,
                fees=Decimal("0"),
                product_type="derivatives",
                margin_mode="cross",
                strategy_family="independent",
                strategy_sleeve_id="sleeve_independent_long_short",
                allocation_id="alloc_independent_1",
                strategy_bundle_id="bundle_independent_1",
                strategy_leg_role="hedge",
                pos_side="long",
                submission_payload={},
            )
        )
        strategy_runtime_repo.save_execution_bundle(
            StrategyExecutionBundle(
                bundle_id="bundle_independent_1",
                decision_id="decision_independent_1",
                family="independent",
                participating_families=["independent"],
                strategy_sleeve_refs=["sleeve_independent_long_short"],
                allocation_id="alloc_independent_1",
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=("BTC-USDT-SWAP",),
                route_action="override_target",
                bundle_type="single_sleeve",
                status="partial_fill_recovery",
                selected_symbol="BTC-USDT-SWAP",
                legs=[
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        execution_chain_id="independent:decision_independent_1:long:open",
                        product_type="derivatives",
                        side="buy",
                        position_mode="long_short_mode",
                        pos_side="long",
                        action="open",
                        family="independent",
                        role="hedge",
                        strategy_sleeve_id="sleeve_independent_long_short",
                        margin_mode="cross",
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("0.02"),
                        delta_position_qty=Decimal("0.02"),
                        execution_compatible=True,
                        execution_mode="independent_long_book",
                    )
                ],
            )
        )
        recovery = self._service(
            execution_repo=execution_repo,
            strategy_runtime_repo=strategy_runtime_repo,
            settings_override={
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ["BTC-USDT-SWAP"],
            },
        )

        artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

        self.assertTrue(artifacts.status.independent_recovery_snapshots)
        long_snapshot = next(
            snapshot
            for snapshot in artifacts.status.independent_recovery_snapshots
            if snapshot.leg == "long"
        )
        self.assertEqual(long_snapshot.recovery_posture, "pending_execution_attempts")
        self.assertEqual(
            long_snapshot.expected_chain_ids,
            ["independent:decision_independent_1:long:open"],
        )
        self.assertEqual(
            long_snapshot.active_execution_chain_ids,
            ["independent:decision_independent_1:long:open"],
        )
        self.assertEqual(long_snapshot.unresolved_attempt_ids, ["attempt_independent_1"])
        self.assertEqual(long_snapshot.decision_snapshot["execution_policy"]["order_type_preference"], "limit")
        self.assertEqual(long_snapshot.replay_snapshot["prior_book_state"], "flat")
        self.assertIn("adaptive_entry_threshold", long_snapshot.threshold_snapshot)
        self.assertIn("independent_recovery_snapshots:2", artifacts.status.notes)

    def test_recovery_normalizes_legacy_guard_book_states_from_runtime_payload(self) -> None:
        now = utc_now()
        scenarios = (
            ("cooldown", False, Decimal("0"), "flat", None),
            ("cooldown", True, Decimal("0"), "flat", "cooldown"),
            ("suspended", False, Decimal("0.01"), "holding", None),
            ("suspended", True, Decimal("0.01"), "holding", "suspended"),
        )
        for legacy_guard, active_guard, current_qty, expected_book_state, expected_guard_state in scenarios:
            with self.subTest(
                legacy_guard=legacy_guard,
                active_guard=active_guard,
                current_qty=str(current_qty),
            ):
                strategy_runtime_repo = InMemoryStrategyRuntimeRepository()
                runtime_state = {
                    "leg": "long",
                    "current_qty": format(current_qty, "f"),
                    "target_qty": format(current_qty, "f"),
                    "state": "blocked",
                    "book_state": legacy_guard,
                    "book_action": "blocked",
                    "prior_book_state": legacy_guard,
                    "blocked_reasons": ["independent_long_book_score_stability_below_threshold"],
                    "cooldown_until": (
                        None
                        if legacy_guard != "cooldown" or not active_guard
                        else (now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
                    ),
                    "suspended_until": (
                        None
                        if legacy_guard != "suspended" or not active_guard
                        else (now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
                    ),
                }
                strategy_runtime_repo.save_allocation_decision(
                    PortfolioAllocationDecision(
                        allocation_id=f"alloc_legacy_{legacy_guard}_{'active' if active_guard else 'stale'}_{str(current_qty).replace('.', '_')}",
                        decision_id=f"decision_legacy_{legacy_guard}_{'active' if active_guard else 'stale'}_{str(current_qty).replace('.', '_')}",
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        margin_mode="cross",
                        primary_family="independent",
                        approved_families=["independent"],
                        sleeve_intents=[
                            StrategySleeveIntent(
                                decision_id=f"decision_legacy_{legacy_guard}_{'active' if active_guard else 'stale'}_{str(current_qty).replace('.', '_')}",
                                family="independent",
                                strategy_sleeve_id="sleeve_independent_legacy_guard",
                                state="candidate",
                                symbol="BTC-USDT-SWAP",
                                product_type="derivatives",
                                margin_mode="cross",
                                inventory_policy="paired_inventory",
                                route_action="override_target",
                                family_action="open_independent_book",
                                selectable=True,
                                execution_compatible=True,
                                metrics={
                                    "book_runtime_states": [runtime_state],
                                    "long_replay_snapshot": {
                                        "leg": "long",
                                        "state": "blocked",
                                        "book_state": legacy_guard,
                                        "book_action": "blocked",
                                        "prior_book_state": legacy_guard,
                                    },
                                },
                            )
                        ],
                    )
                )
                recovery = self._service(
                    strategy_runtime_repo=strategy_runtime_repo,
                    settings_override={
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "default_symbol": "BTC-USDT-SWAP",
                        "allowed_symbols": ["BTC-USDT-SWAP"],
                    },
                )

                artifacts = recovery.recover(portfolio_state=PortfolioState(initial_usdt_balance=10_000.0))

                self.assertTrue(artifacts.status.independent_recovery_snapshots)
                long_snapshot = artifacts.status.independent_recovery_snapshots[0]
                self.assertEqual(long_snapshot.book_state, expected_book_state)
                self.assertEqual(long_snapshot.guard_state, expected_guard_state)
                self.assertEqual(long_snapshot.prior_book_state, expected_book_state)
                self.assertEqual(long_snapshot.prior_guard_state, expected_guard_state)
                self.assertEqual(long_snapshot.decision_snapshot["book_state"], expected_book_state)
                self.assertEqual(long_snapshot.decision_snapshot["guard_state"], expected_guard_state)
                self.assertEqual(long_snapshot.replay_snapshot["book_state"], expected_book_state)
                self.assertEqual(long_snapshot.replay_snapshot["guard_state"], expected_guard_state)

    @staticmethod
    def _service(
        *,
        execution_repo: InMemoryExecutionRepository | None = None,
        obligation_repo: InMemoryExecutionObligationRepository | None = None,
        portfolio_repo: InMemoryPortfolioRepository | None = None,
        kill_switch: KillSwitch | None = None,
        bootstrap_portfolio_from_exchange: bool = False,
        reconciliation_repo: InMemoryReconciliationRepository | None = None,
        strategy_runtime_repo: InMemoryStrategyRuntimeRepository | None = None,
        settings_override: dict | None = None,
    ) -> ExecutionRecoveryService:
        payload = {
            "storage_mode": "memory",
            "market_data_backend": "demo",
            "execution_backend": "paper",
            "account_backend": "disabled",
            "account_read_enabled": False,
        }
        payload.update(settings_override or {})
        settings = AATSSettings.model_validate(payload)
        return ExecutionRecoveryService(
            settings=settings,
            execution_repo=execution_repo or InMemoryExecutionRepository(),
            obligation_repo=obligation_repo or InMemoryExecutionObligationRepository(),
            portfolio_repo=portfolio_repo or InMemoryPortfolioRepository(),
            reconciliation_repo=reconciliation_repo or InMemoryReconciliationRepository(),
            strategy_runtime_repo=strategy_runtime_repo,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=settings.initial_usdt_balance,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("0"),
            kill_switch=kill_switch or KillSwitch(),
            bootstrap_portfolio_from_exchange=bootstrap_portfolio_from_exchange,
            reconciliation_stale_after_seconds=settings.reconciliation_stale_after_seconds,
        )
