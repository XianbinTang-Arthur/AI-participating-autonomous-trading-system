from __future__ import annotations

import unittest
from datetime import timedelta

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import (
    AccountBaselineSnapshot,
    ExchangeAccountSnapshot,
    ExchangeAccountConfiguration,
    ExchangeBalance,
    ExchangeFill,
    ExchangeOpenOrder,
    ExchangePosition,
)
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.services.reconciliation_service.repair import ReconciliationRepairService, ReconciliationService
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository


class TestReconciliationComparator(unittest.TestCase):
    def test_compare_derivatives_balance_matches_exchange_cash_balance(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_clean",
            portfolio_snapshot_ref="evt_portfolio_derivatives_clean",
            product_type="derivatives",
            margin_mode="cross",
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_qty=0.01,
                        position_notional=710.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=10.0,
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=10.0,
                total_equity=10_010.0,
                gross_exposure=710.0,
                net_exposure=710.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_qty=0.01,
                        position_notional=710.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=10.0,
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=10.0,
                total_equity=10_010.0,
                gross_exposure=710.0,
                net_exposure=710.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.01,
                        average_entry_price=70_000.0,
                        mark_price=71_000.0,
                    )
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertFalse(report.balance_diff["exchange"])

    def test_compare_derivatives_long_short_positions_by_position_key(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_long_short_clean",
            portfolio_snapshot_ref="evt_portfolio_derivatives_long_short_clean",
            product_type="derivatives",
            margin_mode="cross",
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    ),
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:short",
                        position_qty=-0.01,
                        position_notional=-710.0,
                        avg_entry_price=71_000.0,
                        unrealized_pnl=5.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="short",
                    ),
                ],
                cost_basis={"BTC-USDT-SWAP:long": 70_000.0, "BTC-USDT-SWAP:short": 71_000.0},
                realized_pnl=0.0,
                unrealized_pnl=25.0,
                total_equity=10_025.0,
                gross_exposure=2130.0,
                net_exposure=710.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    ),
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:short",
                        position_qty=-0.01,
                        position_notional=-710.0,
                        avg_entry_price=71_000.0,
                        unrealized_pnl=5.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="short",
                    ),
                ],
                cost_basis={"BTC-USDT-SWAP:long": 70_000.0, "BTC-USDT-SWAP:short": 71_000.0},
                realized_pnl=0.0,
                unrealized_pnl=25.0,
                total_equity=10_025.0,
                gross_exposure=2130.0,
                net_exposure=710.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=70_000.0,
                        mark_price=71_000.0,
                        side="long",
                    ),
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.01,
                        average_entry_price=71_000.0,
                        mark_price=70_500.0,
                        side="short",
                    ),
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="long_short_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="long_short_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertFalse(report.position_diff["exchange_mismatches"])

    def test_compare_classifies_long_short_leg_qty_drift_as_leg_mismatch_not_unknown_chain(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_leg_mismatch",
            portfolio_snapshot_ref="evt_portfolio_derivatives_leg_mismatch",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    ),
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:short",
                        position_qty=-0.01,
                        position_notional=-710.0,
                        avg_entry_price=71_000.0,
                        unrealized_pnl=5.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="short",
                    ),
                ],
                cost_basis={"BTC-USDT-SWAP:long": 70_000.0, "BTC-USDT-SWAP:short": 71_000.0},
                realized_pnl=0.0,
                unrealized_pnl=25.0,
                total_equity=10_025.0,
                gross_exposure=2130.0,
                net_exposure=710.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    ),
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:short",
                        position_qty=-0.01,
                        position_notional=-710.0,
                        avg_entry_price=71_000.0,
                        unrealized_pnl=5.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="short",
                    ),
                ],
                cost_basis={"BTC-USDT-SWAP:long": 70_000.0, "BTC-USDT-SWAP:short": 71_000.0},
                realized_pnl=0.0,
                unrealized_pnl=25.0,
                total_equity=10_025.0,
                gross_exposure=2130.0,
                net_exposure=710.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=70_000.0,
                        mark_price=71_000.0,
                        side="long",
                    ),
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=71_000.0,
                        mark_price=70_500.0,
                        side="short",
                    ),
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="long_short_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="long_short_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertFalse(report.only_reduce_required)
        self.assertIn("derivatives_leg_position_mismatch", report.mismatch_categories)
        self.assertIn("derivatives_leg_position_differs_from_exchange", report.mismatch_reasons)
        self.assertIn("BTC-USDT-SWAP:short", report.position_diff["exchange_leg_mismatches"])
        self.assertTrue(
            all(
                detail.get("kind") != "exchange_position_without_local_execution_chain"
                for detail in report.unknown_state_details
            )
        )

    def test_compare_marks_missing_short_leg_without_local_execution_chain_by_position_key(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_missing_short_leg",
            portfolio_snapshot_ref="evt_portfolio_derivatives_missing_short_leg",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            order_states=[
                OrderState(
                    decision_id="decision_derivatives_missing_short_leg",
                    intent_id="intent_derivatives_missing_short_leg_long",
                    symbol="BTC-USDT-SWAP",
                    client_order_id="clord_derivatives_missing_short_leg_long",
                    venue="OKX",
                    exchange_order_id="ord_derivatives_missing_short_leg_long",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.02,
                    filled_qty=0.02,
                    remaining_qty=0.0,
                    average_fill_price=70_000.0,
                    fees=0.0,
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    submission_payload={},
                )
            ],
            fills=[
                FillEvent(
                    fill_id="fill_derivatives_missing_short_leg_long",
                    decision_id="decision_derivatives_missing_short_leg",
                    intent_id="intent_derivatives_missing_short_leg_long",
                    client_order_id="clord_derivatives_missing_short_leg_long",
                    exchange_order_id="ord_derivatives_missing_short_leg_long",
                    symbol="BTC-USDT-SWAP",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.02,
                    fill_price=70_000.0,
                    fee_amount=0.0,
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    liquidity_role="taker",
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP:long": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=20.0,
                total_equity=10_020.0,
                gross_exposure=1420.0,
                net_exposure=1420.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP:long": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=20.0,
                total_equity=10_020.0,
                gross_exposure=1420.0,
                net_exposure=1420.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=70_000.0,
                        mark_price=71_000.0,
                        side="long",
                    ),
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.01,
                        average_entry_price=71_000.0,
                        mark_price=70_500.0,
                        side="short",
                    ),
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="long_short_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="long_short_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertTrue(report.only_reduce_required)
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", report.mismatch_categories)
        self.assertIn("BTC-USDT-SWAP:short", report.position_diff["exchange_leg_mismatches"])
        self.assertEqual(report.unknown_state_details[0]["position_key"], "BTC-USDT-SWAP:short")
        self.assertEqual(report.unknown_state_details[0]["leg_side"], "short")

    def test_compare_detects_exchange_position_margin_metric_mismatch_for_exchange_sourced_snapshot(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_margin_mismatch",
            portfolio_snapshot_ref="evt_portfolio_derivatives_margin_mismatch",
            product_type="derivatives",
            margin_mode="cross",
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        margin_allocated=320.0,
                        maintenance_margin=140.0,
                        margin_ratio=5.2,
                        liquidation_price=62_000.0,
                        margin_source="exchange",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=20.0,
                total_equity=10_020.0,
                gross_exposure=1420.0,
                net_exposure=1420.0,
                risk_budget_usage={"margin_usage": 320.0},
                product_type="derivatives",
                margin_mode="cross",
                margin_usage=320.0,
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=20.0,
                total_equity=10_020.0,
                gross_exposure=1420.0,
                net_exposure=1420.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=70_000.0,
                        mark_price=71_000.0,
                        side="net",
                        margin_mode="cross",
                        margin_allocated=340.0,
                        maintenance_margin=150.0,
                        margin_ratio=5.6,
                        liquidation_price=61_500.0,
                    )
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="net_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="net_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "SOFT_MISMATCH")
        self.assertFalse(report.review_required)
        self.assertIn("local_position_margin_divergence", report.mismatch_categories)
        self.assertIn(
            "local_position_margin_differs_from_exchange_position_margin",
            report.mismatch_reasons,
        )
        self.assertIn("exchange_margin_state_differs_from_local_snapshot", report.safety_impacts)
        self.assertIn("BTC-USDT-SWAP", report.position_diff["exchange_margin_mismatches"])

    def test_compare_halts_when_exchange_position_margin_mode_conflicts_with_local_snapshot(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_margin_mode_conflict",
            portfolio_snapshot_ref="evt_portfolio_derivatives_margin_mode_conflict",
            product_type="derivatives",
            margin_mode="cross",
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                        margin_allocated=320.0,
                        margin_source="exchange",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=20.0,
                total_equity=10_020.0,
                gross_exposure=1420.0,
                net_exposure=1420.0,
                risk_budget_usage={"margin_usage": 320.0},
                product_type="derivatives",
                margin_mode="cross",
                margin_usage=320.0,
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_qty=0.02,
                        position_notional=1420.0,
                        avg_entry_price=70_000.0,
                        unrealized_pnl=20.0,
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP": 70_000.0},
                realized_pnl=0.0,
                unrealized_pnl=20.0,
                total_equity=10_020.0,
                gross_exposure=1420.0,
                net_exposure=1420.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=70_000.0,
                        mark_price=71_000.0,
                        side="net",
                        margin_mode="isolated",
                        margin_allocated=320.0,
                    )
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="net_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="net_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "HARD_MISMATCH")
        self.assertTrue(report.halt_required)
        self.assertIn("local_position_margin_profile_divergence", report.mismatch_categories)
        self.assertIn(
            "local_position_margin_mode_differs_from_exchange_position_margin_mode",
            report.mismatch_reasons,
        )
        self.assertIn("cross_isolated_margin_mode_is_not_confirmed", report.safety_impacts)
        self.assertIn("BTC-USDT-SWAP", report.position_diff["exchange_margin_mode_mismatches"])

    def test_compare_detects_hard_snapshot_mismatch(self) -> None:
        comparator = StateComparator()
        report = comparator.compare(
            decision_id="decision_1",
            portfolio_snapshot_ref="evt_portfolio_1",
            order_states=[
                OrderState(
                    decision_id="decision_1",
                    intent_id="intent_1",
                    symbol="BTC-USDT",
                    client_order_id="clord_1",
                    exchange_order_id="paper_1",
                    status="FILLED",
                    submitted_ts=utc_now(),
                    last_update_ts=utc_now(),
                    requested_qty=1.0,
                    filled_qty=1.0,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.0,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="fill_1",
                    decision_id="decision_1",
                    intent_id="intent_1",
                    client_order_id="clord_1",
                    exchange_order_id="paper_1",
                    symbol="BTC-USDT",
                    venue="PAPER",
                    side="buy",
                    fill_qty=1.0,
                    fill_price=100.0,
                    fee_amount=0.0,
                    liquidity_role="taker",
                    exchange_timestamp=utc_now(),
                    ingestion_timestamp=utc_now(),
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=utc_now(),
                balances={"USDT": 9_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=0.0,
                        position_notional=0.0,
                        avg_entry_price=0.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=utc_now(),
                balances={"USDT": 9_900.0},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=1.0,
                        position_notional=100.0,
                        avg_entry_price=100.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={"BTC-USDT": 100.0},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=100.0,
                net_exposure=100.0,
                risk_budget_usage={},
            ),
        )

        self.assertEqual(report.severity, "HARD_MISMATCH")
        self.assertTrue(report.halt_required)
        self.assertEqual(report.decision_id, "decision_1")
        self.assertEqual(report.portfolio_snapshot_ref, "evt_portfolio_1")
        self.assertTrue(report.balance_diff["reconstructed"])
        self.assertTrue(report.position_diff["reconstructed_mismatches"])

    def test_compare_detects_exchange_order_and_fill_mismatch(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_2",
            portfolio_snapshot_ref="evt_portfolio_2",
            order_states=[
                OrderState(
                    decision_id="decision_2",
                    intent_id="intent_2",
                    symbol="BTC-USDT",
                    client_order_id="clord_2",
                    venue="OKX",
                    exchange_order_id="ord_2",
                    status="SUBMITTED",
                    exchange_status="live",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.0,
                    remaining_qty=0.001,
                    average_fill_price=None,
                    fees=0.0,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="trade_1",
                    decision_id="decision_2",
                    intent_id="intent_2",
                    client_order_id="clord_2",
                    exchange_order_id="ord_2",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[],
                open_orders=[
                    ExchangeOpenOrder(
                        instrument_id="BTC-USDT",
                        client_order_id="clord_2",
                        exchange_order_id="ord_2",
                        side="buy",
                        order_type="market",
                        status="PARTIALLY_FILLED",
                        quantity=0.001,
                        filled_quantity=0.0005,
                        price=None,
                        created_ts=now,
                        updated_ts=now,
                    )
                ],
                fills=[],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=False,
        )

        self.assertEqual(report.severity, "HARD_MISMATCH")
        self.assertTrue(report.halt_required)
        self.assertIn("local_open_order_divergence", report.mismatch_categories)
        self.assertTrue(report.order_diff["exchange"])
        self.assertTrue(report.fill_diff["exchange"])

    def test_compare_detects_canceled_local_order_missing_on_exchange(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_cancel",
            portfolio_snapshot_ref="evt_portfolio_cancel",
            order_states=[
                OrderState(
                    decision_id="decision_cancel",
                    intent_id="intent_cancel",
                    symbol="BTC-USDT",
                    client_order_id="clord_cancel",
                    venue="OKX",
                    exchange_order_id="ord_cancel",
                    status="CANCELED",
                    exchange_status="canceled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.0004,
                    remaining_qty=0.0006,
                    average_fill_price=100.0,
                    fees=0.01,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="trade_cancel_partial",
                    decision_id="decision_cancel",
                    intent_id="intent_cancel",
                    client_order_id="clord_cancel",
                    exchange_order_id="ord_cancel",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.0004,
                    fill_price=100.0,
                    fee_amount=0.01,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[],
                open_orders=[],
                fills=[],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=False,
        )

        self.assertEqual(report.severity, "SOFT_MISMATCH")
        self.assertFalse(report.halt_required)
        self.assertTrue(report.fill_diff["exchange"])
        self.assertTrue(report.mismatch_reasons)
        self.assertEqual(report.recommended_operator_action, "investigate_state_divergence")

    def test_compare_ignores_blocked_local_order_for_exchange_open_order_diff(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_blocked",
            portfolio_snapshot_ref="evt_portfolio_blocked",
            order_states=[
                OrderState(
                    decision_id="decision_blocked",
                    intent_id="intent_blocked",
                    symbol="BTC-USDT-SWAP",
                    client_order_id="clord_blocked",
                    venue="OKX",
                    exchange_order_id=None,
                    status="BLOCKED",
                    exchange_status=None,
                    submitted_ts=None,
                    last_update_ts=now,
                    last_exchange_update_ts=None,
                    requested_qty=0.0028,
                    filled_qty=0.0,
                    remaining_qty=0.0028,
                    average_fill_price=None,
                    fees=0.0,
                    execution_error="max_open_orders_reached",
                )
            ],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 10_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[],
                open_orders=[],
                fills=[],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=False,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertFalse(report.halt_required)
        self.assertFalse(report.order_diff["exchange"])

    def test_compare_reports_clean_when_local_and_exchange_state_match(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_clean",
            portfolio_snapshot_ref="evt_portfolio_clean",
            order_states=[
                OrderState(
                    decision_id="decision_clean",
                    intent_id="intent_clean",
                    symbol="BTC-USDT",
                    client_order_id="clord_clean",
                    venue="OKX",
                    exchange_order_id="ord_clean",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.001,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.1,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="trade_clean",
                    decision_id="decision_clean",
                    intent_id="intent_clean",
                    client_order_id="clord_clean",
                    exchange_order_id="ord_clean",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 9_999.9},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_999.9,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 9_999.9},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_999.9,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=9_999.9, available=9_999.9, frozen=0.0)],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="trade_clean",
                        exchange_order_id="ord_clean",
                        client_order_id="clord_clean",
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=100.0,
                        fee_amount=0.1,
                        fee_currency="USDT",
                        fill_ts=now,
                    )
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertFalse(report.halt_required)
        self.assertEqual(report.mismatch_reasons, [])

    def test_compare_ignores_spot_position_drift_when_exchange_positions_are_unavailable(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_spot",
            portfolio_snapshot_ref="evt_portfolio_spot",
            order_states=[
                OrderState(
                    decision_id="decision_spot",
                    intent_id="intent_spot",
                    symbol="BTC-USDT",
                    client_order_id="clord_spot",
                    venue="OKX",
                    exchange_order_id="ord_spot",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.001,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.1,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="trade_spot",
                    decision_id="decision_spot",
                    intent_id="intent_spot",
                    client_order_id="clord_spot",
                    exchange_order_id="ord_spot",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 9_899.9, "BTC": 0.001},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=0.001,
                        position_notional=0.1,
                        avg_entry_price=100.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={"BTC-USDT": 100.0},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_999.9,
                gross_exposure=0.1,
                net_exposure=0.1,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 9_899.9, "BTC": 0.001},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=0.001,
                        position_notional=0.1,
                        avg_entry_price=100.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={"BTC-USDT": 100.0},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_999.9,
                gross_exposure=0.1,
                net_exposure=0.1,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(currency="USDT", total=9_899.9, available=9_899.9, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.001, available=0.001, frozen=0.0),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="trade_spot",
                        exchange_order_id="ord_spot",
                        client_order_id="clord_spot",
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=100.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    )
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertEqual(report.position_diff["exchange_mismatches"], {})

    def test_compare_defers_exchange_comparison_until_snapshot_catches_up_with_local_fill(self) -> None:
        comparator = StateComparator()
        snapshot_ts = utc_now()
        fill_ts = snapshot_ts + timedelta(seconds=1)
        report = comparator.compare(
            decision_id="decision_snapshot_lag",
            portfolio_snapshot_ref="evt_portfolio_snapshot_lag",
            order_states=[
                OrderState(
                    decision_id="decision_snapshot_lag",
                    intent_id="intent_snapshot_lag",
                    symbol="BTC-USDT",
                    client_order_id="clord_snapshot_lag",
                    venue="OKX",
                    exchange_order_id="ord_snapshot_lag",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=fill_ts,
                    last_update_ts=fill_ts,
                    last_exchange_update_ts=fill_ts,
                    requested_qty=0.001,
                    filled_qty=0.001,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.1,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="trade_snapshot_lag",
                    decision_id="decision_snapshot_lag",
                    intent_id="intent_snapshot_lag",
                    client_order_id="clord_snapshot_lag",
                    exchange_order_id="ord_snapshot_lag",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    liquidity_role="taker",
                    exchange_timestamp=fill_ts,
                    ingestion_timestamp=fill_ts,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=fill_ts,
                balances={"USDT": 9_899.9, "BTC": 0.001},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=0.001,
                        position_notional=0.1,
                        avg_entry_price=100.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={"BTC-USDT": 100.0},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_999.9,
                gross_exposure=0.1,
                net_exposure=0.1,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=fill_ts,
                balances={"USDT": 9_899.9, "BTC": 0.001},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=0.001,
                        position_notional=0.1,
                        avg_entry_price=100.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={"BTC-USDT": 100.0},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_999.9,
                gross_exposure=0.1,
                net_exposure=0.1,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=snapshot_ts,
                balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
                positions=[],
                open_orders=[],
                fills=[],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertFalse(report.exchange_comparison_enabled)
        self.assertEqual(report.severity, "CLEAN")
        self.assertEqual(report.fill_diff["exchange"], {})
        self.assertEqual(report.balance_diff["exchange"], {})

    def test_compare_ignores_exchange_fills_already_accepted_in_baseline(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_baseline_fill",
            portfolio_snapshot_ref="evt_portfolio_baseline_fill",
            order_states=[
                OrderState(
                    decision_id="decision_baseline_fill",
                    intent_id="intent_baseline_fill",
                    symbol="BTC-USDT",
                    client_order_id="clord_baseline_fill",
                    venue="OKX",
                    exchange_order_id="ord_baseline_fill",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.001,
                    remaining_qty=0.0,
                    average_fill_price=101.0,
                    fees=0.1,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="trade_new",
                    decision_id="decision_baseline_fill",
                    intent_id="intent_baseline_fill",
                    client_order_id="clord_baseline_fill",
                    exchange_order_id="ord_baseline_fill",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=101.0,
                    fee_amount=0.1,
                    fee_currency="USDT",
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 9_899.8, "BTC": 0.001},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_899.8,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 9_899.8, "BTC": 0.001},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_899.8,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="trade_hist_1",
                        exchange_order_id="ord_hist_1",
                        client_order_id=None,
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=99.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    ),
                    ExchangeFill(
                        fill_id="trade_new",
                        exchange_order_id="ord_baseline_fill",
                        client_order_id="clord_baseline_fill",
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=101.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    ),
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=False,
            accepted_exchange_fill_ids={"trade_hist_1"},
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertFalse(report.review_required)
        self.assertEqual(report.fill_diff["exchange"], {})

    def test_compare_classifies_historical_exchange_state_without_local_execution_as_info(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id=None,
            portfolio_snapshot_ref="evt_portfolio_hist",
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0, "BTC": 0.01},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.01, available=0.01, frozen=0.0),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="hist_fill_1",
                        exchange_order_id="ord_hist_1",
                        client_order_id=None,
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.01,
                        fill_price=70000.0,
                        fee_amount=0.0,
                        fill_ts=now,
                    )
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertEqual(report.severity, "INFO")
        self.assertFalse(report.halt_required)
        self.assertFalse(report.review_required)
        self.assertIn("historical_state_only", report.mismatch_categories)
        self.assertEqual(report.recommended_operator_action, "observe_only")

    def test_compare_treats_exchange_drift_after_trusted_baseline_as_review_required(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_baseline_drift",
            portfolio_snapshot_ref="evt_portfolio_baseline_drift",
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(currency="USDT", total=500.0, available=500.0, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.01, available=0.01, frozen=0.0),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="manual_fill_after_baseline",
                        exchange_order_id="ord_ext_1",
                        client_order_id=None,
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.01,
                        fill_price=70000.0,
                        fee_amount=0.0,
                        fill_ts=now,
                    )
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            trusted_exchange_portfolio_baseline=True,
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertIn("external_manual_activity_detected", report.mismatch_categories)
        self.assertNotIn("historical_state_only", report.mismatch_categories)

    def test_compare_classifies_external_manual_activity_as_review_required(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_manual",
            portfolio_snapshot_ref="evt_portfolio_manual",
            order_states=[
                OrderState(
                    decision_id="decision_manual",
                    intent_id="intent_manual",
                    symbol="BTC-USDT",
                    client_order_id="clord_manual",
                    venue="OKX",
                    exchange_order_id="ord_manual",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.001,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.1,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="local_fill_1",
                    decision_id="decision_manual",
                    intent_id="intent_manual",
                    client_order_id="clord_manual",
                    exchange_order_id="ord_manual",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 900.0, "BTC": 0.001},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=900.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 900.0, "BTC": 0.001},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=900.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(currency="USDT", total=850.0, available=850.0, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.002, available=0.002, frozen=0.0),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="local_fill_1",
                        exchange_order_id="ord_manual",
                        client_order_id="clord_manual",
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=100.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    ),
                    ExchangeFill(
                        fill_id="manual_fill_2",
                        exchange_order_id="ord_ext_2",
                        client_order_id=None,
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=101.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    ),
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertFalse(report.halt_required)
        self.assertTrue(report.review_required)
        self.assertIn("external_manual_activity_detected", report.mismatch_categories)
        self.assertEqual(report.recommended_operator_action, "review_and_rebaseline_if_expected")

    def test_compare_uses_bills_summary_as_auxiliary_reconciliation_evidence(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_manual_bills",
            portfolio_snapshot_ref="evt_portfolio_manual_bills",
            order_states=[
                OrderState(
                    decision_id="decision_manual_bills",
                    intent_id="intent_manual_bills",
                    symbol="BTC-USDT",
                    client_order_id="clord_manual_bills",
                    venue="OKX",
                    exchange_order_id="ord_manual_bills",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.001,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.1,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="local_fill_bills_1",
                    decision_id="decision_manual_bills",
                    intent_id="intent_manual_bills",
                    client_order_id="clord_manual_bills",
                    exchange_order_id="ord_manual_bills",
                    symbol="BTC-USDT",
                    venue="OKX",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 900.0, "BTC": 0.001},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=900.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 900.0, "BTC": 0.001},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=900.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(currency="USDT", total=850.0, available=850.0, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.002, available=0.002, frozen=0.0),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="local_fill_bills_1",
                        exchange_order_id="ord_manual_bills",
                        client_order_id="clord_manual_bills",
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=100.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    ),
                    ExchangeFill(
                        fill_id="manual_fill_bills_2",
                        exchange_order_id="ord_ext_bills_2",
                        client_order_id=None,
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=101.0,
                        fee_amount=0.1,
                        fill_ts=now,
                    ),
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            exchange_bills_summary={
                "available": True,
                "count": 2,
                "latest_bill_id": "bill_2",
                "currencies": ["USDT"],
                "top_categories": [{"type": "1", "sub_type": "173", "currency": "USDT", "count": 2}],
            },
        )

        self.assertIn("exchange_bills_activity_available", report.mismatch_categories)
        self.assertIn("recent_exchange_bills_may_explain_exchange_side_balance_activity", report.mismatch_reasons)
        self.assertIn("review_exchange_bills_before_rebaselining", report.safety_impacts)
        self.assertEqual(report.recommended_operator_action, "review_exchange_bills_and_rebaseline_if_expected")
        self.assertEqual(report.exchange_bills_summary["count"], 2)
        self.assertTrue(report.exchange_bills_explanations)
        self.assertEqual(report.exchange_bills_explanations[0]["semantic_group"], "funding_fee")
        self.assertIn("balance_divergence", report.exchange_bills_explanations[0]["likely_explains"])

    def test_compare_does_not_treat_balance_only_funding_fee_drift_as_external_manual_activity(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_balance_only_bills",
            portfolio_snapshot_ref="evt_portfolio_balance_only_bills",
            order_states=[
                OrderState(
                    decision_id="decision_balance_only_bills",
                    intent_id="intent_balance_only_bills",
                    symbol="BTC-USDT-SWAP",
                    client_order_id="clord_balance_only_bills",
                    venue="OKX",
                    exchange_order_id="ord_balance_only_bills",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.0006,
                    filled_qty=0.0006,
                    remaining_qty=0.0,
                    average_fill_price=73648.2,
                    fees=0.02,
                    product_type="derivatives",
                    margin_mode="cross",
                )
            ],
            fills=[
                FillEvent(
                    fill_id="local_fill_balance_only_1",
                    decision_id="decision_balance_only_bills",
                    intent_id="intent_balance_only_bills",
                    client_order_id="clord_balance_only_bills",
                    exchange_order_id="ord_balance_only_bills",
                    symbol="BTC-USDT-SWAP",
                    venue="OKX",
                    side="sell",
                    fill_qty=0.0006,
                    fill_price=73648.2,
                    fee_amount=0.02,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                    product_type="derivatives",
                    margin_mode="cross",
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 394.235366456642},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=394.235366456642,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 394.235366456642},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=394.235366456642,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(
                        currency="USDT",
                        total=394.2361396350809,
                        available=394.2361396350809,
                        frozen=0.0,
                    ),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="local_fill_balance_only_1",
                        exchange_order_id="ord_balance_only_bills",
                        client_order_id="clord_balance_only_bills",
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        side="sell",
                        fill_qty=0.0006,
                        fill_price=73648.2,
                        fee_amount=0.02,
                        fill_ts=now,
                    )
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            exchange_bills_summary={
                "available": True,
                "count": 7,
                "latest_bill_id": "bill_balance_only_7",
                "currencies": ["USDT"],
                "top_categories": [
                    {"type": "2", "sub_type": "5", "currency": "USDT", "count": 5},
                    {"type": "8", "sub_type": "174", "currency": "USDT", "count": 1},
                ],
            },
        )

        self.assertEqual(report.severity, "SOFT_MISMATCH")
        self.assertFalse(report.review_required)
        self.assertNotIn("external_manual_activity_detected", report.mismatch_categories)
        self.assertIn("exchange_bills_activity_available", report.mismatch_categories)
        self.assertIn("local_balance_differs_from_exchange_balance", report.mismatch_reasons)

    def test_compare_keeps_balance_only_manual_transfer_as_external_manual_activity(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_balance_only_transfer",
            portfolio_snapshot_ref="evt_portfolio_balance_only_transfer",
            order_states=[
                OrderState(
                    decision_id="decision_balance_only_transfer",
                    intent_id="intent_balance_only_transfer",
                    symbol="BTC-USDT-SWAP",
                    client_order_id="clord_balance_only_transfer",
                    venue="OKX",
                    exchange_order_id="ord_balance_only_transfer",
                    status="FILLED",
                    exchange_status="filled",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.0006,
                    filled_qty=0.0006,
                    remaining_qty=0.0,
                    average_fill_price=73648.2,
                    fees=0.02,
                    product_type="derivatives",
                    margin_mode="cross",
                )
            ],
            fills=[
                FillEvent(
                    fill_id="local_fill_balance_only_transfer_1",
                    decision_id="decision_balance_only_transfer",
                    intent_id="intent_balance_only_transfer",
                    client_order_id="clord_balance_only_transfer",
                    exchange_order_id="ord_balance_only_transfer",
                    symbol="BTC-USDT-SWAP",
                    venue="OKX",
                    side="sell",
                    fill_qty=0.0006,
                    fill_price=73648.2,
                    fee_amount=0.02,
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                    product_type="derivatives",
                    margin_mode="cross",
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 394.235366456642},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=394.235366456642,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 394.235366456642},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=394.235366456642,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(
                        currency="USDT",
                        total=414.235366456642,
                        available=414.235366456642,
                        frozen=0.0,
                    ),
                ],
                positions=[],
                open_orders=[],
                fills=[
                    ExchangeFill(
                        fill_id="local_fill_balance_only_transfer_1",
                        exchange_order_id="ord_balance_only_transfer",
                        client_order_id="clord_balance_only_transfer",
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        side="sell",
                        fill_qty=0.0006,
                        fill_price=73648.2,
                        fee_amount=0.02,
                        fill_ts=now,
                    )
                ],
                instruments=[],
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
            exchange_bills_summary={
                "available": True,
                "count": 1,
                "latest_bill_id": "bill_balance_only_transfer_1",
                "currencies": ["USDT"],
                "top_categories": [
                    {"type": "1", "sub_type": "201", "currency": "USDT", "count": 1},
                ],
            },
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertIn("external_manual_activity_detected", report.mismatch_categories)
        self.assertEqual(report.recommended_operator_action, "review_exchange_bills_and_rebaseline_if_expected")

    def test_service_uses_exchange_bootstrap_snapshot_as_reconstruction_baseline(self) -> None:
        now = utc_now()
        baseline_snapshot = PortfolioSnapshot(
            snapshot_ts=now,
            balances={"USDT": 75_630.13129751521, "OKB": 100.0, "ETH": 1.0, "BTC": 5.736e-9},
            positions=[],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=75_630.13129751521,
            gross_exposure=0.0,
            net_exposure=0.0,
            risk_budget_usage={},
        )

        class _PortfolioRepo:
            def latest(self):
                return baseline_snapshot

            def history(self):
                return [baseline_snapshot]

            def save_snapshot(self, snapshot):
                raise AssertionError("save_snapshot should not be called in this unit test")

        class _ExecutionRepo:
            def order_states(self):
                return []

            def fills(self):
                return []

        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=None,  # type: ignore[arg-type]
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=None,  # type: ignore[arg-type]
            execution_repo=_ExecutionRepo(),  # type: ignore[arg-type]
            portfolio_repo=_PortfolioRepo(),  # type: ignore[arg-type]
            event_store=None,  # type: ignore[arg-type]
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            bootstrap_portfolio_from_exchange=True,
            metrics=None,
        )

        reconstructed = service._rebuild_snapshot_for_comparison(
            stored_snapshot=baseline_snapshot,
            fills=[],
        )

        self.assertEqual(reconstructed.balances, baseline_snapshot.balances)
        self.assertEqual(reconstructed.total_equity, baseline_snapshot.total_equity)

    def test_service_ignores_latest_baseline_fill_history_during_exchange_fill_comparison(self) -> None:
        now = utc_now()
        stored_snapshot = PortfolioSnapshot(
            snapshot_ts=now,
            decision_id="decision_1",
            source_fill_id="trade_new_1",
            balances={"USDT": 9_999.799, "BTC": 0.001},
            positions=[
                Position(
                    symbol="BTC-USDT",
                    position_qty=0.001,
                    position_notional=0.101,
                    avg_entry_price=101.0,
                    unrealized_pnl=0.0,
                )
            ],
            cost_basis={"BTC-USDT": 101.0},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=9_999.9,
            gross_exposure=0.101,
            net_exposure=0.101,
            risk_budget_usage={},
        )
        exchange_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=now,
            balances=[
                ExchangeBalance(currency="USDT", total=9_999.799, available=9_999.799, frozen=0.0),
                ExchangeBalance(currency="BTC", total=0.001, available=0.001, frozen=0.0),
            ],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    quantity=0.001,
                    average_entry_price=101.0,
                )
            ],
            open_orders=[],
            fills=[
                ExchangeFill(
                    fill_id="trade_hist_1",
                    exchange_order_id="ord_hist_1",
                    client_order_id=None,
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100.0,
                    fee_amount=0.1,
                    fill_ts=now,
                ),
                ExchangeFill(
                    fill_id="trade_new_1",
                    exchange_order_id="ord_new_1",
                    client_order_id="clord_new_1",
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=101.0,
                    fee_amount=0.1,
                    fill_ts=now,
                ),
            ],
            instruments=[],
        )

        class _PortfolioRepo:
            def latest(self):
                return stored_snapshot

            def history(self):
                return [stored_snapshot]

        class _ExecutionRepo:
            def order_states(self):
                return [
                    OrderState(
                        decision_id="decision_1",
                        intent_id="intent_1",
                        symbol="BTC-USDT",
                        client_order_id="clord_new_1",
                        venue="OKX",
                        exchange_order_id="ord_new_1",
                        status="FILLED",
                        exchange_status="filled",
                        submitted_ts=now,
                        last_update_ts=now,
                        last_exchange_update_ts=now,
                        requested_qty=0.001,
                        filled_qty=0.001,
                        remaining_qty=0.0,
                        average_fill_price=101.0,
                        fees=0.1,
                    )
                ]

            def fills(self):
                return [
                    FillEvent(
                        fill_id="trade_new_1",
                        decision_id="decision_1",
                        intent_id="intent_1",
                        client_order_id="clord_new_1",
                        exchange_order_id="ord_new_1",
                        symbol="BTC-USDT",
                        venue="OKX",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=101.0,
                        fee_amount=0.1,
                        fee_currency="USDT",
                        liquidity_role="taker",
                        exchange_timestamp=now,
                        ingestion_timestamp=now,
                    )
                ]

        class _EventStore:
            def latest(self, topic: str):
                if topic != topics.ACCOUNT_BASELINES:
                    return None
                return build_envelope(
                    topic=topics.ACCOUNT_BASELINES,
                    key="okx",
                    payload_model=AccountBaselineSnapshot(
                        account_source="okx",
                        exchange_snapshot_ts=now,
                        imported_at=now,
                        baseline_status="rebaseline_completed",
                        fills=[
                            ExchangeFill(
                                fill_id="trade_hist_1",
                                exchange_order_id="ord_hist_1",
                                client_order_id=None,
                                instrument_id="BTC-USDT",
                                symbol="BTC-USDT",
                                side="buy",
                                fill_qty=0.001,
                                fill_price=100.0,
                                fee_amount=0.1,
                                fill_ts=now,
                            )
                        ],
                    ),
                    source_component="test",
                )

            def by_topic(self, topic: str):
                latest = self.latest(topic)
                return [latest] if latest is not None else []

        class _AccountService:
            def latest_snapshot(self):
                return exchange_snapshot

        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=None,  # type: ignore[arg-type]
            fetcher=ExchangeStateFetcher(account_service=_AccountService()),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=None,  # type: ignore[arg-type]
            execution_repo=_ExecutionRepo(),  # type: ignore[arg-type]
            portfolio_repo=_PortfolioRepo(),  # type: ignore[arg-type]
            event_store=_EventStore(),  # type: ignore[arg-type]
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        report = service._build_report(
            decision_id="decision_1",
            portfolio_snapshot_ref="evt_portfolio_1",
            stored_snapshot=stored_snapshot,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertEqual(report.fill_diff["exchange"], {})

    def test_service_filters_exchange_bills_acknowledged_by_operator_rebaseline(self) -> None:
        now = utc_now()
        stored_snapshot = PortfolioSnapshot(
            snapshot_ts=now,
            balances={"USDT": 10_000.0},
            positions=[
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:long",
                    position_qty=0.0036,
                    position_notional=250.0,
                    avg_entry_price=69_298.8,
                    unrealized_pnl=0.0,
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    margin_allocated=25.057692,
                    maintenance_margin=1.00230768,
                    margin_ratio=88.987015892055,
                    liquidation_price=41_920.72782242313,
                    margin_source="exchange",
                )
            ],
            cost_basis={"BTC-USDT-SWAP:long": 69_298.8},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=10_000.0,
            gross_exposure=250.0,
            net_exposure=250.0,
            risk_budget_usage={},
            product_type="derivatives",
            margin_mode="cross",
        )
        baseline_imported_at = now
        historical_bill_ts = now - timedelta(hours=1)
        exchange_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=now,
            balances=[ExchangeBalance(currency="USDT", total=10_000.0, available=10_000.0, frozen=0.0)],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=0.0036,
                    average_entry_price=69_298.8,
                    mark_price=69_500.0,
                    side="long",
                    margin_mode="cross",
                    margin_allocated=25.031772,
                    maintenance_margin=1.00127088,
                    margin_ratio=88.84905296445741,
                    liquidation_price=41_920.72782242313,
                )
            ],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="futures",
            position_mode="long_short_mode",
            account_configuration=ExchangeAccountConfiguration(position_mode="long_short_mode"),
        )

        class _PortfolioRepo:
            def latest(self):
                return stored_snapshot

            def history(self):
                return [stored_snapshot]

        class _ExecutionRepo:
            def order_states(self):
                return []

            def fills(self):
                return []

        class _EventStore:
            def latest(self, topic: str):
                if topic != topics.ACCOUNT_BASELINES:
                    return None
                return build_envelope(
                    topic=topics.ACCOUNT_BASELINES,
                    key="okx",
                    payload_model=AccountBaselineSnapshot(
                        account_source="okx",
                        exchange_snapshot_ts=baseline_imported_at,
                        imported_at=baseline_imported_at,
                        product_type="derivatives",
                        margin_mode="cross",
                        baseline_status="rebaseline_completed",
                        baseline_kind="operator_rebaseline",
                    ),
                    source_component="test",
                )

            def by_topic(self, topic: str):
                latest = self.latest(topic)
                return [latest] if latest is not None else []

        class _AccountService:
            def latest_snapshot(self):
                return exchange_snapshot

            def latest_recent_bills(self):
                return [
                    {
                        "billId": "bill_hist_1",
                        "type": "2",
                        "subType": "5",
                        "ccy": "USDT",
                        "ts": str(int(historical_bill_ts.timestamp() * 1000)),
                    }
                ]

            def recent_bills_summary(self):
                return {
                    "available": True,
                    "count": 1,
                    "latest_bill_id": "bill_hist_1",
                    "latest_bill_ts": historical_bill_ts,
                    "currencies": ["USDT"],
                    "top_categories": [{"type": "2", "sub_type": "5", "currency": "USDT", "count": 1}],
                    "funding_fee_summary": {
                        "available": False,
                        "count": 0,
                        "latest_bill_ts": None,
                        "currencies": [],
                        "net_total_by_currency": {},
                        "absolute_total_by_currency": {},
                        "current_position_notional_usd": None,
                        "funding_fee_bps_proxy": None,
                    },
                    "last_error": None,
                }

            def recent_bills_summary_since(self, *, since_ts=None):
                _ = since_ts
                return {
                    "available": False,
                    "count": 0,
                    "latest_bill_id": None,
                    "latest_bill_ts": None,
                    "currencies": [],
                    "top_categories": [],
                    "funding_fee_summary": {
                        "available": False,
                        "count": 0,
                        "latest_bill_ts": None,
                        "currencies": [],
                        "net_total_by_currency": {},
                        "absolute_total_by_currency": {},
                        "current_position_notional_usd": None,
                        "funding_fee_bps_proxy": None,
                    },
                    "last_error": None,
                }

        service = ReconciliationService(
            settings=AATSSettings.model_validate({"trading_product_type": "derivatives", "margin_mode": "cross"}),
            bus=InMemoryEventBus(),
            fetcher=ExchangeStateFetcher(account_service=_AccountService()),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=_ExecutionRepo(),  # type: ignore[arg-type]
            portfolio_repo=_PortfolioRepo(),  # type: ignore[arg-type]
            event_store=_EventStore(),  # type: ignore[arg-type]
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            bootstrap_portfolio_from_exchange=True,
        )

        report = service._build_report(
            decision_id="decision_rebaseline_bills",
            portfolio_snapshot_ref="evt_portfolio_rebaseline_bills",
            stored_snapshot=stored_snapshot,
        )

        self.assertEqual(report.exchange_bills_summary["count"], 0)
        self.assertNotIn("exchange_bills_activity_available", report.mismatch_categories)
        self.assertEqual(report.severity, "SOFT_MISMATCH")
        self.assertFalse(report.review_required)

    def test_service_ignores_local_exchange_fills_older_than_visible_exchange_window(self) -> None:
        now = utc_now()
        old_fill_ts = now - timedelta(hours=2)
        recent_fill_ts = now - timedelta(minutes=1)
        local_fills = [
            FillEvent(
                fill_id="trade_old_visible",
                decision_id="decision_window",
                intent_id="intent_old_visible",
                client_order_id="clord_old_visible",
                exchange_order_id="ord_old_visible",
                symbol="BTC-USDT",
                venue="OKX",
                side="buy",
                fill_qty=0.001,
                fill_price=100.0,
                fee_amount=0.1,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=old_fill_ts,
                ingestion_timestamp=old_fill_ts,
            ),
            FillEvent(
                fill_id="trade_recent_visible",
                decision_id="decision_window",
                intent_id="intent_recent_visible",
                client_order_id="clord_recent_visible",
                exchange_order_id="ord_recent_visible",
                symbol="BTC-USDT",
                venue="OKX",
                side="buy",
                fill_qty=0.001,
                fill_price=101.0,
                fee_amount=0.1,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=recent_fill_ts,
                ingestion_timestamp=recent_fill_ts,
            ),
        ]
        reconstruction_service = PortfolioReconstructionService(
            initial_usdt_balance=10_000.0,
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
        )
        stored_snapshot = reconstruction_service.rebuild_snapshot(
            fills=local_fills,
            price_provider=lambda _symbol: 0.0,
        ).model_copy(
            update={
                "decision_id": "decision_window",
                "source_fill_id": "trade_recent_visible",
            }
        )
        exchange_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=now,
            balances=[
                ExchangeBalance(
                    currency="USDT",
                    total=float(stored_snapshot.balances["USDT"]),
                    available=float(stored_snapshot.balances["USDT"]),
                    frozen=0.0,
                ),
                ExchangeBalance(currency="BTC", total=0.002, available=0.002, frozen=0.0),
            ],
            positions=[],
            open_orders=[],
            fills=[
                ExchangeFill(
                    fill_id="trade_recent_visible",
                    exchange_order_id="ord_recent_visible",
                    client_order_id="clord_recent_visible",
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=101.0,
                    fee_amount=0.1,
                    fill_ts=recent_fill_ts,
                )
            ],
            instruments=[],
        )

        class _PortfolioRepo:
            def latest(self):
                return stored_snapshot

            def history(self):
                return [stored_snapshot]

        class _ExecutionRepo:
            def order_states(self):
                return [
                    OrderState(
                        decision_id="decision_window",
                        intent_id="intent_old_visible",
                        symbol="BTC-USDT",
                        client_order_id="clord_old_visible",
                        venue="OKX",
                        exchange_order_id="ord_old_visible",
                        status="FILLED",
                        exchange_status="filled",
                        submitted_ts=old_fill_ts,
                        last_update_ts=old_fill_ts,
                        last_exchange_update_ts=old_fill_ts,
                        requested_qty=0.001,
                        filled_qty=0.001,
                        remaining_qty=0.0,
                        average_fill_price=100.0,
                        fees=0.1,
                    ),
                    OrderState(
                        decision_id="decision_window",
                        intent_id="intent_recent_visible",
                        symbol="BTC-USDT",
                        client_order_id="clord_recent_visible",
                        venue="OKX",
                        exchange_order_id="ord_recent_visible",
                        status="FILLED",
                        exchange_status="filled",
                        submitted_ts=recent_fill_ts,
                        last_update_ts=recent_fill_ts,
                        last_exchange_update_ts=recent_fill_ts,
                        requested_qty=0.001,
                        filled_qty=0.001,
                        remaining_qty=0.0,
                        average_fill_price=101.0,
                        fees=0.1,
                    ),
                ]

            def fills(self):
                return list(local_fills)

        class _AccountService:
            def latest_snapshot(self):
                return exchange_snapshot

        service = ReconciliationService(
            settings=AATSSettings.model_validate({"okx_fill_fetch_limit": 1}),
            bus=None,  # type: ignore[arg-type]
            fetcher=ExchangeStateFetcher(account_service=_AccountService()),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=None,  # type: ignore[arg-type]
            execution_repo=_ExecutionRepo(),  # type: ignore[arg-type]
            portfolio_repo=_PortfolioRepo(),  # type: ignore[arg-type]
            event_store=InMemoryEventStore(),  # type: ignore[arg-type]
            reconstruction_service=reconstruction_service,
            price_provider=lambda _symbol: 0.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        report = service._build_report(
            decision_id="decision_window",
            portfolio_snapshot_ref="evt_portfolio_window",
            stored_snapshot=stored_snapshot,
        )

        self.assertEqual(report.severity, "CLEAN")
        self.assertEqual(report.fill_diff["exchange"], {})

    def test_compare_marks_derivatives_exchange_position_without_local_execution_chain_as_review_required(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_only_reduce",
            portfolio_snapshot_ref="evt_portfolio_derivatives_only_reduce",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            order_states=[],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=980.0, available=980.0, frozen=0.0)],
                positions=[
                    ExchangePosition(
                        instrument_id="BTC-USDT-SWAP",
                        symbol="BTC-USDT-SWAP",
                        quantity=0.02,
                        average_entry_price=65000.0,
                        mark_price=65500.0,
                        side="long",
                    )
                ],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="net_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="net_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertFalse(report.halt_required)
        self.assertTrue(report.only_reduce_required)
        self.assertTrue(report.structural_review_required)
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", report.mismatch_categories)
        self.assertIn("derivatives_exchange_position_not_replayed_locally", report.mismatch_reasons)
        self.assertEqual(report.recommended_operator_action, "go_close_position_on_exchange")
        self.assertEqual(report.unknown_state_details[0]["kind"], "exchange_position_without_local_execution_chain")

    def test_compare_halts_derivatives_when_local_position_mode_conflicts_with_exchange_account_configuration(self) -> None:
        comparator = StateComparator()
        now = utc_now()
        report = comparator.compare(
            decision_id="decision_derivatives_mode_conflict",
            portfolio_snapshot_ref="evt_portfolio_derivatives_mode_conflict",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            order_states=[
                OrderState(
                    decision_id="decision_derivatives_mode_conflict",
                    intent_id="intent_derivatives_mode_conflict",
                    symbol="BTC-USDT-SWAP",
                    client_order_id="clord_derivatives_mode_conflict",
                    venue="OKX",
                    exchange_order_id="ord_derivatives_mode_conflict",
                    status="SUBMITTED",
                    exchange_status="live",
                    submitted_ts=now,
                    last_update_ts=now,
                    last_exchange_update_ts=now,
                    requested_qty=0.01,
                    filled_qty=0.0,
                    remaining_qty=0.01,
                    average_fill_price=None,
                    fees=0.0,
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    submission_payload={},
                )
            ],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            ),
            exchange_snapshot=ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
                positions=[],
                open_orders=[
                    ExchangeOpenOrder(
                        instrument_id="BTC-USDT-SWAP",
                        client_order_id="clord_derivatives_mode_conflict",
                        exchange_order_id="ord_derivatives_mode_conflict",
                        side="buy",
                        order_type="market",
                        status="LIVE",
                        quantity=0.01,
                        filled_quantity=0.0,
                        created_ts=now,
                        updated_ts=now,
                    )
                ],
                fills=[],
                instruments=[],
                account_mode="futures",
                position_mode="net_mode",
                account_configuration=ExchangeAccountConfiguration(position_mode="net_mode"),
            ),
            exchange_comparison_enabled=True,
            compare_exchange_portfolio=True,
        )

        self.assertEqual(report.severity, "HARD_MISMATCH")
        self.assertTrue(report.halt_required)
        self.assertIn("derivatives_position_mode_mismatch", report.mismatch_categories)
        self.assertIn(
            "derivatives_local_position_mode_differs_from_exchange_account_configuration",
            report.mismatch_reasons,
        )
        self.assertEqual(report.recommended_operator_action, "halt_execution_and_investigate_state_divergence")

    def test_compare_reports_unknown_submit_as_soft_finding_before_review_threshold(self) -> None:
        now = utc_now()
        comparator = StateComparator(
            settings=AATSSettings.model_validate({"execution_unknown_submit_review_after_seconds": 300.0})
        )
        report = comparator.compare(
            decision_id="decision_unknown_submit_soft",
            portfolio_snapshot_ref="evt_unknown_submit_soft",
            order_states=[
                OrderState(
                    decision_id="decision_unknown_submit_soft",
                    execution_chain_id="chain_unknown_submit_soft",
                    intent_id="intent_unknown_submit_soft",
                    symbol="BTC-USDT",
                    client_order_id="clord_unknown_submit_soft",
                    venue="OKX",
                    exchange_order_id=None,
                    status="SUBMITTED",
                    exchange_status="live",
                    submitted_ts=now,
                    last_update_ts=now,
                    requested_qty=0.001,
                    filled_qty=0.0,
                    remaining_qty=0.001,
                    average_fill_price=None,
                    fees=0.0,
                    execution_error="submission_unknown_check_exchange:OKXRequestError",
                )
            ],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
        )

        self.assertEqual(report.severity, "SOFT_MISMATCH")
        self.assertFalse(report.review_required)
        self.assertEqual(report.recommended_operator_action, "refresh_exchange_state_for_unknown_write")
        self.assertEqual(report.unknown_state_details[0]["kind"], "unknown_submit_unresolved")
        finding = next(
            item for item in report.findings if item.finding_type == "unknown_submit_unresolved"
        )
        self.assertEqual(finding.severity_class, "soft")
        self.assertFalse(finding.review_required)
        self.assertEqual(finding.reason_code, "unknown_submit_requires_exchange_reconciliation")

    def test_compare_escalates_aged_unknown_cancel_to_review_required(self) -> None:
        now = utc_now()
        comparator = StateComparator(
            settings=AATSSettings.model_validate({"execution_unknown_cancel_review_after_seconds": 30.0})
        )
        report = comparator.compare(
            decision_id="decision_unknown_cancel_review",
            portfolio_snapshot_ref="evt_unknown_cancel_review",
            order_states=[
                OrderState(
                    decision_id="decision_unknown_cancel_review",
                    execution_chain_id="chain_unknown_cancel_review",
                    intent_id="intent_unknown_cancel_review",
                    symbol="BTC-USDT",
                    client_order_id="clord_unknown_cancel_review",
                    venue="OKX",
                    exchange_order_id="ord_unknown_cancel_review",
                    status="CANCEL_PENDING",
                    exchange_status="live",
                    submitted_ts=now - timedelta(minutes=2),
                    cancellation_requested_ts=now - timedelta(minutes=2),
                    last_update_ts=now - timedelta(minutes=2),
                    requested_qty=0.001,
                    filled_qty=0.0,
                    remaining_qty=0.001,
                    average_fill_price=None,
                    fees=0.0,
                    execution_error="cancel_unknown_check_exchange:OKXRequestError",
                )
            ],
            fills=[],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 1000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
        )

        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertEqual(report.recommended_operator_action, "review_unknown_write_and_refresh_exchange_state")
        self.assertEqual(report.unknown_state_details[0]["kind"], "unknown_cancel_unresolved")
        self.assertTrue(report.unknown_state_details[0]["operator_review_required"])
        finding = next(
            item for item in report.findings if item.finding_type == "unknown_cancel_unresolved"
        )
        self.assertEqual(finding.severity_class, "review")
        self.assertTrue(finding.review_required)
        self.assertTrue(finding.blocks_resume)
        self.assertEqual(finding.reason_code, "unknown_cancel_requires_exchange_reconciliation")


