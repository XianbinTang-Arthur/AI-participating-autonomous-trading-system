from __future__ import annotations

import unittest
from decimal import Decimal
from datetime import datetime, timezone

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.execution import OrderState
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.decision_engine.context_builder import DecisionContextBuilder
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository


class _FakeHealthService:
    def snapshot(self):  # pragma: no cover - not used by these tests
        raise AssertionError("health snapshot should not be requested in this unit test")


class TestDecisionContextBuilder(unittest.TestCase):
    def test_position_qty_falls_back_to_base_balance_for_spot_snapshots(self) -> None:
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 1_000.0, "BTC": 0.0015},
            positions=[],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=1_000.0,
            gross_exposure=0.0,
            net_exposure=0.0,
            risk_budget_usage={},
        )

        quantity = DecisionContextBuilder._position_qty(snapshot, "BTC-USDT", "spot")

        self.assertEqual(quantity, Decimal("0.0015"))

    def test_position_qty_does_not_treat_balance_as_derivatives_position(self) -> None:
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 75_000.0, "BTC": 0.0015},
            positions=[],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=75_000.0,
            gross_exposure=0.0,
            net_exposure=0.0,
            risk_budget_usage={},
            product_type="derivatives",
            margin_mode="cross",
        )

        quantity = DecisionContextBuilder._position_qty(snapshot, "BTC-USDT-SWAP", "derivatives")

        self.assertEqual(quantity, Decimal("0"))

    def test_build_uses_repo_snapshot_when_portfolio_event_is_missing(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "trading_product_type": "spot",
                "margin_mode": "cash",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 1_000.0, "BTC": 0.0015},
            positions=[],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=1_000.0,
            gross_exposure=0.0,
            net_exposure=0.0,
            risk_budget_usage={},
        )
        portfolio_repo.save_snapshot(snapshot)
        event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key="BTC-USDT",
                payload_model=MarketSnapshot(
                    symbol="BTC-USDT",
                    exchange="OKX",
                    snapshot_ts=datetime.now(timezone.utc),
                    best_bid=70_000.0,
                    best_ask=70_001.0,
                    last_price=70_000.5,
                    bid_size=1.0,
                    ask_size=1.0,
                    volume_24h=10_000_000.0,
                    kline_15m={"open": 69_900.0, "high": 70_100.0, "low": 69_800.0, "close": 70_000.5},
                    kline_1h={"open": 69_800.0, "high": 70_200.0, "low": 69_700.0, "close": 70_000.5},
                ),
                source_component="test",
            )
        )
        event_store.append(
            build_envelope(
                topic=topics.FEATURE_SNAPSHOTS,
                key="BTC-USDT",
                payload_model=FeatureSnapshot(
                    symbol="BTC-USDT",
                    snapshot_ts=datetime.now(timezone.utc),
                    market_snapshot_ref="evt_market",
                    trend_strength=0.7,
                    volatility_state="medium",
                    volatility_value=0.2,
                    momentum_score=12.0,
                    liquidity_score=0.8,
                    regime_indicator="trend",
                    regime_confidence=0.75,
                    multi_timeframe_alignment=0.6,
                    composite_alpha_score=0.4,
                    suggested_position_scale=0.5,
                    volatility_target_scale=1.0,
                    feature_version="test",
                ),
                source_component="test",
            )
        )

        builder = DecisionContextBuilder(
            settings=settings,
            event_store=event_store,
            portfolio_repo=portfolio_repo,
            execution_repo=InMemoryExecutionRepository(),
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
        )

        context = builder.build(
            "BTC-USDT",
            "15m",
            decision_id="decision_test",
            health_snapshot_ref="evt_health",
        )

        self.assertTrue(context.portfolio_snapshot_ref.startswith("portfolio_snapshot:"))
        self.assertEqual(context.current_position_qty, Decimal("0.0015"))
        self.assertEqual(context.current_open_orders, [])

    def test_build_includes_scoped_open_orders_in_context(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "trading_product_type": "spot",
                "margin_mode": "cash",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        execution_repo = InMemoryExecutionRepository()
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=datetime.now(timezone.utc),
                balances={"USDT": 1_000.0, "BTC": 0.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=1_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            )
        )
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_open_order",
                intent_id="intent_open_order",
                symbol="BTC-USDT",
                client_order_id="order_open_order",
                status="SUBMITTED",
                requested_qty=Decimal("0.001"),
                remaining_qty=Decimal("0.001"),
            )
        )
        for topic, payload in (
            (
                topics.MARKET_SNAPSHOTS,
                MarketSnapshot(
                    symbol="BTC-USDT",
                    exchange="OKX",
                    snapshot_ts=datetime.now(timezone.utc),
                    best_bid=70_000.0,
                    best_ask=70_001.0,
                    last_price=70_000.5,
                    bid_size=1.0,
                    ask_size=1.0,
                    volume_24h=10_000_000.0,
                    kline_15m={"open": 69_900.0, "high": 70_100.0, "low": 69_800.0, "close": 70_000.5},
                    kline_1h={"open": 69_800.0, "high": 70_200.0, "low": 69_700.0, "close": 70_000.5},
                ),
            ),
            (
                topics.FEATURE_SNAPSHOTS,
                FeatureSnapshot(
                    symbol="BTC-USDT",
                    snapshot_ts=datetime.now(timezone.utc),
                    market_snapshot_ref="evt_market",
                    trend_strength=0.7,
                    volatility_state="medium",
                    volatility_value=0.2,
                    momentum_score=12.0,
                    liquidity_score=0.8,
                    regime_indicator="trend",
                    regime_confidence=0.75,
                    multi_timeframe_alignment=0.6,
                    composite_alpha_score=0.4,
                    suggested_position_scale=0.5,
                    volatility_target_scale=1.0,
                    feature_version="test",
                ),
            ),
        ):
            event_store.append(
                build_envelope(
                    topic=topic,
                    key="BTC-USDT",
                    payload_model=payload,
                    source_component="test",
                )
            )

        builder = DecisionContextBuilder(
            settings=settings,
            event_store=event_store,
            portfolio_repo=portfolio_repo,
            execution_repo=execution_repo,
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
        )

        context = builder.build("BTC-USDT", "15m", decision_id="decision_test", health_snapshot_ref="evt_health")

        self.assertEqual(context.current_open_orders, ["order_open_order"])


if __name__ == "__main__":
    unittest.main()
