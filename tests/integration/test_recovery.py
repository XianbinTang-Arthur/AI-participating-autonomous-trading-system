from __future__ import annotations

import os
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.schemas.exchange import (
    ExchangeAccountSnapshot,
    ExchangeBalance,
    ExchangeFill,
    ExchangeOpenOrder,
    ExchangePosition,
)
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.operator.query_service import OperatorQueryService
from aats.storage.sqlalchemy_models import EventEnvelopeModel, PortfolioSnapshotModel, ReconciliationReportModel
from tests.support.postgres import temporary_postgres_url


class FakeBaselineAccountService:
    SNAPSHOT: ExchangeAccountSnapshot | None = None

    def __init__(self, *, settings, client, private_ws_client=None) -> None:
        self.settings = settings
        self.client = client
        self.private_ws_client = private_ws_client
        self._snapshot = self.SNAPSHOT

    async def refresh(self, *, force: bool = False):
        return self._snapshot

    async def run_private_ws_forever(self) -> None:
        return None

    async def stop_private_ws(self) -> None:
        return None

    def latest_snapshot(self):
        return self._snapshot

    def instrument_metadata(self, symbol: str):
        return None

    def open_order_count(self, symbol: str | None = None) -> int:
        if self._snapshot is None:
            return 0
        return len(self._snapshot.open_orders)

    def recent_fills(self, symbol: str | None = None):
        if self._snapshot is None:
            return []
        return list(self._snapshot.fills)

    def status(self):
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": self._snapshot is not None,
            "fresh": self._snapshot is not None,
            "last_update_ts": self._snapshot.fetched_at if self._snapshot is not None else None,
            "last_error": None,
            "ready": self._snapshot is not None,
            "detail": "fake_baseline_account",
            "blockers": [] if self._snapshot is not None else ["account_snapshot_missing"],
        }


class TestRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_recovers_portfolio_state_from_persisted_storage(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                original_snapshot = runtime.portfolio_repo.latest()
                self.assertIsNotNone(original_snapshot)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertEqual(recovered_runtime.recovery_status.status, "recovered")
                self.assertFalse(recovered_runtime.recovery_status.halted)
                self.assertTrue(recovered_runtime.recovery_status.safe_startup)
                self.assertIsNone(recovered_runtime.recovery_status.recovery_action)
                self.assertEqual(recovered_runtime.recovery_status.divergence_count, 0)
                recovered_snapshot = recovered_runtime.portfolio_repo.latest()
                self.assertIsNotNone(recovered_snapshot)
                self.assertEqual(
                    recovered_snapshot.model_dump(mode="json"),
                    original_snapshot.model_dump(mode="json"),
                )
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_runtime_rebuilds_snapshot_from_fills_when_snapshot_missing(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                self._delete_portfolio_snapshots(runtime)
                self._delete_event_topic(runtime, topics.PORTFOLIO_SNAPSHOTS)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertEqual(recovered_runtime.recovery_status.status, "recovered")
                self.assertTrue(recovered_runtime.recovery_status.rebuilt_snapshot_saved)
                self.assertTrue(recovered_runtime.recovery_status.recovered_snapshot_available)
                self.assertIsNotNone(recovered_runtime.portfolio_repo.latest())
                self.assertIsNotNone(recovered_runtime.event_store.latest(topics.PORTFOLIO_SNAPSHOTS))
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_recovered_runtime_republishes_scoped_portfolio_snapshot_for_new_decisions(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                self._delete_event_topic(runtime, topics.PORTFOLIO_SNAPSHOTS)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertIsNotNone(recovered_runtime.event_store.latest(topics.PORTFOLIO_SNAPSHOTS))
                decision_count_before = recovered_runtime.event_store.count(topic=topics.DECISION_CONTEXTS)
                await recovered_runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=1,
                    interval_seconds=0.0,
                )
                decision_count_after = recovered_runtime.event_store.count(topic=topics.DECISION_CONTEXTS)
                self.assertGreater(decision_count_after, decision_count_before)
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_runtime_enters_safe_halt_when_execution_state_has_no_reconciliation_context(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                self._delete_reconciliation_reports(runtime)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertEqual(recovered_runtime.recovery_status.status, "recovered_halted")
                self.assertTrue(recovered_runtime.recovery_status.halted)
                self.assertFalse(recovered_runtime.recovery_status.safe_startup)
                self.assertFalse(recovered_runtime.recovery_status.recovered_reconciliation_available)
                self.assertEqual(
                    recovered_runtime.recovery_status.recovery_action,
                    "halted_missing_reconciliation_context",
                )
                self.assertIn("halted_missing_reconciliation_context", recovered_runtime.recovery_status.notes)
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_runtime_imports_clean_account_baseline_on_startup(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        FakeBaselineAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )

        with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
            runtime = await build_runtime(settings)

        baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(baseline)
        self.assertTrue(runtime.recovery_status.baseline_imported)
        self.assertEqual(runtime.recovery_status.baseline_status, "baseline_imported")
        self.assertTrue(runtime.recovery_status.safe_startup)
        self.assertFalse(runtime.recovery_status.halted)
        self.assertEqual(runtime.recovery_status.baseline_balance_count, 1)
        self.assertEqual(runtime.recovery_status.baseline_open_order_count, 0)
        self.assertIsNotNone(runtime.portfolio_repo.latest())
        self.assertEqual(runtime.portfolio_repo.latest().balances["USDT"], 1000.0)

    async def test_runtime_imports_non_empty_account_baseline_and_reconstructs_local_portfolio(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        FakeBaselineAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[
                ExchangeBalance(currency="USDT", total=900.0, available=900.0, frozen=0.0),
                ExchangeBalance(currency="BTC", total=0.01, available=0.01, frozen=0.0),
            ],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    quantity=0.01,
                    average_entry_price=70000.0,
                )
            ],
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
                    fill_ts=utc_now(),
                )
            ],
            instruments=[],
            account_mode="cash",
        )

        with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
            runtime = await build_runtime(settings)

        baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(baseline)
        self.assertTrue(runtime.recovery_status.baseline_imported)
        self.assertEqual(runtime.recovery_status.baseline_status, "baseline_imported")
        self.assertEqual(runtime.recovery_status.baseline_position_count, 1)
        self.assertEqual(runtime.recovery_status.baseline_fill_count, 1)
        latest_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(latest_snapshot)
        self.assertEqual(latest_snapshot.balances["BTC"], Decimal("0.010000000000"))
        self.assertEqual(latest_snapshot.positions[0].symbol, "BTC-USDT")
        self.assertEqual(latest_snapshot.positions[0].position_qty, Decimal("0.010000000000"))

    async def test_derivatives_runtime_ignores_persisted_spot_state_when_selecting_recovery_context(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            spot_settings = self._postgres_settings(database_url)
            runtime = await build_runtime(spot_settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=spot_settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                spot_snapshot = runtime.portfolio_repo.latest()
                self.assertIsNotNone(spot_snapshot)
                self.assertEqual(spot_snapshot.product_type, "spot")
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            derivatives_settings = AATSSettings.model_validate(
                {
                    **spot_settings.model_dump(),
                    "mode": "guarded_live",
                    "execution_backend": "okx",
                    "account_backend": "okx",
                    "account_read_enabled": True,
                    "okx_simulated_trading": True,
                    "live_submit_enabled": False,
                    "guarded_execution_dry_run": True,
                    "bootstrap_portfolio_from_exchange": True,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                }
            )
            FakeBaselineAccountService.SNAPSHOT = ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=utc_now(),
                balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
                positions=[],
                open_orders=[],
                fills=[],
                instruments=[],
                account_mode="cross",
            )

            with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
                recovered_runtime = await build_runtime(derivatives_settings)

            try:
                query = OperatorQueryService(recovered_runtime)
                self.assertFalse(recovered_runtime.recovery_status.halted)
                self.assertEqual(recovered_runtime.recovery_status.recovered_fill_count, 0)
                scoped_snapshot = query.portfolio_latest()["portfolio"]
                self.assertIsNotNone(scoped_snapshot)
                self.assertEqual(scoped_snapshot["product_type"], "derivatives")
                self.assertEqual(scoped_snapshot["margin_mode"], "cross")
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_reconciliation_detects_exchange_drift_after_clean_baseline_before_local_execution(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        FakeBaselineAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )

        with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
            runtime = await build_runtime(settings)

        runtime.account_service._snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now() + timedelta(seconds=5),
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
                    fill_ts=utc_now() + timedelta(seconds=5),
                )
            ],
            instruments=[],
            account_mode="cash",
        )

        report = await runtime.reconciliation_service.validate_now(reason="external_activity_probe")

        self.assertTrue(report.exchange_comparison_enabled)
        self.assertEqual(report.severity, "REVIEW_REQUIRED")
        self.assertTrue(report.review_required)
        self.assertIn("external_manual_activity_detected", report.mismatch_categories)

    async def test_runtime_enters_review_required_state_when_imported_baseline_has_open_orders(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        FakeBaselineAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT",
                    client_order_id="extorder1",
                    exchange_order_id="ord_ext_1",
                    side="buy",
                    order_type="limit",
                    status="LIVE",
                    quantity=0.001,
                    filled_quantity=0.0,
                    price=65000.0,
                    created_ts=utc_now(),
                    updated_ts=utc_now(),
                )
            ],
            fills=[],
            instruments=[],
            account_mode="cash",
        )

        with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
            runtime = await build_runtime(settings)

        self.assertEqual(runtime.recovery_status.status, "baseline_import_requires_review")
        self.assertTrue(runtime.recovery_status.baseline_requires_operator_review)
        self.assertFalse(runtime.recovery_status.safe_startup)
        self.assertTrue(runtime.recovery_status.halted)
        self.assertEqual(runtime.recovery_status.recovery_action, "halted_imported_baseline_requires_review")

    async def test_operator_rebaseline_rebuilds_trusted_state_and_resume_succeeds(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        startup_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT",
                    client_order_id="extorder1",
                    exchange_order_id="ord_ext_1",
                    side="buy",
                    order_type="limit",
                    status="LIVE",
                    quantity=0.001,
                    filled_quantity=0.0,
                    price=65000.0,
                    created_ts=utc_now(),
                    updated_ts=utc_now(),
                )
            ],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        FakeBaselineAccountService.SNAPSHOT = startup_snapshot

        with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
            runtime = await build_runtime(settings)

        query = OperatorQueryService(runtime)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        self.assertEqual(query.recovery_view()["recovery_state"], "review_required")

        clean_snapshot = startup_snapshot.model_copy(
            update={
                "fetched_at": utc_now() + timedelta(seconds=5),
                "open_orders": [],
                "balances": [
                    ExchangeBalance(currency="USDT", total=950.0, available=950.0, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.001, available=0.001, frozen=0.0),
                ],
                "positions": [
                    ExchangePosition(
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        quantity=0.001,
                        average_entry_price=68000.0,
                    )
                ],
                "fills": [
                    ExchangeFill(
                        fill_id="manual_fill_1",
                        exchange_order_id="ord_manual_1",
                        client_order_id=None,
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        side="buy",
                        fill_qty=0.001,
                        fill_price=68000.0,
                        fee_amount=0.0,
                        fill_ts=utc_now(),
                    )
                ],
            }
        )
        runtime.account_service._snapshot = clean_snapshot

        previous_baseline_ref = runtime.recovery_status.baseline_event_ref
        rebaseline = await query.rebaseline(reason="accept_current_exchange_state", actor_role="admin")
        self.assertEqual(rebaseline["status"], "review_required")
        self.assertEqual(rebaseline["rebaseline_status"], "review_required")
        self.assertTrue(rebaseline["halted"])
        self.assertIsNotNone(rebaseline["auto_resume"])
        self.assertEqual(rebaseline["auto_resume"]["status"], "resume_blocked")
        self.assertFalse(runtime.recovery_status.resume_eligible)
        self.assertEqual(runtime.recovery_status.last_rebaseline_event_ref, rebaseline["baseline_event_ref"])
        latest_baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(latest_baseline)
        self.assertEqual(latest_baseline.payload["baseline_kind"], "operator_rebaseline")
        self.assertEqual(latest_baseline.payload["previous_baseline_ref"], previous_baseline_ref)
        latest_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(latest_snapshot)
        self.assertEqual(latest_snapshot.balances["BTC"], Decimal("0.001000000000"))
        resumed = await query.resume(reason="resume_after_rebaseline", actor_role="admin")
        self.assertEqual(resumed["status"], "resume_blocked")
        self.assertTrue(resumed["halted"])
        self.assertFalse(resumed["runnable"])
        self.assertEqual(query.recovery_view()["recovery_state"], "review_required")

        operator_actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        self.assertTrue(any(item["action"] == "rebaseline" for item in operator_actions))
        self.assertTrue(any(item["action"] == "resume" for item in operator_actions))

    async def test_operator_rebaseline_refresh_failure_does_not_force_pending_halt_state(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        FakeBaselineAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )

        with patch("aats.bootstrap.config.OKXAccountService", FakeBaselineAccountService):
            runtime = await build_runtime(settings)

        query = OperatorQueryService(runtime)
        recovery_before = query.recovery_view()
        halted_before = runtime.kill_switch.halted
        runtime.account_service._snapshot = None

        with self.assertRaisesRegex(ValueError, "rebaseline_requires_account_snapshot"):
            await query.rebaseline(reason="accept_current_exchange_state", actor_role="admin")

        self.assertEqual(query.recovery_view()["recovery_state"], recovery_before["recovery_state"])
        self.assertEqual(runtime.kill_switch.halted, halted_before)

    async def test_derivatives_recovery_view_surfaces_only_reduce_state_without_forcing_halt(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_integration_only_reduce",
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
                severity="SOFT_MISMATCH",
                recovery_classification="derivatives_only_reduce",
                only_reduce_required=True,
                only_reduce_reasons=["derivatives_exchange_position_without_local_execution_chain"],
                unknown_state_details=[
                    {
                        "kind": "exchange_position_without_local_execution_chain",
                        "symbol": "BTC-USDT-SWAP",
                        "stored_qty": "0",
                        "exchange_qty": "0.02",
                    }
                ],
                recommended_operator_action="go_close_position_on_exchange",
            )
        )

        query = OperatorQueryService(runtime)
        recovery = query.recovery_view()

        self.assertEqual(recovery["recovery_state"], "only_reduce")
        self.assertTrue(recovery["safe_to_trade"])
        self.assertTrue(recovery["resume_eligible"])
        self.assertTrue(recovery["only_reduce_required"])
        self.assertIn("derivatives_exchange_position_without_local_execution_chain", recovery["only_reduce_reasons"])
        self.assertEqual(recovery["latest_reconciliation"]["recovery_classification"], "derivatives_only_reduce")
        self.assertEqual(
            recovery["latest_reconciliation"]["recommended_operator_action"],
            "go_close_position_on_exchange",
        )

    async def test_restarted_runtime_tracks_structured_bundle_open_orders_without_halt(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            try:
                now = utc_now()
                runtime.execution_repo.save_order_state(
                    OrderState(
                        decision_id="decision_restart_bundle_1",
                        intent_id="intent_restart_bundle_grid",
                        symbol=settings.default_symbol,
                        client_order_id="cl_restart_bundle_grid",
                        venue="OKX",
                        exchange_order_id="ord_restart_bundle_grid",
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
                        strategy_sleeve_id="sleeve_restart_grid",
                        allocation_id="alloc_restart_bundle",
                        strategy_bundle_id="bundle_restart_inventory",
                        strategy_leg_role="inventory",
                        submission_payload={},
                    )
                )
                runtime.execution_repo.save_order_state(
                    OrderState(
                        decision_id="decision_restart_bundle_1",
                        intent_id="intent_restart_bundle_dca",
                        symbol=settings.default_symbol,
                        client_order_id="cl_restart_bundle_dca",
                        venue="OKX",
                        exchange_order_id="ord_restart_bundle_dca",
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
                        strategy_family="dca",
                        strategy_sleeve_id="sleeve_restart_dca",
                        allocation_id="alloc_restart_bundle",
                        strategy_bundle_id="bundle_restart_inventory",
                        strategy_leg_role="accumulation",
                        submission_payload={},
                    )
                )
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertFalse(recovered_runtime.recovery_status.halted)
                self.assertEqual(recovered_runtime.recovery_status.recovery_state, "bundle_recovery")
                self.assertTrue(recovered_runtime.recovery_status.bundle_recovery_required)
                self.assertEqual(recovered_runtime.recovery_status.bundle_recovery_count, 1)
                self.assertEqual(recovered_runtime.recovery_status.open_order_count, 2)
                self.assertFalse(recovered_runtime.recovery_status.safe_to_trade)
                self.assertIn(
                    "strategy_bundle_recovery_in_progress",
                    recovered_runtime.recovery_status.only_reduce_reasons,
                )

                query = OperatorQueryService(recovered_runtime)
                recovery = query.recovery_view()
                self.assertEqual(recovery["recovery_state"], "bundle_recovery")
                self.assertEqual(len(recovery["bundle_summaries"]), 1)
                self.assertEqual(
                    recovery["bundle_summaries"][0]["participating_families"],
                    ["spot_grid", "dca"],
                )
                self.assertEqual(recovery["bundle_summaries"][0]["open_order_count"], 2)
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_resume_rechecks_blockers_and_stays_halted_when_market_is_stale(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "storage_mode": "memory",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
            }
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        latest_snapshot = runtime.market_gateway.latest_snapshot(settings.default_symbol)
        runtime.market_gateway._latest_snapshots[settings.default_symbol] = latest_snapshot.model_copy(
            update={"snapshot_ts": utc_now() - timedelta(seconds=120)}
        )
        query = OperatorQueryService(runtime)
        query.halt(reason="prepare_resume_block", actor_role="admin")

        resumed = await query.resume(reason="resume_with_stale_market", actor_role="admin")

        self.assertEqual(resumed["status"], "resume_blocked")
        self.assertTrue(resumed["halted"])
        self.assertFalse(resumed["runnable"])
        self.assertTrue(any(item["blocker"] == "market_data_stale" for item in resumed["blockers"]))

    @staticmethod
    def _postgres_settings(database_url: str) -> AATSSettings:
        return AATSSettings.model_validate(
            {
                "storage_mode": "postgres",
                "database_url": database_url,
                "database_auto_create_schema": True,
                "local_publish_iterations": 4,
                "local_publish_interval_seconds": 0.0,
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
            }
        )

    @staticmethod
    def _delete_portfolio_snapshots(runtime) -> None:
        if runtime.database_runtime is None:
            return
        with runtime.database_runtime.session_factory() as session:
            session.query(PortfolioSnapshotModel).delete()
            session.commit()

    @staticmethod
    def _delete_reconciliation_reports(runtime) -> None:
        if runtime.database_runtime is None:
            return
        with runtime.database_runtime.session_factory() as session:
            session.query(ReconciliationReportModel).delete()
            session.commit()

    @staticmethod
    def _delete_event_topic(runtime, topic: str) -> None:
        if runtime.database_runtime is None:
            return
        with runtime.database_runtime.session_factory() as session:
            session.query(EventEnvelopeModel).filter(EventEnvelopeModel.topic == topic).delete()
            session.commit()


if __name__ == "__main__":
    unittest.main()
