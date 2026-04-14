from __future__ import annotations

import unittest
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.execution import FillEvent
from aats.schemas.exchange import ExchangeAccountRiskSnapshot, ExchangeAccountSnapshot
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot, Position
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
    def test_available_trading_equity_prefers_exchange_available_equity(self) -> None:
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            risk_snapshot=ExchangeAccountRiskSnapshot(
                available_equity=Decimal("390"),
                total_equity=Decimal("420"),
            ),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("300")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("300"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
        )

        resolved = DecisionContextBuilder._available_trading_equity(
            account_snapshot=account_snapshot,
            portfolio_snapshot=portfolio_snapshot,
        )

        self.assertEqual(resolved, Decimal("390"))

    def test_available_trading_equity_does_not_fallback_to_total_equity_when_available_missing(self) -> None:
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("420"),
                total_equity=Decimal("450"),
            ),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("390")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("450"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
            cash_equity=Decimal("390"),
            collateral_value=Decimal("420"),
        )

        resolved = DecisionContextBuilder._available_trading_equity(
            account_snapshot=account_snapshot,
            portfolio_snapshot=portfolio_snapshot,
        )

        self.assertEqual(resolved, Decimal("0"))

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

        state = DecisionContextBuilder._position_state(snapshot, "BTC-USDT", "spot")

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.net_position_qty, Decimal("0.0015"))

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

        state = DecisionContextBuilder._position_state(snapshot, "BTC-USDT-SWAP", "derivatives")

        self.assertIsNone(state)

    def test_position_qty_aggregates_derivatives_legs_for_same_symbol(self) -> None:
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 75_000.0},
            positions=[
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:long",
                    position_qty=Decimal("0.02"),
                    position_notional=Decimal("1400"),
                    avg_entry_price=Decimal("70000"),
                    unrealized_pnl=Decimal("0"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                ),
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:short",
                    position_qty=Decimal("-0.01"),
                    position_notional=Decimal("-700"),
                    avg_entry_price=Decimal("70000"),
                    unrealized_pnl=Decimal("0"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="short",
                ),
            ],
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

        state = DecisionContextBuilder._position_state(snapshot, "BTC-USDT-SWAP", "derivatives")

        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(state.dual_legged)
        self.assertEqual(state.net_position_qty, Decimal("0.01"))
        self.assertEqual(state.gross_position_qty, Decimal("0.03"))
        self.assertEqual(state.long_position_qty, Decimal("0.02"))
        self.assertEqual(state.short_position_qty, Decimal("0.01"))
        self.assertEqual(state.net_position_notional, Decimal("700"))
        self.assertEqual(state.gross_position_notional, Decimal("2100"))
        self.assertEqual(len(state.legs), 2)

    def test_build_keeps_conservative_leg_anchor_when_fill_history_is_incomplete(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        execution_repo = InMemoryExecutionRepository()
        snapshot_ts = datetime.now(timezone.utc)
        fill_ts = snapshot_ts - timedelta(minutes=15)
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=snapshot_ts,
                balances={"USDT": 75_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=Decimal("0.02"),
                        position_notional=Decimal("1400"),
                        avg_entry_price=Decimal("70000"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=75_000.0,
                gross_exposure=1400.0,
                net_exposure=1400.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_incomplete_leg_history",
                decision_id="decision_incomplete_leg_history",
                intent_id="intent_incomplete_leg_history",
                leg_intent_id="leg_incomplete_leg_history",
                client_order_id="cl_incomplete_leg_history",
                exchange_order_id="ord_incomplete_leg_history",
                symbol="BTC-USDT-SWAP",
                venue="OKX",
                side="buy",
                fill_qty=Decimal("0.01"),
                fill_price=Decimal("70000"),
                fee_amount=Decimal("0.1"),
                fee_currency="USDT",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="long",
                product_type="derivatives",
                target_leverage=2.0,
                margin_mode="cross",
                exposure_side="long",
                execution_action="enter",
                leg_action="open",
                position_intent="open_long",
                liquidity_role="taker",
                exchange_timestamp=fill_ts,
                ingestion_timestamp=fill_ts,
            )
        )
        event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key="BTC-USDT-SWAP",
                payload_model=MarketSnapshot(
                    symbol="BTC-USDT-SWAP",
                    exchange="OKX",
                    snapshot_ts=snapshot_ts,
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
                key="BTC-USDT-SWAP",
                payload_model=FeatureSnapshot(
                    symbol="BTC-USDT-SWAP",
                    snapshot_ts=snapshot_ts,
                    market_snapshot_ref="evt_market_derivatives_incomplete_leg",
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
            execution_repo=execution_repo,
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
        )

        context = builder.build(
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_derivatives_incomplete_leg",
            health_snapshot_ref="evt_health_derivatives_incomplete_leg",
        )

        self.assertEqual(context.current_long_position_qty, Decimal("0.02"))
        self.assertEqual(context.current_long_leg_opened_at, fill_ts)
        self.assertEqual(context.latest_long_leg_fill_timestamp, fill_ts)

    def test_build_uses_continuous_open_snapshot_anchor_when_fill_history_is_missing(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        execution_repo = InMemoryExecutionRepository()
        first_snapshot_ts = datetime.now(timezone.utc) - timedelta(minutes=20)
        latest_snapshot_ts = first_snapshot_ts + timedelta(minutes=10)
        for snapshot_ts in (first_snapshot_ts, latest_snapshot_ts):
            portfolio_repo.save_snapshot(
                PortfolioSnapshot(
                    snapshot_ts=snapshot_ts,
                    balances={"USDT": 75_000.0},
                    positions=[
                        Position(
                            symbol="BTC-USDT-SWAP",
                            position_key="BTC-USDT-SWAP:long",
                            position_qty=Decimal("0.02"),
                            position_notional=Decimal("1400"),
                            avg_entry_price=Decimal("70000"),
                            unrealized_pnl=Decimal("0"),
                            product_type="derivatives",
                            margin_mode="cross",
                            position_mode="long_short_mode",
                            pos_side="long",
                        )
                    ],
                    cost_basis={},
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    total_equity=75_000.0,
                    gross_exposure=1400.0,
                    net_exposure=1400.0,
                    risk_budget_usage={},
                    product_type="derivatives",
                    margin_mode="cross",
                )
            )
        event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key="BTC-USDT-SWAP",
                payload_model=MarketSnapshot(
                    symbol="BTC-USDT-SWAP",
                    exchange="OKX",
                    snapshot_ts=latest_snapshot_ts,
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
                key="BTC-USDT-SWAP",
                payload_model=FeatureSnapshot(
                    symbol="BTC-USDT-SWAP",
                    snapshot_ts=latest_snapshot_ts,
                    market_snapshot_ref="evt_market_derivatives_snapshot_anchor",
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
            execution_repo=execution_repo,
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
        )

        context = builder.build(
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_derivatives_snapshot_anchor",
            health_snapshot_ref="evt_health_derivatives_snapshot_anchor",
        )

        self.assertEqual(context.current_long_position_qty, Decimal("0.02"))
        self.assertEqual(context.current_long_leg_opened_at, first_snapshot_ts)
        self.assertEqual(context.latest_long_leg_fill_timestamp, first_snapshot_ts)

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

    def test_build_populates_dual_leg_position_state_for_derivatives_runtime(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=datetime.now(timezone.utc),
                balances={"USDT": 75_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=Decimal("0.02"),
                        position_notional=Decimal("1400"),
                        avg_entry_price=Decimal("70000"),
                        unrealized_pnl=Decimal("15"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    ),
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:short",
                        position_qty=Decimal("-0.01"),
                        position_notional=Decimal("-700"),
                        avg_entry_price=Decimal("70500"),
                        unrealized_pnl=Decimal("-3"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="short",
                    ),
                ],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=12.0,
                total_equity=75_012.0,
                gross_exposure=2100.0,
                net_exposure=700.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        for topic, payload in (
            (
                topics.MARKET_SNAPSHOTS,
                MarketSnapshot(
                    symbol="BTC-USDT-SWAP",
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
                    symbol="BTC-USDT-SWAP",
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
                    key="BTC-USDT-SWAP",
                    payload_model=payload,
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
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_dual_leg",
            health_snapshot_ref="evt_health",
        )

        self.assertEqual(context.current_position_qty, Decimal("0.01"))
        self.assertEqual(context.current_net_position_qty, Decimal("0.01"))
        self.assertEqual(context.current_gross_position_qty, Decimal("0.03"))
        self.assertEqual(context.current_long_position_qty, Decimal("0.02"))
        self.assertEqual(context.current_short_position_qty, Decimal("0.01"))
        self.assertEqual(context.current_net_position_notional, Decimal("700"))
        self.assertEqual(context.current_gross_position_notional, Decimal("2100"))
        self.assertEqual(context.current_exposure_side, "long")
        self.assertEqual(len(context.current_position_legs), 2)
        self.assertIsNotNone(context.current_position_state)
        assert context.current_position_state is not None
        self.assertTrue(context.current_position_state.dual_legged)

    def test_build_populates_dual_leg_lifecycle_timestamps_from_fills(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        execution_repo = InMemoryExecutionRepository()
        now = datetime.now(timezone.utc)
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 75_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=Decimal("0.02"),
                        position_notional=Decimal("1400"),
                        avg_entry_price=Decimal("70000"),
                        unrealized_pnl=Decimal("15"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    ),
                ],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=15.0,
                total_equity=75_015.0,
                gross_exposure=1400.0,
                net_exposure=1400.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_short_open",
                decision_id="decision_leg_lifecycle",
                intent_id="intent_short_open",
                client_order_id="order_short_open",
                exchange_order_id="exchange_short_open",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="sell",
                fill_qty=Decimal("0.01"),
                fill_price=Decimal("70100"),
                fee_amount=Decimal("0.1"),
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                leg_action="open",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=7),
                ingestion_timestamp=now - timedelta(minutes=7),
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_short_close",
                decision_id="decision_leg_lifecycle",
                intent_id="intent_short_close",
                client_order_id="order_short_close",
                exchange_order_id="exchange_short_close",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="buy",
                fill_qty=Decimal("0.01"),
                fill_price=Decimal("70080"),
                fee_amount=Decimal("0.1"),
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                leg_action="close",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=5),
                ingestion_timestamp=now - timedelta(minutes=5),
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_long_open",
                decision_id="decision_leg_lifecycle",
                intent_id="intent_long_open",
                client_order_id="order_long_open",
                exchange_order_id="exchange_long_open",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="buy",
                fill_qty=Decimal("0.02"),
                fill_price=Decimal("70000"),
                fee_amount=Decimal("0.1"),
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="long",
                leg_action="open",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=3),
                ingestion_timestamp=now - timedelta(minutes=3),
            )
        )
        for topic, payload in (
            (
                topics.MARKET_SNAPSHOTS,
                MarketSnapshot(
                    symbol="BTC-USDT-SWAP",
                    exchange="OKX",
                    snapshot_ts=now,
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
                    symbol="BTC-USDT-SWAP",
                    snapshot_ts=now,
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
                    key="BTC-USDT-SWAP",
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

        context = builder.build(
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_leg_lifecycle",
            health_snapshot_ref="evt_health",
        )

        self.assertEqual(context.current_long_position_qty, Decimal("0.02"))
        self.assertEqual(context.current_short_position_qty, Decimal("0"))
        self.assertEqual(context.current_long_leg_opened_at, now - timedelta(minutes=3))
        self.assertIsNone(context.current_short_leg_opened_at)
        self.assertEqual(context.last_short_leg_closed_at, now - timedelta(minutes=5))
        self.assertEqual(context.latest_short_leg_fill_timestamp, now - timedelta(minutes=5))
        self.assertEqual(context.latest_long_leg_fill_timestamp, now - timedelta(minutes=3))

    def test_build_separates_leg_strategy_health_for_long_and_short_books(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "strategy_health_lookback_trades": 8,
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        execution_repo = InMemoryExecutionRepository()
        now = datetime.now(timezone.utc)
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now - timedelta(minutes=12),
                balances={"USDT": 75_000.0},
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
        )
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now - timedelta(minutes=6),
                balances={"USDT": 74_995.0},
                positions=[],
                cost_basis={},
                realized_pnl=-5.0,
                unrealized_pnl=0.0,
                total_equity=74_995.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
                source_fill_id="fill_long_close_health",
            )
        )
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
                balances={"USDT": 74_995.0},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:short",
                        position_qty=Decimal("-0.01"),
                        position_notional=Decimal("-700"),
                        avg_entry_price=Decimal("70020"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="short",
                    )
                ],
                cost_basis={},
                realized_pnl=-5.0,
                unrealized_pnl=0.0,
                total_equity=74_995.0,
                gross_exposure=700.0,
                net_exposure=-700.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
                source_fill_id="fill_short_open_health",
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_long_open_health",
                decision_id="decision_leg_health",
                intent_id="intent_long_open_health",
                client_order_id="order_long_open_health",
                exchange_order_id="exchange_long_open_health",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="buy",
                fill_qty=Decimal("0.01"),
                fill_price=Decimal("70000"),
                fee_amount=Decimal("0.1"),
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="long",
                leg_action="open",
                position_intent="open_long",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=10),
                ingestion_timestamp=now - timedelta(minutes=10),
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_long_close_health",
                decision_id="decision_leg_health",
                intent_id="intent_long_close_health",
                client_order_id="order_long_close_health",
                exchange_order_id="exchange_long_close_health",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="sell",
                fill_qty=Decimal("0.01"),
                fill_price=Decimal("69950"),
                fee_amount=Decimal("0.1"),
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="long",
                leg_action="close",
                position_intent="close_long",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=6),
                ingestion_timestamp=now - timedelta(minutes=6),
            )
        )
        execution_repo.save_fill(
            FillEvent(
                fill_id="fill_short_open_health",
                decision_id="decision_leg_health",
                intent_id="intent_short_open_health",
                client_order_id="order_short_open_health",
                exchange_order_id="exchange_short_open_health",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="sell",
                fill_qty=Decimal("0.01"),
                fill_price=Decimal("70020"),
                fee_amount=Decimal("0.1"),
                product_type="derivatives",
                margin_mode="cross",
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                leg_action="open",
                position_intent="open_short",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=2),
                ingestion_timestamp=now - timedelta(minutes=2),
            )
        )
        for topic, payload in (
            (
                topics.MARKET_SNAPSHOTS,
                MarketSnapshot(
                    symbol="BTC-USDT-SWAP",
                    exchange="OKX",
                    snapshot_ts=now,
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
                    symbol="BTC-USDT-SWAP",
                    snapshot_ts=now,
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
                    key="BTC-USDT-SWAP",
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

        context = builder.build(
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_leg_health",
            health_snapshot_ref="evt_health",
        )

        self.assertEqual(context.current_short_position_qty, Decimal("0.01"))
        self.assertEqual(context.leg_strategy_health["long"]["recent_closed_trade_count"], 1)
        self.assertEqual(context.leg_strategy_health["short"]["recent_closed_trade_count"], 0)
        self.assertEqual(context.leg_strategy_health["long"]["recent_net_realized_pnl"], -5.0)
        self.assertEqual(context.leg_strategy_health["short"]["recent_net_realized_pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
