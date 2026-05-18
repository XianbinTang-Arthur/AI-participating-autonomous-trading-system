from __future__ import annotations

import unittest
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.execution import FillEvent
from aats.schemas.exchange import ExchangeAccountRiskSnapshot, ExchangeAccountSnapshot, ExchangeBalance
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


class _StatusAccountService:
    def __init__(self, *, snapshot: ExchangeAccountSnapshot, status: dict) -> None:
        self._snapshot = snapshot
        self._status = status

    def latest_snapshot(self) -> ExchangeAccountSnapshot:
        return self._snapshot

    def status(self) -> dict:
        return dict(self._status)


class _RaisingStatusAccountService(_StatusAccountService):
    def status(self) -> dict:
        raise RuntimeError("account_status_down")


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

    def test_available_trading_equity_falls_back_to_total_equity_when_available_missing(self) -> None:
        """When OKX does not return availEq (e.g. account-position-risk endpoint),
        fall back to total_equity from the same risk snapshot."""
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

        self.assertEqual(resolved, Decimal("450"))

    def test_available_trading_equity_falls_back_to_portfolio_when_risk_snapshot_empty(self) -> None:
        """When risk_snapshot has neither available nor total equity,
        fall back to portfolio_snapshot.total_equity."""
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            risk_snapshot=ExchangeAccountRiskSnapshot(),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("390")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("390"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
        )

        resolved = DecisionContextBuilder._available_trading_equity(
            account_snapshot=account_snapshot,
            portfolio_snapshot=portfolio_snapshot,
        )

        self.assertEqual(resolved, Decimal("390"))

    def test_exchange_required_available_trading_equity_prefers_okx_balance_available(self) -> None:
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            balances=[
                ExchangeBalance(
                    currency="USDT",
                    total=Decimal("420"),
                    available=Decimal("390"),
                    frozen=Decimal("30"),
                )
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("10000")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("10000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
        )

        resolved = DecisionContextBuilder._available_trading_equity(
            account_snapshot=account_snapshot,
            portfolio_snapshot=portfolio_snapshot,
            require_exchange_available=True,
        )

        self.assertEqual(resolved, Decimal("390"))

    def test_exchange_required_available_trading_equity_uses_symbol_quote_currency_only(self) -> None:
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            balances=[
                ExchangeBalance(
                    currency="USDT",
                    total=Decimal("420"),
                    available=Decimal("390"),
                    frozen=Decimal("30"),
                ),
                ExchangeBalance(
                    currency="USDC",
                    total=Decimal("900"),
                    available=Decimal("900"),
                    frozen=Decimal("0"),
                ),
            ],
            risk_snapshot=ExchangeAccountRiskSnapshot(),
        )

        resolved = DecisionContextBuilder._available_trading_equity(
            account_snapshot=account_snapshot,
            portfolio_snapshot=None,
            require_exchange_available=True,
            symbol="BTC-USDT-SWAP",
        )

        self.assertEqual(resolved, Decimal("390"))

    def test_exchange_required_available_trading_equity_does_not_fall_back_to_portfolio(self) -> None:
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            risk_snapshot=ExchangeAccountRiskSnapshot(total_equity=Decimal("450")),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("10000")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("10000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
        )

        with self.assertLogs("aats.decision_engine.context_builder", level="CRITICAL") as ctx:
            resolved = DecisionContextBuilder._available_trading_equity(
                account_snapshot=account_snapshot,
                portfolio_snapshot=portfolio_snapshot,
                require_exchange_available=True,
            )

        self.assertEqual(resolved, Decimal("0"))
        self.assertTrue(
            any(
                "available_trading_equity_all_fallbacks_exhausted" in record.getMessage()
                for record in ctx.records
            )
        )

    def test_exchange_required_account_snapshot_ignores_stale_latest_when_status_not_ready(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            balances=[
                ExchangeBalance(
                    currency="USDT",
                    total=Decimal("1000"),
                    available=Decimal("1000"),
                    frozen=Decimal("0"),
                )
            ],
        )
        settings = AATSSettings.model_validate(
            {
                "account_backend": "okx",
                "account_read_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        builder = DecisionContextBuilder(
            settings=settings,
            event_store=InMemoryEventStore(),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=InMemoryExecutionRepository(),
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
            account_service=_StatusAccountService(
                snapshot=snapshot,
                status={"ready": False, "last_error": "balance_down"},
            ),
        )

        self.assertIsNone(builder._account_snapshot())

    def test_account_snapshot_status_error_is_not_treated_as_fresh_exchange_state(self) -> None:
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            balances=[
                ExchangeBalance(
                    currency="USDT",
                    total=Decimal("1000"),
                    available=Decimal("1000"),
                    frozen=Decimal("0"),
                )
            ],
        )
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        builder = DecisionContextBuilder(
            settings=settings,
            event_store=InMemoryEventStore(),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=InMemoryExecutionRepository(),
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
            account_service=_RaisingStatusAccountService(snapshot=snapshot, status={}),
        )

        self.assertIsNone(builder._account_snapshot())

    def test_real_market_paper_can_fall_back_to_local_portfolio_when_okx_status_not_ready(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "paper_live",
                "market_data_backend": "okx",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        builder = DecisionContextBuilder(
            settings=settings,
            event_store=InMemoryEventStore(),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=InMemoryExecutionRepository(),
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
            account_service=_StatusAccountService(
                snapshot=ExchangeAccountSnapshot(
                    account_source="okx",
                    fetched_at=datetime.now(timezone.utc),
                    balances=[
                        ExchangeBalance(
                            currency="USDT",
                            total=Decimal("1000"),
                            available=Decimal("1000"),
                            frozen=Decimal("0"),
                        )
                    ],
                ),
                status={"ready": False, "last_error": "balance_down"},
            ),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("10000")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("10000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
        )

        self.assertFalse(builder._exchange_available_balance_required())
        self.assertIsNone(builder._account_snapshot())
        resolved = DecisionContextBuilder._available_trading_equity(
            account_snapshot=builder._account_snapshot(),
            portfolio_snapshot=portfolio_snapshot,
            require_exchange_available=builder._exchange_available_balance_required(),
        )

        self.assertEqual(resolved, Decimal("10000"))

    def test_available_trading_equity_returns_zero_when_no_data(self) -> None:
        """When both account and portfolio snapshots are None, return zero."""
        with self.assertLogs("aats.decision_engine.context_builder", level="CRITICAL") as ctx:
            resolved = DecisionContextBuilder._available_trading_equity(
                account_snapshot=None,
                portfolio_snapshot=None,
            )

        self.assertEqual(resolved, Decimal("0"))
        # R4-D4：三级 fallback 全部失败时必须喊出来，否则下游
        # resolve_balance_aware_reference_qty 基于 0 计算出的名义仓位
        # 只会被 P0-D4 的零余额 guard 挡掉，运营侧拿不到任何预警线索。
        self.assertTrue(
            any(
                "available_trading_equity_all_fallbacks_exhausted" in record.getMessage()
                for record in ctx.records
            )
        )

    def test_available_trading_equity_logs_critical_when_all_sources_zero(self) -> None:
        """即使 snapshot 都存在、但三路数据都是 0/None，也必须 CRITICAL 告警，
        因为 0 equity 已经无法继续 size 任何仓位，这是真实运营事故而非正常状态。"""
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            risk_snapshot=ExchangeAccountRiskSnapshot(
                available_equity=Decimal("0"),
                total_equity=Decimal("0"),
            ),
        )
        portfolio_snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": Decimal("0")},
            positions=[],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("0"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            risk_budget_usage={},
        )

        with self.assertLogs("aats.decision_engine.context_builder", level="CRITICAL") as ctx:
            resolved = DecisionContextBuilder._available_trading_equity(
                account_snapshot=account_snapshot,
                portfolio_snapshot=portfolio_snapshot,
            )

        self.assertEqual(resolved, Decimal("0"))
        critical_records = [
            record for record in ctx.records if record.levelname == "CRITICAL"
        ]
        self.assertEqual(len(critical_records), 1)
        message = critical_records[0].getMessage()
        self.assertIn("available_trading_equity_all_fallbacks_exhausted", message)
        self.assertIn("account_snapshot_present=True", message)
        self.assertIn("portfolio_snapshot_present=True", message)

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

    def test_build_uses_market_snapshot_hint_when_cache_is_stale(self) -> None:
        """2026-04-23 P1-a 锚定: market_snapshot_hint 优先于 stream_cache。

        场景：trigger 在 T1 抓到 snapshot 并决定触发 run_cycle；但 run_cycle
        实际执行到 context_builder.build() 时（可能几百 ms 后），stream_cache
        已经被 T2 的新 snapshot 覆盖。pre-fix 行为：build 读 cache 得到 T2
        snapshot，决策依据与 trigger 评估依据不一致（ref 漂移）。post-fix
        行为：hint 里的 T1 snapshot 优先用于决策路径，ref 指向 inline envelope
        供 audit。
        """
        from aats.schemas.market import MarketSnapshot
        from aats.storage.stream_snapshot_cache import StreamSnapshotCache

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

        now = datetime.now(timezone.utc)
        trigger_ts = now - timedelta(milliseconds=500)  # T1 = trigger 时刻
        cache_ts = now - timedelta(milliseconds=100)    # T2 = cache 最新（已 overwrite）

        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
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

        # Cache 里的是 T2 市场（last_price=71000）——新的
        cache_envelope = build_envelope(
            topic=topics.MARKET_SNAPSHOTS,
            key="BTC-USDT-SWAP",
            payload_model=MarketSnapshot(
                symbol="BTC-USDT-SWAP",
                exchange="OKX",
                snapshot_ts=cache_ts,
                best_bid=71_000.0,
                best_ask=71_001.0,
                last_price=71_000.5,
                bid_size=1.0,
                ask_size=1.0,
                volume_24h=10_000_000.0,
                kline_15m={"open": 71_000.0, "high": 71_100.0, "low": 70_900.0, "close": 71_000.5},
                kline_1h={"open": 71_000.0, "high": 71_200.0, "low": 70_800.0, "close": 71_000.5},
            ),
            source_component="test_cache",
        )
        stream_cache = StreamSnapshotCache()
        stream_cache.update(cache_envelope)
        event_store.append(cache_envelope)

        # Feature envelope 只走 event_store 即可（本 test 不关心 feature 路径）
        event_store.append(
            build_envelope(
                topic=topics.FEATURE_SNAPSHOTS,
                key="BTC-USDT-SWAP",
                payload_model=FeatureSnapshot(
                    symbol="BTC-USDT-SWAP",
                    snapshot_ts=cache_ts,
                    market_snapshot_ref=cache_envelope.event_id,
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

        # Hint 是 T1 市场（trigger 时刻的 snapshot）——旧的，但是决策应依据的
        hint_snapshot = MarketSnapshot(
            symbol="BTC-USDT-SWAP",
            exchange="OKX",
            snapshot_ts=trigger_ts,
            best_bid=70_000.0,
            best_ask=70_001.0,
            last_price=70_000.5,  # 与 cache 的 71_000 显著不同
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10_000_000.0,
            kline_15m={"open": 69_900.0, "high": 70_100.0, "low": 69_800.0, "close": 70_000.5},
            kline_1h={"open": 69_800.0, "high": 70_200.0, "low": 69_700.0, "close": 70_000.5},
        )

        builder = DecisionContextBuilder(
            settings=settings,
            event_store=event_store,
            portfolio_repo=portfolio_repo,
            execution_repo=execution_repo,
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
            stream_snapshot_cache=stream_cache,
        )

        context = builder.build(
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_p1a_hint_test",
            health_snapshot_ref="evt_health_p1a_hint_test",
            market_snapshot_hint=hint_snapshot,
        )

        # 关键断言 1: last_price 来自 hint（70000），不是 cache（71000）
        self.assertEqual(
            context.market_last_price,
            Decimal("70000.5"),
            "build 必须用 hint 的 market snapshot，不能因为 cache 已 overwrite 而读 cache",
        )
        # 关键断言 2: market_snapshot_ref 指向 inline envelope（非 cache 的 event_id）
        self.assertTrue(
            context.market_snapshot_ref.startswith("inline_market_"),
            f"cache 已 overwrite 时应 synthesize inline envelope，实际 ref={context.market_snapshot_ref}",
        )
        self.assertNotEqual(
            context.market_snapshot_ref,
            cache_envelope.event_id,
            "build 不应该使用 cache 里的 envelope（语义已漂移）",
        )

    def test_build_uses_cache_envelope_when_hint_ts_matches(self) -> None:
        """2026-04-23 P1-a 锚定补充: 当 cache 仍保留 trigger 瞬间的 envelope
        （即 hint.snapshot_ts == cache.payload.snapshot_ts），应优先用 cache
        envelope（保留 canonical event_id 供 audit trail）而非 synthesize inline。
        """
        from aats.schemas.market import MarketSnapshot
        from aats.storage.stream_snapshot_cache import StreamSnapshotCache

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

        now = datetime.now(timezone.utc)

        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
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

        # Cache 和 hint 是同一个 ts（trigger 刚触发，尚未被覆盖）
        shared_ts = now - timedelta(milliseconds=200)
        shared_snapshot = MarketSnapshot(
            symbol="BTC-USDT-SWAP",
            exchange="OKX",
            snapshot_ts=shared_ts,
            best_bid=70_000.0,
            best_ask=70_001.0,
            last_price=70_000.5,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=10_000_000.0,
            kline_15m={"open": 69_900.0, "high": 70_100.0, "low": 69_800.0, "close": 70_000.5},
            kline_1h={"open": 69_800.0, "high": 70_200.0, "low": 69_700.0, "close": 70_000.5},
        )
        cache_envelope = build_envelope(
            topic=topics.MARKET_SNAPSHOTS,
            key="BTC-USDT-SWAP",
            payload_model=shared_snapshot,
            source_component="test_canonical",
        )
        stream_cache = StreamSnapshotCache()
        stream_cache.update(cache_envelope)
        event_store.append(cache_envelope)

        event_store.append(
            build_envelope(
                topic=topics.FEATURE_SNAPSHOTS,
                key="BTC-USDT-SWAP",
                payload_model=FeatureSnapshot(
                    symbol="BTC-USDT-SWAP",
                    snapshot_ts=shared_ts,
                    market_snapshot_ref=cache_envelope.event_id,
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
            stream_snapshot_cache=stream_cache,
        )

        context = builder.build(
            "BTC-USDT-SWAP",
            "15m",
            decision_id="decision_p1a_canonical_test",
            health_snapshot_ref="evt_health_p1a_canonical_test",
            market_snapshot_hint=shared_snapshot,
        )

        # hint 的 snapshot_ts 等于 cache 的 → 用 cache 的 canonical event_id
        self.assertEqual(context.market_snapshot_ref, cache_envelope.event_id)
        self.assertFalse(
            context.market_snapshot_ref.startswith("inline_market_"),
            "cache 仍保留同 ts envelope 时，不应 synthesize inline",
        )

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
        # P1-10：context_builder 对 market_snapshot 做了 45s 新鲜度检查，historical
        # portfolio 快照测试不应该被行情新鲜度拖住，给 market/feature 用 now。
        fresh_market_ts = datetime.now(timezone.utc)
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
                    snapshot_ts=fresh_market_ts,
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
                    snapshot_ts=fresh_market_ts,
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

    def test_exchange_position_truth_overrides_stale_local_derivatives_position(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "mode": "guarded_live",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        now = datetime.now(timezone.utc)
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now,
                snapshot_origin="recovery_auto_healed",
                balances={"USDT": Decimal("373")},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=Decimal("0.0001"),
                        position_notional=Decimal("10"),
                        avg_entry_price=Decimal("100000"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
                cost_basis={},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("373"),
                gross_exposure=Decimal("10"),
                net_exposure=Decimal("10"),
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
                    snapshot_ts=now,
                    best_bid=100_000.0,
                    best_ask=100_001.0,
                    last_price=100_000.5,
                    bid_size=1.0,
                    ask_size=1.0,
                    volume_24h=10_000_000.0,
                    kline_15m={"open": 99_900.0, "high": 100_100.0, "low": 99_800.0, "close": 100_000.5},
                    kline_1h={"open": 99_800.0, "high": 100_200.0, "low": 99_700.0, "close": 100_000.5},
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
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=now,
            balances=[
                ExchangeBalance(
                    currency="USDT",
                    total=Decimal("373"),
                    available=Decimal("373"),
                    frozen=Decimal("0"),
                )
            ],
            positions=[],
            position_mode="long_short_mode",
        )
        builder = DecisionContextBuilder(
            settings=settings,
            event_store=event_store,
            portfolio_repo=portfolio_repo,
            execution_repo=InMemoryExecutionRepository(),
            mode_controller=RuntimeModeController(settings=settings, kill_switch=KillSwitch()),
            health_service=_FakeHealthService(),
            account_service=_StatusAccountService(
                snapshot=account_snapshot,
                status={"ready": True, "last_error": None},
            ),
        )

        with self.assertLogs("aats.decision_engine.context_builder", level="WARNING") as ctx:
            context = builder.build(
                "BTC-USDT-SWAP",
                "15m",
                decision_id="decision_exchange_flat",
                health_snapshot_ref="evt_health",
            )

        self.assertEqual(context.current_position_qty, Decimal("0"))
        self.assertEqual(context.current_net_position_qty, Decimal("0"))
        self.assertEqual(context.current_gross_position_qty, Decimal("0"))
        self.assertEqual(context.current_long_position_qty, Decimal("0"))
        self.assertEqual(context.current_short_position_qty, Decimal("0"))
        self.assertEqual(context.current_position_legs, [])
        self.assertIsNone(context.current_position_state)
        self.assertEqual(context.current_exposure_side, "flat")
        self.assertEqual(context.current_target_leverage, 1.0)
        self.assertEqual(context.available_trading_equity, Decimal("373"))
        self.assertTrue(
            any(
                "exchange_position_truth_overrode_portfolio_position" in record.getMessage()
                for record in ctx.records
            )
        )

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
