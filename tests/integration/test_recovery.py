from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.exchange import (
    ExchangeAccountSnapshot,
    ExchangeBalance,
    ExchangeFill,
    ExchangeOpenOrder,
    ExchangePosition,
)
from aats.services.operator.query_service import OperatorQueryService
from aats.storage.sqlalchemy_models import PortfolioSnapshotModel, ReconciliationReportModel


class FakeBaselineAccountService:
    SNAPSHOT: ExchangeAccountSnapshot | None = None

    def __init__(self, *, settings, client) -> None:
        self.settings = settings
        self.client = client
        self._snapshot = self.SNAPSHOT

    async def refresh(self, *, force: bool = False):
        return self._snapshot

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
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
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
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                self._delete_portfolio_snapshots(runtime)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertEqual(recovered_runtime.recovery_status.status, "recovered")
                self.assertTrue(recovered_runtime.recovery_status.rebuilt_snapshot_saved)
                self.assertTrue(recovered_runtime.recovery_status.recovered_snapshot_available)
                self.assertIsNotNone(recovered_runtime.portfolio_repo.latest())
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_runtime_enters_safe_halt_when_execution_state_has_no_reconciliation_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
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
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    async def test_runtime_imports_clean_account_baseline_on_startup(self) -> None:
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
        self.assertEqual(latest_snapshot.balances["BTC"], 0.01)
        self.assertEqual(latest_snapshot.positions[0].symbol, "BTC-USDT")
        self.assertAlmostEqual(latest_snapshot.positions[0].position_qty, 0.01)

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
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
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
        self.assertEqual(rebaseline["status"], "rebaseline_completed")
        self.assertTrue(rebaseline["halted"])
        self.assertEqual(runtime.recovery_status.last_rebaseline_event_ref, rebaseline["baseline_event_ref"])
        latest_baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(latest_baseline)
        self.assertEqual(latest_baseline.payload["baseline_kind"], "operator_rebaseline")
        self.assertEqual(latest_baseline.payload["previous_baseline_ref"], previous_baseline_ref)
        latest_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(latest_snapshot)
        self.assertEqual(latest_snapshot.balances["BTC"], 0.001)

        resumed = await query.resume(reason="resume_after_rebaseline", actor_role="admin")
        self.assertEqual(resumed["status"], "resumed")
        self.assertFalse(resumed["halted"])
        self.assertTrue(resumed["runnable"])
        self.assertEqual(query.recovery_view()["recovery_state"], "normal_operation")

        operator_actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        self.assertTrue(any(item["action"] == "rebaseline" for item in operator_actions))
        self.assertTrue(any(item["action"] == "resume" for item in operator_actions))

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
    def _sqlite_settings(temp_dir: Path) -> AATSSettings:
        database_path = (temp_dir / "aats_recovery.db").resolve().as_posix()
        return AATSSettings.model_validate(
            {
                "storage_mode": "postgres",
                "database_url": f"sqlite+pysqlite:///{database_path}",
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


if __name__ == "__main__":
    unittest.main()