class TestReconciliationServiceIdempotency(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_portfolio_snapshot_event_does_not_create_duplicate_report(self) -> None:
        now = utc_now()
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        reconciliation_repo = InMemoryReconciliationRepository()
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=bus,
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=reconciliation_repo,
            execution_repo=InMemoryExecutionRepository(),
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )
        snapshot = PortfolioSnapshot(
            snapshot_ts=now,
            decision_id="decision_recon_dup",
            source_fill_id="fill_recon_dup",
            balances={"USDT": 9_950.0, "BTC": 0.001},
            positions=[
                Position(
                    symbol="BTC-USDT",
                    position_qty=0.001,
                    position_notional=50.0,
                    avg_entry_price=50_000.0,
                    unrealized_pnl=0.0,
                )
            ],
            cost_basis={"BTC-USDT": 50_000.0},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=10_000.0,
            gross_exposure=50.0,
            net_exposure=50.0,
            risk_budget_usage={},
        )
        envelope = build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component="test",
        )
        message = {
            "topic": topics.PORTFOLIO_SNAPSHOTS,
            "key": "portfolio",
            "payload": envelope.model_dump(mode="json"),
        }

        await service.handle_portfolio_snapshot(message)
        await service.handle_portfolio_snapshot(message)

        self.assertEqual(len(reconciliation_repo.history()), 1)
        self.assertEqual(event_store.count(topic=topics.RECONCILIATION_REPORTS), 1)
        report = reconciliation_repo.latest()
        self.assertIsNotNone(report)
        self.assertEqual(report.portfolio_snapshot_ref, envelope.event_id)

    async def test_repair_missing_portfolio_snapshot_rebuilds_and_publishes_snapshot(self) -> None:
        now = utc_now()
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        execution_repo = InMemoryExecutionRepository()
        portfolio_repo = InMemoryPortfolioRepository()
        reconciliation_repo = InMemoryReconciliationRepository()
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=bus,
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=reconciliation_repo,
            execution_repo=execution_repo,
            portfolio_repo=portfolio_repo,
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 100.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )
        await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, service.handle_portfolio_snapshot)
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_chain_repair_1",
                decision_id="decision_chain_repair_1",
                intent_id="intent_chain_repair_1",
                client_order_id="clord_chain_repair_1",
                exchange_order_id="paper_chain_repair_1",
                symbol="BTC-USDT",
                venue="PAPER",
                side="buy",
                fill_qty=0.001,
                fill_price=100.0,
                fee_amount=0.001,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=now,
                ingestion_timestamp=now,
                product_type="spot",
                margin_mode="cash",
            )
        )

        snapshot = await service.repair_missing_portfolio_snapshot(reason="unit_test")

        self.assertIsNotNone(snapshot)
        latest_snapshot = portfolio_repo.latest()
        self.assertIsNotNone(latest_snapshot)
        self.assertEqual(latest_snapshot.source_fill_id, "fill_chain_repair_1")
        self.assertEqual(latest_snapshot.snapshot_origin, "recovery_rebuild")
        self.assertEqual(event_store.count(topic=topics.PORTFOLIO_SNAPSHOTS), 1)
        self.assertEqual(event_store.count(topic=topics.RECONCILIATION_REPORTS), 1)
        report = reconciliation_repo.latest()
        self.assertIsNotNone(report)
        self.assertEqual(report.decision_id, "decision_chain_repair_1")

    async def test_repair_missing_portfolio_snapshot_is_noop_when_latest_fill_already_snapshotted(self) -> None:
        now = utc_now()
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        execution_repo = InMemoryExecutionRepository()
        portfolio_repo = InMemoryPortfolioRepository()
        reconciliation_repo = InMemoryReconciliationRepository()
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=bus,
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=reconciliation_repo,
            execution_repo=execution_repo,
            portfolio_repo=portfolio_repo,
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 100.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )
        await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, service.handle_portfolio_snapshot)
        fill = FillEvent(
            fill_id="fill_chain_repair_noop",
            decision_id="decision_chain_repair_noop",
            intent_id="intent_chain_repair_noop",
            client_order_id="clord_chain_repair_noop",
            exchange_order_id="paper_chain_repair_noop",
            symbol="BTC-USDT",
            venue="PAPER",
            side="buy",
            fill_qty=0.001,
            fill_price=100.0,
            fee_amount=0.001,
            fee_currency="USDT",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
            product_type="spot",
            margin_mode="cash",
        )
        execution_repo.save_fill(fill)
        first_snapshot = await service.repair_missing_portfolio_snapshot(reason="unit_test")

        second_snapshot = await service.repair_missing_portfolio_snapshot(reason="unit_test")

        self.assertIsNotNone(first_snapshot)
        self.assertIsNone(second_snapshot)
        self.assertEqual(len(portfolio_repo.history()), 1)
        self.assertEqual(event_store.count(topic=topics.PORTFOLIO_SNAPSHOTS), 1)


if __name__ == "__main__":
    unittest.main()
