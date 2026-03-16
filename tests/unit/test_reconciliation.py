from __future__ import annotations

import unittest
from datetime import timedelta

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import (
    AccountBaselineSnapshot,
    ExchangeAccountSnapshot,
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


class TestReconciliationComparator(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
