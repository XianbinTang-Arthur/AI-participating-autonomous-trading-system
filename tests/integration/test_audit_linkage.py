from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot


class TestAuditLinkage(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_decisions_do_not_cross_link_snapshots_or_reconciliation(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT", "ETH-USDT"),
            }
        )
        runtime = await build_runtime(settings)

        await runtime.market_gateway.run_local_publisher(
            symbol="BTC-USDT",
            iterations=6,
            interval_seconds=0.0,
        )
        await runtime.market_gateway.run_local_publisher(
            symbol="ETH-USDT",
            iterations=6,
            interval_seconds=0.0,
        )

        event_store = runtime.event_store
        records_with_execution = [record for record in runtime.audit_repo.all() if record.fill_event_refs]
        self.assertGreaterEqual(len(records_with_execution), 2)
        self.assertEqual(
            len({record.portfolio_delta_ref for record in records_with_execution if record.portfolio_delta_ref}),
            len(records_with_execution),
        )

        for record in records_with_execution:
            self.assertIsNotNone(record.portfolio_delta_ref)
            self.assertTrue(record.portfolio_delta_refs)
            self.assertIn(record.portfolio_delta_ref, record.portfolio_delta_refs)
            snapshot_event = event_store.get(record.portfolio_delta_ref)
            self.assertIsNotNone(snapshot_event)
            self.assertEqual(snapshot_event.payload.get("decision_id"), record.decision_id)

            source_fill_ids = {
                event_store.get(ref).payload.get("fill_id")
                for ref in record.fill_event_refs
                if event_store.get(ref) is not None
            }
            self.assertEqual(snapshot_event.payload.get("source_fill_id") in source_fill_ids, True)

            self.assertTrue(record.reconciliation_refs)
            for reconciliation_ref in record.reconciliation_refs:
                reconciliation_event = event_store.get(reconciliation_ref)
                self.assertIsNotNone(reconciliation_event)
                self.assertEqual(reconciliation_event.payload.get("decision_id"), record.decision_id)
                self.assertIn(
                    reconciliation_event.payload.get("portfolio_snapshot_ref"),
                    record.portfolio_delta_refs,
                )

    async def test_smart_arbitrage_dual_fill_keeps_all_snapshot_refs_available_for_reconciliation(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 0.0,
                "smart_arbitrage_estimated_cost_bps": 0.0,
                "smart_arbitrage_cost_model_enabled": False,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        runtime = await build_runtime(settings)

        await runtime.market_gateway.run_local_publisher(symbol="BTC-USDT", iterations=1, interval_seconds=0.0)
        await runtime.market_gateway.run_local_publisher(symbol=settings.default_symbol, iterations=1, interval_seconds=0.0)

        # Inject controlled prices so the explicit run_cycle sees
        # positive basis (hedge > spot) large enough to produce an opening signal.
        _now = utc_now()
        runtime.market_gateway._latest_snapshots["BTC-USDT"] = MarketSnapshot(
            symbol="BTC-USDT", exchange="OKX", snapshot_ts=_now,
            best_bid=67_000.0, best_ask=67_010.0, last_price=67_005.0,
            bid_size=1.0, ask_size=1.0, volume_24h=100_000_000.0,
            kline_15m={"open": 67_000.0, "high": 67_100.0, "low": 66_900.0, "close": 67_005.0},
            kline_1h={"open": 66_800.0, "high": 67_200.0, "low": 66_700.0, "close": 67_005.0},
        )
        runtime.market_gateway._latest_snapshots["BTC-USDT-SWAP"] = MarketSnapshot(
            symbol="BTC-USDT-SWAP", exchange="OKX", snapshot_ts=_now,
            best_bid=67_100.0, best_ask=67_110.0, last_price=67_105.0,
            bid_size=1.0, ask_size=1.0, volume_24h=100_000_000.0,
            kline_15m={"open": 67_050.0, "high": 67_200.0, "low": 66_950.0, "close": 67_105.0},
            kline_1h={"open": 66_850.0, "high": 67_300.0, "low": 66_750.0, "close": 67_105.0},
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        record = runtime.audit_repo.get(target.decision_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertGreaterEqual(len(record.fill_event_refs), 2)
        self.assertGreaterEqual(len(record.portfolio_delta_refs), 2)
        self.assertTrue(record.reconciliation_refs)
        self.assertIn(record.portfolio_delta_ref, record.portfolio_delta_refs)

        for reconciliation_ref in record.reconciliation_refs:
            reconciliation_event = runtime.event_store.get(reconciliation_ref)
            self.assertIsNotNone(reconciliation_event)
            if reconciliation_event is not None:
                self.assertIn(
                    reconciliation_event.payload.get("portfolio_snapshot_ref"),
                    record.portfolio_delta_refs,
                )


if __name__ == "__main__":
    unittest.main()
