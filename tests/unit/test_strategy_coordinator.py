from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import unittest

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import (
    BaselineAssessment,
    DecisionContext,
    DecisionOutcome,
    PositionSizingBreakdown,
    PositionTarget,
)
from aats.schemas.execution import FillEvent
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance, ExchangePosition
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot, SleevePnLRecord
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    StrategyBookRuntimeState,
    StrategyCandidate,
    StrategyCoordinatorSnapshot,
    StrategyLegIntent,
    StrategySleeveIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.allocator import PortfolioAllocatorV2Phase2
from aats.services.strategy_engines.base import (
    StrategyEngineInput,
    StrategyEvaluationContext,
    StrategyFamilyRuntimeControl,
    StrategyMarketHistoryRequest,
)
from aats.services.strategy_engines.coordinator import StrategyCoordinatorService
from aats.services.strategy_engines.sleeve_identity import build_strategy_sleeve_id
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository
from aats.storage.sleeve_pnl_repo import InMemorySleevePnLRepository
from aats.storage.strategy_sleeve_repo import InMemoryStrategySleeveRepository


class _FakeMarketGateway:
    def __init__(self, snapshots: dict[str, MarketSnapshot]) -> None:
        self._snapshots = snapshots

    def latest_snapshot(self, symbol: str):
        return self._snapshots.get(symbol)


class _StaticAccountService:
    def __init__(self, snapshot: ExchangeAccountSnapshot) -> None:
        self._snapshot = snapshot

    def latest_snapshot(self):
        return self._snapshot


def _market_snapshot(symbol: str, price: str) -> MarketSnapshot:
    price_decimal = Decimal(price)
    return MarketSnapshot(
        symbol=symbol,
        exchange="TEST",
        snapshot_ts=utc_now(),
        best_bid=price_decimal - Decimal("0.1"),
        best_ask=price_decimal + Decimal("0.1"),
        last_price=price_decimal,
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume_24h=Decimal("1000"),
        kline_15m={"open": price_decimal, "high": price_decimal, "low": price_decimal, "close": price_decimal, "volume": Decimal("100")},
        kline_1h={"open": price_decimal, "high": price_decimal, "low": price_decimal, "close": price_decimal, "volume": Decimal("400")},
        recent_trades=[],
        orderbook_depth={},
    )


def _portfolio_snapshot(
    *,
    product_type: str,
    margin_mode: str,
    total_equity: str = "1000",
) -> PortfolioSnapshot:
    total_equity_decimal = Decimal(total_equity)
    return PortfolioSnapshot(
        snapshot_ts=utc_now(),
        balances={"USDT": total_equity_decimal},
        positions=[],
        cost_basis={},
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=total_equity_decimal,
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        risk_budget_usage={},
        product_type=product_type,
        margin_mode=margin_mode,
    )


def _decision_context(
    *,
    symbol: str,
    product_type: str,
    current_position_qty: str,
    available_trading_equity: str = "10000",
) -> DecisionContext:
    now = utc_now()
    quantity = Decimal(current_position_qty)
    return DecisionContext(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        timeframe="15m",
        as_of_ts=now,
        market_snapshot_ref="market_ref",
        feature_snapshot_ref="feature_ref",
        portfolio_snapshot_ref="portfolio_ref",
        health_snapshot_ref="health_ref",
        mode="paper_live",
        current_position_qty=quantity,
        current_open_orders=[],
        product_type=product_type,
        current_exposure_side="flat" if quantity == 0 else ("long" if quantity > 0 else "short"),
        available_trading_equity=Decimal(available_trading_equity),
    )


def _baseline(*, symbol: str, regime: str = "range", confidence: float = 0.65) -> BaselineAssessment:
    return BaselineAssessment(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        regime=regime,
        direction_bias="flat",
        trend_strength=0.2,
        volatility_state="medium",
        confidence=confidence,
        composite_alpha_score=0.15,
        suggested_position_scale=0.4,
        volatility_target_scale=1.0,
        factor_scores={},
        holding_horizon="15m",
        invalidation_conditions=[],
        reason_codes=[f"regime_{regime}"],
        engine_version="test",
    )


def _decision_outcome(symbol: str) -> DecisionOutcome:
    return DecisionOutcome(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        decision_source="baseline",
        decision_authority="reference_only",
    )


def _position_target(
    *,
    symbol: str,
    product_type: str,
    margin_mode: str,
    current_qty: str,
    target_qty: str,
    rebalance_reason: str = "directional_target",
    position_intent: str = "hold",
    target_leverage: float | None = None,
    sizing_breakdown: PositionSizingBreakdown | None = None,
) -> PositionTarget:
    now = utc_now()
    current_decimal = Decimal(current_qty)
    target_decimal = Decimal(target_qty)
    return PositionTarget(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        current_position_qty=current_decimal,
        target_position_qty=target_decimal,
        delta_position_qty=target_decimal - current_decimal,
        current_notional=current_decimal * Decimal("100"),
        target_notional=target_decimal * Decimal("100"),
        rebalance_reason=rebalance_reason,
        urgency="medium",
        max_slippage_tolerance_bps=10,
        source_mix={"directional": 1.0},
        decision_expiry_ts=now + timedelta(minutes=5),
        product_type=product_type,
        current_exposure_side="flat" if current_decimal == 0 else "long",
        target_exposure_side="flat" if target_decimal == 0 else "long",
        position_intent=position_intent,
        target_leverage=1.0 if target_leverage is None else target_leverage,
        margin_mode=margin_mode,
        expected_signal_edge_bps=12.0,
        expected_cost_bps=4.0,
        expected_net_edge_bps=8.0,
        sizing_breakdown=sizing_breakdown,
        decision_outcome=_decision_outcome(symbol),
    )


def _fill_event(
    *,
    fill_id: str,
    symbol: str,
    side: str,
    qty: str,
    price: str,
    product_type: str,
    margin_mode: str,
    strategy_family: str,
    strategy_sleeve_id: str,
    allocation_id: str = "alloc_test",
    strategy_leg_role: str | None = None,
    position_intent: str = "open_long",
    position_mode: str | None = None,
    pos_side: str | None = None,
) -> FillEvent:
    timestamp = utc_now()
    exposure_side = "long"
    if product_type == "derivatives" and side == "sell":
        exposure_side = "short"
    return FillEvent(
        fill_id=fill_id,
        decision_id=f"decision_{symbol}",
        intent_id=f"intent_{fill_id}",
        client_order_id=f"client_{fill_id}",
        exchange_order_id=f"exchange_{fill_id}",
        symbol=symbol,
        side=side,
        fill_qty=Decimal(qty),
        fill_price=Decimal(price),
        fee_amount=Decimal("0"),
        fee_currency="USDT",
        strategy_family=strategy_family,
        strategy_sleeve_id=strategy_sleeve_id,
        allocation_id=allocation_id,
        strategy_bundle_id="bundle_test" if strategy_family == "smart_arbitrage" else None,
        strategy_leg_role=strategy_leg_role,
        product_type=product_type,
        target_leverage=1.0 if product_type == "spot" else 3.0,
        margin_mode=margin_mode,
        exposure_side=exposure_side,
        execution_action="enter",
        position_intent=position_intent,
        liquidity_role="maker",
        exchange_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        position_mode=position_mode,
        pos_side=pos_side,
        td_mode=margin_mode,
        settle_currency="USDT",
    )


def _independent_full_close_intent(
    *,
    decision_id: str,
    family_action: str,
    book_action: str,
    close_reason: str,
    current_qty: str = "0.007",
    reference_price: str = "100000",
) -> StrategySleeveIntent:
    current_decimal = Decimal(current_qty)
    sleeve_id = build_strategy_sleeve_id(
        family="independent",
        primary_symbol="BTC-USDT-SWAP",
        product_scope="derivatives",
        margin_scope="cross",
        symbol_scope=("BTC-USDT-SWAP",),
    )
    leg = StrategyLegIntent(
        symbol="BTC-USDT-SWAP",
        execution_chain_id=f"independent:{decision_id}:long:{book_action}",
        product_type="derivatives",
        side="sell",
        position_mode="long_short_mode",
        pos_side="long",
        action="close",
        family="independent",
        role="primary",
        margin_mode="cross",
        target_leverage=3.0,
        current_position_qty=current_decimal,
        target_position_qty=Decimal("0"),
        delta_position_qty=-current_decimal,
        reference_price=Decimal(reference_price),
        execution_compatible=True,
        execution_mode="independent_long_book",
        strategy_sleeve_id=sleeve_id,
        book_action=book_action,
        close_reason=close_reason,
        policy_reason=f"test_{close_reason}",
    )
    return StrategySleeveIntent(
        decision_id=decision_id,
        family="independent",
        strategy_sleeve_id=sleeve_id,
        state="unwinding",
        symbol="BTC-USDT-SWAP",
        product_type="derivatives",
        margin_mode="cross",
        inventory_policy="inventory_accumulation",
        route_action="override_target",
        family_action=family_action,
        headline="independent full close",
        selectable=True,
        execution_compatible=True,
        current_position_qty=current_decimal,
        target_position_qty=Decimal("0"),
        delta_position_qty=-current_decimal,
        target_notional=Decimal("0"),
        requested_delta_position_qty=-current_decimal,
        legs=[leg],
    )


class TestStrategyCoordinator(unittest.TestCase):
    def test_recent_market_snapshots_fetch_each_symbol_independently(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT", "ETH-USDT"),
                "spot_grid_enabled": True,
                "spot_grid_anchor_lookback_snapshots": 2,
            }
        )
        event_store = InMemoryEventStore()
        for symbol, price in (
            ("ETH-USDT", "200"),
            ("ETH-USDT", "201"),
            ("BTC-USDT", "100"),
            ("BTC-USDT", "101"),
            ("BTC-USDT", "102"),
            ("BTC-USDT", "103"),
            ("BTC-USDT", "104"),
        ):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key=symbol,
                    payload_model=_market_snapshot(symbol, price),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "104"),
                    "ETH-USDT": _market_snapshot("ETH-USDT", "201"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        rows = coordinator._recent_market_snapshots(
            requests={
                "spot_grid": StrategyMarketHistoryRequest(
                    family="spot_grid",
                    symbols=("BTC-USDT", "ETH-USDT"),
                    topic=topics.MARKET_SNAPSHOTS,
                    sampling_source="event_store_recent",
                    lookback_snapshots=2,
                )
            }
        )

        self.assertEqual(
            [snapshot.last_price for snapshot in rows["BTC-USDT"]],
            [Decimal("103"), Decimal("104")],
        )
        self.assertEqual(
            [snapshot.last_price for snapshot in rows["ETH-USDT"]],
            [Decimal("200"), Decimal("201")],
        )

    def test_recent_market_snapshots_are_dispatched_with_family_specific_windows(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "spot_grid_enabled": True,
                "spot_grid_anchor_lookback_snapshots": 1,
                "dca_enabled": True,
                "dca_pullback_only_enabled": True,
            }
        )
        event_store = InMemoryEventStore()
        for price in ("100", "99", "98"):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", price),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "98")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        resolved_pair_definitions_by_family = coordinator._resolved_pair_definitions_by_family(
            primary_symbol="BTC-USDT"
        )
        requests = coordinator._market_history_requests(
            primary_symbol="BTC-USDT",
            resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
        )
        latest_market_snapshots_by_symbol_by_family = coordinator._latest_market_snapshots_by_symbol_by_family(
            requests=requests,
        )
        latest_snapshots = coordinator._latest_market_snapshots_by_family(
            requests=requests,
            latest_market_snapshots_by_symbol_by_family=latest_market_snapshots_by_symbol_by_family,
        )
        rows = coordinator._recent_market_snapshots(requests=requests)
        directional_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0",
        )
        overlay_parent_exposures_by_family = coordinator._overlay_parent_exposures_by_family(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            directional_target=directional_target,
        )
        windows = {
            family: request.lookback_snapshots
            for family, request in requests.items()
        }
        evaluation_context = StrategyEvaluationContext.from_engine_input(
            StrategyEngineInput(
                context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
                baseline=_baseline(symbol="BTC-USDT", regime="range"),
                directional_target=directional_target,
                latest_snapshot=None,
                latest_account_snapshot=None,
                latest_market_snapshot=_market_snapshot("BTC-USDT", "98"),
                resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
                latest_market_snapshots_by_symbol_by_family=latest_market_snapshots_by_symbol_by_family,
                latest_market_snapshots_by_family=latest_snapshots,
                overlay_parent_exposures_by_family=overlay_parent_exposures_by_family,
                recent_market_snapshots=rows,
                recent_targets_by_family={},
                recent_market_snapshot_windows_by_family=windows,
                market_history_requests_by_family=requests,
            ),
            family_runtime_controls={
                "directional": StrategyFamilyRuntimeControl(enabled=True, live_execution_enabled=True),
                "smart_arbitrage": StrategyFamilyRuntimeControl(),
                "spot_grid": StrategyFamilyRuntimeControl(enabled=True, live_execution_enabled=True),
                "dca": StrategyFamilyRuntimeControl(enabled=True, live_execution_enabled=True),
                "independent": StrategyFamilyRuntimeControl(),
            },
        )

        self.assertEqual(requests["directional"].sampling_source, "not_required")
        self.assertEqual(requests["directional"].latest_snapshot_source, "not_required")
        self.assertEqual(requests["smart_arbitrage"].sampling_source, "not_required")
        self.assertIn("BTC-USDT", requests["smart_arbitrage"].symbols)
        self.assertEqual(requests["smart_arbitrage"].latest_snapshot_source, "gateway_or_event_store_latest")
        self.assertIn("BTC-USDT", requests["smart_arbitrage"].latest_snapshot_symbols)
        self.assertEqual(requests["spot_grid"].sampling_source, "event_store_recent")
        self.assertEqual(requests["spot_grid"].topic, topics.MARKET_SNAPSHOTS)
        self.assertEqual(requests["spot_grid"].symbols, ("BTC-USDT",))
        self.assertEqual(requests["spot_grid"].latest_snapshot_symbols, ("BTC-USDT",))
        self.assertEqual(requests["spot_grid"].latest_snapshot_symbol, "BTC-USDT")
        self.assertEqual(requests["spot_grid"].latest_snapshot_topic, topics.MARKET_SNAPSHOTS)
        self.assertEqual(requests["spot_grid"].latest_snapshot_source, "gateway_or_event_store_latest")
        self.assertEqual(requests["dca"].sampling_source, "event_store_recent")
        self.assertEqual(requests["dca"].symbols, ("BTC-USDT",))
        self.assertEqual(requests["dca"].latest_snapshot_symbols, ("BTC-USDT",))
        self.assertEqual(requests["dca"].latest_snapshot_symbol, "BTC-USDT")
        self.assertEqual(requests["dca"].latest_snapshot_topic, topics.MARKET_SNAPSHOTS)
        self.assertEqual(requests["dca"].latest_snapshot_source, "gateway_or_event_store_latest")
        self.assertEqual(windows["spot_grid"], 1)
        self.assertEqual(windows["dca"], 2)
        self.assertEqual(len(rows["BTC-USDT"]), 2)
        self.assertIsNone(latest_snapshots["directional"])
        self.assertEqual(latest_market_snapshots_by_symbol_by_family["smart_arbitrage"]["BTC-USDT"].symbol, "BTC-USDT")
        self.assertEqual(latest_snapshots["spot_grid"].symbol, "BTC-USDT")
        self.assertEqual(latest_snapshots["dca"].symbol, "BTC-USDT")
        self.assertEqual(evaluation_context.for_family("directional").recent_market_snapshots, {})
        self.assertIsNone(evaluation_context.for_family("directional").latest_market_snapshot)
        self.assertEqual(len(evaluation_context.for_family("spot_grid").recent_market_snapshots["BTC-USDT"]), 1)
        self.assertEqual(evaluation_context.for_family("spot_grid").latest_market_snapshot.symbol, "BTC-USDT")
        self.assertEqual(len(evaluation_context.for_family("dca").recent_market_snapshots["BTC-USDT"]), 2)
        self.assertEqual(evaluation_context.for_family("dca").latest_market_snapshot.symbol, "BTC-USDT")
        self.assertNotIn("protective", requests)
        self.assertNotIn("opportunistic", requests)
        self.assertEqual(overlay_parent_exposures_by_family, {})
        self.assertIsNone(evaluation_context.for_family("independent").overlay_parent_exposure)

    def test_latest_portfolio_and_account_snapshots_are_dispatched_with_family_specific_requests(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
            }
        )
        portfolio_repo = InMemoryPortfolioRepository()
        portfolio_repo.save_snapshot(
            _portfolio_snapshot(
                product_type="derivatives",
                margin_mode="cross",
                total_equity="2500",
            )
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("2500"), available=Decimal("2400"))],
            positions=[],
            account_mode="cross",
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "99"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
                }
            ),
            portfolio_repo=portfolio_repo,
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        resolved_pair_definitions_by_family = coordinator._resolved_pair_definitions_by_family(
            primary_symbol="BTC-USDT-SWAP"
        )
        requests = coordinator._market_history_requests(
            primary_symbol="BTC-USDT-SWAP",
            resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
        )
        latest_snapshots = coordinator._latest_snapshots_by_family(
            requests=requests,
            latest_snapshot=coordinator._latest_portfolio_snapshot(),
        )
        latest_account_snapshots = coordinator._latest_account_snapshots_by_family(
            requests=requests,
            latest_account_snapshot=coordinator._latest_account_snapshot(),
        )
        latest_market_snapshots_by_symbol_by_family = coordinator._latest_market_snapshots_by_symbol_by_family(
            requests=requests,
        )
        latest_market_snapshots = coordinator._latest_market_snapshots_by_family(
            requests=requests,
            latest_market_snapshots_by_symbol_by_family=latest_market_snapshots_by_symbol_by_family,
        )
        evaluation_context = StrategyEvaluationContext.from_engine_input(
            StrategyEngineInput(
                context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
                baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
                directional_target=_position_target(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    margin_mode="cross",
                    current_qty="0",
                    target_qty="0",
                ),
                latest_snapshot=coordinator._latest_portfolio_snapshot(),
                latest_account_snapshot=coordinator._latest_account_snapshot(),
                latest_market_snapshot=_market_snapshot("BTC-USDT-SWAP", "100"),
                recent_market_snapshots={},
                recent_targets_by_family={},
                latest_snapshots_by_family=latest_snapshots,
                latest_account_snapshots_by_family=latest_account_snapshots,
                resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
                latest_market_snapshots_by_symbol_by_family=latest_market_snapshots_by_symbol_by_family,
                latest_market_snapshots_by_family=latest_market_snapshots,
                recent_market_snapshot_windows_by_family={
                    family: request.lookback_snapshots
                    for family, request in requests.items()
                },
                market_history_requests_by_family=requests,
            ),
            family_runtime_controls={
                "directional": StrategyFamilyRuntimeControl(enabled=True, live_execution_enabled=True),
                "smart_arbitrage": StrategyFamilyRuntimeControl(enabled=True, live_execution_enabled=True),
                "spot_grid": StrategyFamilyRuntimeControl(),
                "dca": StrategyFamilyRuntimeControl(),
                "protective": StrategyFamilyRuntimeControl(),
                "opportunistic": StrategyFamilyRuntimeControl(),
                "independent": StrategyFamilyRuntimeControl(),
            },
        )

        self.assertEqual(requests["smart_arbitrage"].latest_portfolio_snapshot_source, "runtime_scope_latest")
        self.assertEqual(requests["smart_arbitrage"].latest_account_snapshot_source, "account_service_latest")
        self.assertEqual(requests["smart_arbitrage"].latest_snapshot_source, "gateway_or_event_store_latest")
        self.assertEqual(len(resolved_pair_definitions_by_family["smart_arbitrage"]), 1)
        self.assertIn("BTC-USDT", requests["smart_arbitrage"].latest_snapshot_symbols)
        self.assertIn("BTC-USDT-SWAP", requests["smart_arbitrage"].latest_snapshot_symbols)
        self.assertEqual(requests["spot_grid"].latest_portfolio_snapshot_source, "not_required")
        self.assertEqual(requests["spot_grid"].latest_account_snapshot_source, "not_required")
        self.assertIsNotNone(latest_snapshots["smart_arbitrage"])
        self.assertIsNotNone(latest_account_snapshots["smart_arbitrage"])
        self.assertEqual(
            set(latest_market_snapshots_by_symbol_by_family["smart_arbitrage"]),
            {"BTC-USDT", "BTC-USDT-SWAP"},
        )
        self.assertEqual(latest_market_snapshots["smart_arbitrage"].symbol, "BTC-USDT")
        self.assertIsNone(latest_snapshots["spot_grid"])
        self.assertIsNone(latest_account_snapshots["spot_grid"])
        self.assertIsNone(evaluation_context.for_family("directional").latest_snapshot)
        self.assertIsNone(evaluation_context.for_family("directional").latest_account_snapshot)
        self.assertEqual(
            evaluation_context.for_family("smart_arbitrage").latest_snapshot.total_equity,
            Decimal("2500"),
        )
        self.assertEqual(
            evaluation_context.for_family("smart_arbitrage").latest_account_snapshot.account_mode,
            "cross",
        )
        self.assertEqual(
            set(evaluation_context.for_family("smart_arbitrage").latest_market_snapshots_by_symbol),
            {"BTC-USDT", "BTC-USDT-SWAP"},
        )

    def test_spot_grid_can_override_directional_target(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.05,
                "max_abs_position_qty": 1.0,
            }
        )
        event_store = InMemoryEventStore()
        portfolio_repo = InMemoryPortfolioRepository()
        latest_snapshot = _market_snapshot("BTC-USDT", "99")
        gateway = _FakeMarketGateway({"BTC-USDT": latest_snapshot})
        for price in ("100", "101", "100", "99"):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", price),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=gateway,
            portfolio_repo=portfolio_repo,
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0",
                target_qty="0",
            ),
        )
        applied = coordinator.apply_selected_target(
            base_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0",
                target_qty="0",
            ),
            snapshot=snapshot,
        )

        self.assertEqual(snapshot.selected_family, "spot_grid")
        self.assertEqual(snapshot.selected_route_action, "override_target")
        self.assertEqual(applied.strategy_family, "spot_grid")
        self.assertEqual(applied.strategy_route_action, "override_target")
        self.assertIsNotNone(applied.strategy_sleeve_id)
        self.assertIsNotNone(applied.allocation_id)
        self.assertGreater(abs(applied.target_position_qty), Decimal("0"))
        self.assertEqual(applied.target_notional, applied.target_position_qty * Decimal("99"))

    def test_spot_grid_uses_sleeve_inventory_truth_and_preserves_foreign_inventory(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.05,
                "max_abs_position_qty": 1.0,
            }
        )
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        gateway = _FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "99")})
        for price in ("100", "101", "100", "99"):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", price),
                    source_component="test",
                )
            )
        sleeve_id = build_strategy_sleeve_id(
            family="spot_grid",
            primary_symbol="BTC-USDT",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT",),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_grid_1",
                symbol="BTC-USDT",
                side="buy",
                qty="0.2",
                price="100",
                product_type="spot",
                margin_mode="cash",
                strategy_family="spot_grid",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="inventory",
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0.5",
            target_qty="0.5",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.5"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        candidate = next(item for item in snapshot.candidates if item.family == "spot_grid")

        self.assertEqual(snapshot.selected_family, "spot_grid")
        self.assertEqual(candidate.metrics["current_account_position_qty"], Decimal("0.5"))
        self.assertEqual(candidate.metrics["current_sleeve_position_qty"], Decimal("0.2"))
        self.assertEqual(candidate.metrics["target_sleeve_position_qty"], Decimal("0.6"))
        self.assertEqual(candidate.metrics["target_account_position_qty"], Decimal("0.9"))
        self.assertEqual(candidate.delta_position_qty, Decimal("0.4"))
        self.assertEqual(applied.target_position_qty, Decimal("0.9"))

    def test_spot_grid_requires_full_anchor_history_before_becoming_actionable(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.05,
                "max_abs_position_qty": 1.0,
            }
        )
        event_store = InMemoryEventStore()
        event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key="BTC-USDT",
                payload_model=_market_snapshot("BTC-USDT", "100"),
                source_component="test",
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        candidate = next(item for item in snapshot.candidates if item.family == "spot_grid")

        self.assertEqual(candidate.state, "inactive")
        self.assertEqual(candidate.route_action, "hold_current")
        self.assertIn("spot_grid_anchor_history_insufficient", candidate.reason_codes)
        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(applied.strategy_family, "directional")
        self.assertEqual(applied.target_position_qty, Decimal("0"))

    def test_spot_grid_respects_inventory_ceiling_fraction_above_one(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.5,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.0,
                "max_abs_position_qty": 1.0,
            }
        )
        event_store = InMemoryEventStore()
        for _ in range(4):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", "100"),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "95")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        candidate = next(item for item in snapshot.candidates if item.family == "spot_grid")

        self.assertEqual(snapshot.selected_family, "spot_grid")
        self.assertEqual(candidate.metrics["inventory_ceiling_qty"], Decimal("1.5"))
        self.assertEqual(candidate.metrics["target_sleeve_position_qty"], Decimal("1.5"))
        self.assertEqual(candidate.metrics["target_account_position_qty"], Decimal("1.5"))
        self.assertEqual(applied.target_position_qty, Decimal("1.5"))

    def test_spot_grid_fails_closed_when_band_width_config_is_non_positive(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 0.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.0,
                "max_abs_position_qty": 1.0,
            }
        )
        event_store = InMemoryEventStore()
        for _ in range(4):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", "100"),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        candidate = next(item for item in snapshot.candidates if item.family == "spot_grid")

        self.assertEqual(candidate.state, "inactive")
        self.assertEqual(candidate.route_action, "hold_current")
        self.assertIn("spot_grid_band_invalid", candidate.reason_codes)
        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(applied.target_position_qty, Decimal("0"))

    def test_smart_arbitrage_keeps_protective_directional_exit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "smart_arbitrage",
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 10.0,
                "smart_arbitrage_estimated_cost_bps": 2.0,
            }
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100.5"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.02",
            target_qty="0",
            rebalance_reason="risk_exit",
            position_intent="close_long",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0.02"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(snapshot.selected_route_action, "override_target")
        self.assertEqual(applied.strategy_family, "directional")
        self.assertEqual(applied.strategy_route_action, "override_target")
        self.assertEqual(applied.target_position_qty, Decimal("0"))
        self.assertEqual(applied.decision_outcome.selected_strategy_route_action, "override_target")

    def test_directional_selection_preserves_execution_metadata_for_strategy_legs(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "80000")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.05",
            target_qty="0.05",
        ).model_copy(
            update={
                "strategy_execution_mode": "protective_overlay",
                "strategy_state_phase": "active",
                "strategy_reason_codes": [
                    "protective_overlay_signal_above_open_threshold",
                ],
                "strategy_headline": "protective overlay 主链测试",
                "strategy_execution_legs": [
                    StrategyLegIntent(
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        side="sell",
                        position_mode="long_short_mode",
                        pos_side="short",
                        action="open",
                        family="directional",
                        role="hedge",
                        strategy_sleeve_id="sleeve_protective_short",
                        allocation_id="alloc_test",
                        margin_mode="cross",
                        target_leverage=2.0,
                        current_position_qty=Decimal("0"),
                        target_position_qty=Decimal("-0.02"),
                        delta_position_qty=Decimal("-0.02"),
                        reference_price=Decimal("80000"),
                        execution_compatible=True,
                        execution_mode="protective_overlay",
                        state_phase="active",
                        overlay_mode="protective",
                        trigger_reason_codes=["protective_overlay_signal_above_open_threshold"],
                    )
                ],
            }
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0.05"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        directional_candidate = next(item for item in snapshot.candidates if item.family == "directional")

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(directional_candidate.execution_mode, "protective_overlay")
        self.assertEqual(directional_candidate.state_phase, "active")
        self.assertIn(
            "protective_overlay_signal_above_open_threshold",
            directional_candidate.reason_codes,
        )
        self.assertEqual(directional_candidate.headline, "protective overlay 主链测试")
        self.assertEqual(applied.strategy_execution_mode, "protective_overlay")
        self.assertEqual(applied.strategy_state_phase, "active")
        self.assertIn(
            "protective_overlay_signal_above_open_threshold",
            applied.strategy_reason_codes,
        )
        self.assertEqual(len(applied.strategy_execution_legs), 1)
        self.assertEqual(applied.strategy_execution_legs[0].execution_mode, "protective_overlay")

    def test_spot_fixed_incompatible_family_falls_back_to_directional_selection(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "smart_arbitrage",
                "strategy_family_auto_selection_enabled": False,
                "smart_arbitrage_enabled": True,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(applied.strategy_family, "directional")
        self.assertIn("legacy_configured_strategy_directional_fallback", snapshot.selection_reason_codes)
        self.assertIn("smart_arbitrage_derivatives_runtime_required", snapshot.selection_reason_codes)
        self.assertNotIn("legacy_configured_strategy_family_smart_arbitrage", snapshot.selection_reason_codes)

    def test_derivatives_fixed_independent_blocked_keeps_independent_selection(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "independent",
                "strategy_family_auto_selection_enabled": False,
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        selected_family, candidate, reason_codes = coordinator._select_candidate(
            candidates_by_family={
                "directional": StrategyCandidate(
                    family="directional",
                    state="ready",
                    enabled=True,
                    selectable=True,
                    execution_compatible=True,
                    route_action="override_target",
                    family_action="hold_family",
                    headline="directional ready",
                ),
                "independent": StrategyCandidate(
                    family="independent",
                    state="blocked",
                    enabled=True,
                    selectable=False,
                    execution_compatible=False,
                    route_action="advisory_only",
                    family_action="blocked",
                    headline="independent blocked",
                    reason_codes=["independent_family_candidate_inactive"],
                    blocking_reasons=["independent_long_book_expected_cost_above_max_acceptable"],
                ),
            },
        )

        self.assertEqual(selected_family, "independent")
        self.assertEqual(candidate.family, "independent")
        self.assertIn("legacy_configured_strategy_family_independent_unavailable", reason_codes)
        self.assertIn("legacy_configured_strategy_family_independent_hold_only", reason_codes)
        self.assertNotIn("legacy_configured_strategy_directional_fallback", reason_codes)

    def test_derivatives_fixed_independent_incompatible_falls_back_to_directional_selection(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "independent",
                "strategy_family_auto_selection_enabled": False,
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        selected_family, candidate, reason_codes = coordinator._select_candidate(
            candidates_by_family={
                "directional": StrategyCandidate(
                    family="directional",
                    state="ready",
                    enabled=True,
                    selectable=True,
                    execution_compatible=True,
                    route_action="override_target",
                    family_action="hold_family",
                    headline="directional ready",
                ),
                "independent": StrategyCandidate(
                    family="independent",
                    state="incompatible",
                    enabled=True,
                    selectable=False,
                    execution_compatible=False,
                    route_action="advisory_only",
                    family_action="blocked",
                    headline="independent incompatible",
                    reason_codes=["hedge_overlay_runtime_not_supported"],
                    blocking_reasons=["hedge_overlay_runtime_not_supported"],
                ),
            },
        )

        self.assertEqual(selected_family, "directional")
        self.assertEqual(candidate.family, "directional")
        self.assertIn("legacy_configured_strategy_family_independent_unavailable", reason_codes)
        self.assertIn("legacy_configured_strategy_directional_fallback", reason_codes)
        self.assertNotIn("legacy_configured_strategy_family_independent_hold_only", reason_codes)

    def test_derivatives_fixed_independent_unavailable_does_not_approve_directional_or_retain_legs(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "independent",
                "strategy_family_auto_selection_enabled": False,
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        directional_leg = StrategyLegIntent(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            side="buy",
            position_mode="long_short_mode",
            pos_side="long",
            action="open",
            family="directional",
            role="primary",
            margin_mode="cross",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0.01"),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0.01",
            position_intent="open_long",
        ).model_copy(
            update={
                "strategy_family": "directional",
                "strategy_execution_legs": [directional_leg],
            }
        )
        independent_sleeve_id = build_strategy_sleeve_id(
            family="independent",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT-SWAP",),
        )
        directional_sleeve_id = build_strategy_sleeve_id(
            family="directional",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT-SWAP",),
        )
        sleeve_intents = [
            StrategySleeveIntent(
                decision_id=base_target.decision_id,
                family="independent",
                strategy_sleeve_id=independent_sleeve_id,
                state="blocked",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                inventory_policy="inventory_accumulation",
                route_action="advisory_only",
                family_action="blocked",
                headline="independent blocked",
                selectable=False,
                execution_compatible=False,
                current_position_qty=Decimal("0"),
                target_position_qty=Decimal("0"),
                delta_position_qty=Decimal("0"),
                reason_codes=["independent_family_candidate_inactive"],
                blocking_reasons=["independent_long_book_expected_cost_above_max_acceptable"],
            ).model_copy(update={"allocation_id": "alloc_fixed_independent"}),
            StrategySleeveIntent(
                decision_id=base_target.decision_id,
                family="directional",
                strategy_sleeve_id=directional_sleeve_id,
                state="ready",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                inventory_policy="account_net_inventory",
                route_action="override_target",
                family_action="hold_family",
                headline="directional ready",
                selectable=True,
                execution_compatible=True,
                current_position_qty=Decimal("0"),
                target_position_qty=Decimal("0.01"),
                delta_position_qty=Decimal("0.01"),
                reason_codes=["directional_strategy_target"],
                legs=[directional_leg],
            ).model_copy(update={"allocation_id": "alloc_fixed_directional"}),
        ]

        allocation = allocator.allocate(
            base_target=base_target,
            selected_family="independent",
            selection_reason_codes=["legacy_configured_strategy_family_independent_unavailable"],
            sleeve_intents=sleeve_intents,
        )

        self.assertEqual(allocation.primary_family, "independent")
        self.assertEqual(allocation.route_action, "advisory_only")
        self.assertEqual(allocation.approved_families, [])
        self.assertEqual(allocation.execution_legs, [])
        self.assertIn("allocator_primary_family_independent", allocation.reason_codes)

        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        snapshot = StrategyCoordinatorSnapshot(
            decision_id=base_target.decision_id,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=("BTC-USDT-SWAP",),
            active_family="independent",
            selected_family="independent",
            selected_state="blocked",
            selected_route_action="advisory_only",
            selected_family_action="blocked",
            selected_headline="independent blocked",
            selection_reason_codes=allocation.reason_codes,
            active_families=["directional", "independent"],
            approved_families=[],
            candidates=[
                StrategyCandidate(
                    family="independent",
                    state="blocked",
                    enabled=True,
                    selectable=False,
                    execution_compatible=False,
                    route_action="advisory_only",
                    family_action="blocked",
                    headline="independent blocked",
                    reason_codes=["independent_family_candidate_inactive"],
                    blocking_reasons=["independent_long_book_expected_cost_above_max_acceptable"],
                ),
                StrategyCandidate(
                    family="directional",
                    state="ready",
                    enabled=True,
                    selectable=True,
                    execution_compatible=True,
                    route_action="override_target",
                    family_action="hold_family",
                    headline="directional ready",
                ),
            ],
            sleeve_intents=sleeve_intents,
            allocation_decision=allocation,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(applied.strategy_family, "independent")
        self.assertEqual(applied.strategy_route_action, "advisory_only")
        self.assertEqual(applied.target_position_qty, Decimal("0"))
        self.assertEqual(applied.strategy_execution_legs, [])
        self.assertNotIn("directional_strategy_execution_legs_retained", applied.strategy_reason_codes)

    def test_apply_selected_target_clears_inherited_legs_when_allocation_decision_missing(self) -> None:
        # Defensive path: when snapshot.allocation_decision is None (legacy replay /
        # unit scaffolding), independent hold/advisory must not leak directional legs
        # inherited from base_target.strategy_execution_legs.
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "independent",
                "strategy_family_auto_selection_enabled": False,
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        directional_leg = StrategyLegIntent(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            side="buy",
            position_mode="long_short_mode",
            pos_side="long",
            action="open",
            family="directional",
            role="primary",
            margin_mode="cross",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0.01"),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0.01",
            position_intent="open_long",
        ).model_copy(
            update={
                "strategy_family": "directional",
                "strategy_execution_legs": [directional_leg],
                "strategy_bundle_id": "bundle_prior_directional",
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        snapshot = StrategyCoordinatorSnapshot(
            decision_id=base_target.decision_id,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=("BTC-USDT-SWAP",),
            active_family="independent",
            selected_family="independent",
            selected_state="blocked",
            selected_route_action="advisory_only",
            selected_family_action="blocked",
            selected_headline="independent blocked",
            selection_reason_codes=[
                "legacy_configured_strategy_family_independent_unavailable",
                "legacy_configured_strategy_family_independent_hold_only",
            ],
            active_families=["independent"],
            approved_families=[],
            candidates=[
                StrategyCandidate(
                    family="independent",
                    state="blocked",
                    enabled=True,
                    selectable=False,
                    execution_compatible=False,
                    route_action="advisory_only",
                    family_action="blocked",
                    headline="independent blocked",
                    reason_codes=["independent_family_candidate_inactive"],
                    blocking_reasons=["independent_long_book_expected_cost_above_max_acceptable"],
                ),
            ],
            sleeve_intents=[],
            allocation_decision=None,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(applied.strategy_family, "independent")
        self.assertEqual(applied.strategy_route_action, "advisory_only")
        self.assertEqual(applied.strategy_execution_legs, [])
        self.assertIsNone(applied.strategy_bundle_id)
        self.assertNotIn("directional_strategy_execution_legs_retained", applied.strategy_reason_codes)

    def test_apply_selected_target_reconciles_sizing_breakdown_after_family_override(self) -> None:
        coordinator = StrategyCoordinatorService(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                }
            ),
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100000")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        sizing_breakdown = PositionSizingBreakdown(
            sizing_mode="balance_aware",
            available_equity=Decimal("390"),
            margin_usage_fraction=Decimal("0.75"),
            target_leverage=5.0,
            leverage_bias=1.0,
            last_price=Decimal("100000"),
            default_order_qty=Decimal("0.004"),
            position_scale=Decimal("1"),
            legacy_reference_qty=Decimal("0.004"),
            balance_reference_qty=Decimal("0.014625"),
            resolved_reference_qty=Decimal("0.014625"),
            resolved_target_qty=Decimal("0.014625"),
            budgeted_notional=Decimal("1462.5"),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0.014625",
            position_intent="open_long",
            target_leverage=5.0,
            sizing_breakdown=sizing_breakdown,
        )
        allocation = PortfolioAllocationDecision(
            decision_id=base_target.decision_id,
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            route_action="override_target",
            primary_family="independent",
            primary_strategy_sleeve_id="sleeve_independent_test",
            active_families=["directional", "independent"],
            approved_families=["independent"],
            reason_codes=["allocator_primary_family_independent"],
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0.01"),
            execution_legs=[
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="buy",
                    family="independent",
                    role="primary",
                    strategy_sleeve_id="sleeve_independent_test",
                    allocation_id="alloc_independent_test",
                    margin_mode="cross",
                    target_leverage=2.0,
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("0.01"),
                    delta_position_qty=Decimal("0.01"),
                    reference_price=Decimal("100000"),
                    execution_compatible=True,
                )
            ],
        )
        snapshot = StrategyCoordinatorSnapshot(
            decision_id=base_target.decision_id,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=("BTC-USDT-SWAP",),
            active_family="independent",
            selected_family="independent",
            selected_state="ready",
            selected_route_action="override_target",
            selected_family_action="open_independent_book",
            selected_headline="independent selected",
            selection_reason_codes=["allocator_primary_family_independent"],
            active_families=["directional", "independent"],
            approved_families=["independent"],
            candidates=[
                StrategyCandidate(
                    family="independent",
                    state="ready",
                    enabled=True,
                    selectable=True,
                    execution_compatible=True,
                    route_action="override_target",
                    family_action="open_independent_book",
                    headline="independent selected",
                )
            ],
            allocation_decision=allocation,
        )

        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertIsNotNone(applied.sizing_breakdown)
        self.assertEqual(applied.target_position_qty, Decimal("0.01"))
        self.assertEqual(applied.target_leverage, 2.0)
        self.assertEqual(applied.sizing_breakdown.balance_reference_qty, Decimal("0.01"))
        self.assertEqual(applied.sizing_breakdown.resolved_reference_qty, Decimal("0.01"))
        self.assertEqual(applied.sizing_breakdown.resolved_target_qty, Decimal("0.01"))
        self.assertEqual(applied.sizing_breakdown.budgeted_notional, Decimal("1000"))
        self.assertIsNotNone(applied.decision_outcome)
        self.assertEqual(
            applied.decision_outcome.sizing_breakdown.resolved_target_qty,  # type: ignore[union-attr]
            Decimal("0.01"),
        )

    def test_dca_interval_uses_last_real_dca_target_instead_of_hold_cycles(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "dca",
                "dca_enabled": True,
                "dca_interval_seconds": 3600.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
            }
        )
        event_store = InMemoryEventStore()
        sleeve_id = build_strategy_sleeve_id(
            family="dca",
            primary_symbol="BTC-USDT",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT",),
        )
        prior_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0.5",
            rebalance_reason="dca_strategy",
            position_intent="open_long",
        ).model_copy(
            update={
                "strategy_family": "dca",
                "strategy_sleeve_id": sleeve_id,
                "strategy_route_action": "override_target",
                "strategy_reason_codes": ["dca_interval_elapsed"],
            }
        )
        prior_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=prior_target,
            source_component="test",
        ).model_copy(update={"event_timestamp": utc_now() - timedelta(minutes=10)})
        event_store.append(prior_event)
        gateway = _FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")})
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.5"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0.5",
                target_qty="0.5",
            ),
        )
        selected = next(candidate for candidate in snapshot.candidates if candidate.family == "dca")

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(selected.state, "inactive")
        self.assertIn("dca_interval_not_elapsed", selected.reason_codes)

    def test_dca_interval_uses_target_margin_mode_for_sleeve_history_scope(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "dca",
                "dca_enabled": True,
                "dca_interval_seconds": 3600.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
            }
        )
        event_store = InMemoryEventStore()
        sleeve_id = build_strategy_sleeve_id(
            family="dca",
            primary_symbol="BTC-USDT",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT",),
        )
        prior_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0.5",
            rebalance_reason="dca_strategy",
            position_intent="open_long",
        ).model_copy(
            update={
                "strategy_family": "dca",
                "strategy_sleeve_id": sleeve_id,
                "strategy_route_action": "override_target",
                "strategy_reason_codes": ["dca_interval_elapsed"],
            }
        )
        prior_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=prior_target,
            source_component="test",
        ).model_copy(update={"event_timestamp": utc_now() - timedelta(minutes=10)})
        event_store.append(prior_event)
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.5"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0.5",
                target_qty="0.5",
            ),
        )
        selected = next(candidate for candidate in snapshot.candidates if candidate.family == "dca")

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(selected.state, "inactive")
        self.assertIn("dca_interval_not_elapsed", selected.reason_codes)

    def test_dca_position_cap_uses_target_margin_mode_for_sleeve_inventory_scope(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "dca",
                "dca_enabled": True,
                "dca_interval_seconds": 0.0,
                "dca_quote_budget_per_cycle": 100.0,
                "dca_max_position_fraction_of_limit": 0.5,
                "max_abs_position_qty": 2.0,
            }
        )
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="dca",
            primary_symbol="BTC-USDT",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT",),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_dca_cash_scope",
                symbol="BTC-USDT",
                side="buy",
                qty="1.0",
                price="100",
                product_type="spot",
                margin_mode="cash",
                strategy_family="dca",
                strategy_sleeve_id=sleeve_id,
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="1.0",
            target_qty="1.0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="1.0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        selected = next(candidate for candidate in snapshot.candidates if candidate.family == "dca")

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(selected.state, "inactive")
        self.assertIn("dca_position_cap_reached", selected.reason_codes)
        self.assertEqual(applied.target_position_qty, Decimal("1.0"))

    def test_dca_pullback_only_requires_anchor_history_before_activation(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "dca",
                "dca_enabled": True,
                "dca_interval_seconds": 0.0,
                "dca_quote_budget_per_cycle": 100.0,
                "dca_pullback_only_enabled": True,
                "dca_pullback_entry_bps": 0.0,
                "max_abs_position_qty": 2.0,
            }
        )
        event_store = InMemoryEventStore()
        event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key="BTC-USDT",
                payload_model=_market_snapshot("BTC-USDT", "100"),
                source_component="test",
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0",
                target_qty="0",
            ),
        )
        selected = next(candidate for candidate in snapshot.candidates if candidate.family == "dca")

        self.assertEqual(snapshot.selected_family, "directional")
        self.assertEqual(selected.state, "inactive")
        self.assertEqual(selected.route_action, "hold_current")
        self.assertIn("dca_pullback_anchor_history_insufficient", selected.reason_codes)
        self.assertEqual(selected.metrics["anchor_history_required"], 2)
        self.assertEqual(selected.metrics["anchor_history_available"], 1)

    def test_dca_uses_sleeve_history_and_inventory_truth(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "dca",
                "dca_enabled": True,
                "dca_interval_seconds": 3600.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
            }
        )
        event_store = InMemoryEventStore()
        prior_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0",
            target_qty="0.5",
            rebalance_reason="dca_strategy",
            position_intent="open_long",
        ).model_copy(
            update={
                "strategy_family": "dca",
                "strategy_sleeve_id": "sleeve-other",
                "strategy_route_action": "override_target",
                "strategy_reason_codes": ["dca_interval_elapsed"],
            }
        )
        prior_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=prior_target,
            source_component="test",
        ).model_copy(update={"event_timestamp": utc_now() - timedelta(minutes=10)})
        event_store.append(prior_event)
        gateway = _FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")})
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=InMemoryExecutionRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0.5",
            target_qty="0.5",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.5"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        selected = next(candidate for candidate in snapshot.candidates if candidate.family == "dca")
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "dca")
        self.assertEqual(selected.metrics["current_account_position_qty"], Decimal("0.5"))
        self.assertEqual(selected.metrics["current_sleeve_position_qty"], Decimal("0"))
        self.assertNotIn("dca_interval_not_elapsed", selected.reason_codes)
        self.assertEqual(selected.target_position_qty, Decimal("1.5"))
        self.assertEqual(applied.target_position_qty, Decimal("1.5"))

    def test_allocator_combines_spot_grid_and_dca_sleeve_deltas(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.05,
                "dca_enabled": True,
                "dca_interval_seconds": 0.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
            }
        )
        event_store = InMemoryEventStore()
        gateway = _FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "99")})
        for price in ("100", "101", "100", "99"):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", price),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0.3",
            target_qty="0.3",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.3"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "spot_grid")
        self.assertEqual(snapshot.allocation_decision.approved_families, ["spot_grid", "dca"])
        self.assertEqual(len(snapshot.sleeve_intents), 5)
        self.assertNotIn("protective", {intent.family for intent in snapshot.sleeve_intents})
        self.assertNotIn("opportunistic", {intent.family for intent in snapshot.sleeve_intents})
        self.assertEqual(len(applied.strategy_execution_legs), 2)
        self.assertEqual({leg.family for leg in applied.strategy_execution_legs}, {"spot_grid", "dca"})
        self.assertEqual(applied.strategy_route_action, "override_target")
        self.assertIn("spot_grid", applied.source_mix)
        self.assertIn("dca", applied.source_mix)
        combined_delta = sum((leg.delta_position_qty for leg in applied.strategy_execution_legs), start=Decimal("0"))
        self.assertEqual(applied.delta_position_qty, combined_delta)

    def test_allocator_redistributes_portfolio_budget_when_total_open_notional_is_capped(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "strategy_family_active": "spot_grid",
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.05,
                "dca_enabled": True,
                "dca_interval_seconds": 0.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
                "max_total_open_notional": 50.0,
            }
        )
        event_store = InMemoryEventStore()
        gateway = _FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "99")})
        for price in ("100", "101", "100", "99"):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", price),
                    source_component="test",
                )
            )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.3"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0.3",
                target_qty="0.3",
            ),
        )
        allocation = snapshot.allocation_decision

        self.assertIsNotNone(allocation)
        self.assertEqual(allocation.allocator_version, "task74_allocator_v2_phase2")
        self.assertIn("allocator_portfolio_max_total_open_notional_capped", allocation.budget_cut_reason_codes)
        self.assertGreater(allocation.portfolio_requested_notional, allocation.portfolio_approved_notional)
        self.assertGreater(allocation.portfolio_budget_cut_notional, Decimal("0"))
        self.assertEqual(allocation.portfolio_risk_budget_state, "redistributed")
        self.assertTrue(allocation.budget_snapshots)
        self.assertTrue(allocation.budget_snapshot_ids)

    def test_smart_arbitrage_positive_basis_builds_executable_pair(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        budget_snapshot = next(item for item in snapshot.allocation_decision.budget_snapshots if item.family == "smart_arbitrage")

        self.assertEqual(snapshot.selected_family, "smart_arbitrage")
        self.assertEqual(snapshot.selected_route_action, "override_target")
        self.assertEqual(budget_snapshot.requested_notional, Decimal("100"))
        self.assertEqual(budget_snapshot.approved_notional, Decimal("100"))
        self.assertEqual(applied.strategy_family, "smart_arbitrage")
        self.assertIsNotNone(applied.strategy_sleeve_id)
        self.assertIsNotNone(applied.allocation_id)
        self.assertIsNotNone(applied.strategy_bundle_id)
        self.assertEqual(len(applied.strategy_execution_legs), 2)
        self.assertEqual({item.product_type for item in applied.strategy_execution_legs}, {"spot", "derivatives"})
        self.assertEqual(
            {item.strategy_sleeve_id for item in applied.strategy_execution_legs},
            {applied.strategy_sleeve_id},
        )
        self.assertEqual(
            {item.allocation_id for item in applied.strategy_execution_legs},
            {applied.allocation_id},
        )

    def test_smart_arbitrage_negative_basis_stays_advisory_without_executable_pair(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
            }
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "advisory_only")
        self.assertEqual(candidate.route_action, "advisory_only")
        self.assertFalse(candidate.execution_compatible)
        self.assertFalse(candidate.legs)
        self.assertIn("smart_arbitrage_negative_basis", candidate.reason_codes)
        self.assertIn("smart_arbitrage_spot_short_not_supported", candidate.reason_codes)

    def test_smart_arbitrage_negative_basis_inventory_backed_builds_executable_pair(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "inventory_backed",
                "smart_arbitrage_inventory_reservation_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[
                ExchangeBalance(currency="BTC", total=Decimal("2"), available=Decimal("2")),
                ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000")),
            ],
            positions=[],
            account_mode="cross",
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(candidate.state, "opening")
        self.assertEqual(candidate.execution_mode, "inventory_reverse_carry")
        self.assertEqual(candidate.state_phase, "opening")
        self.assertIn("smart_arbitrage_inventory_backed_ready", candidate.reason_codes)
        self.assertEqual(len(candidate.legs), 2)
        self.assertEqual(candidate.legs[0].margin_mode, "cash")
        self.assertEqual(applied.strategy_execution_mode, "inventory_reverse_carry")
        self.assertEqual(len(applied.strategy_execution_legs), 2)

    def test_smart_arbitrage_negative_basis_margin_backed_builds_executable_pair_when_ready(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
            positions=[],
            account_mode="cross",
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(candidate.state, "opening")
        self.assertEqual(candidate.execution_mode, "margin_reverse_carry")
        self.assertIn("smart_arbitrage_margin_short_ready", candidate.reason_codes)
        self.assertEqual(len(candidate.legs), 2)
        self.assertEqual(candidate.legs[0].margin_mode, "cross")
        self.assertEqual(applied.strategy_execution_mode, "margin_reverse_carry")
        self.assertEqual(applied.strategy_execution_legs[0].margin_mode, "cross")

    def test_smart_arbitrage_negative_basis_margin_backed_does_not_switch_to_inventory_mode(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_inventory_reservation_enabled": True,
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[
                ExchangeBalance(currency="BTC", total=Decimal("2"), available=Decimal("2")),
                ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000")),
            ],
            positions=[],
            account_mode="cross",
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "opening")
        self.assertEqual(candidate.execution_mode, "margin_reverse_carry")
        self.assertIn("smart_arbitrage_margin_short_ready", candidate.reason_codes)
        self.assertNotIn("smart_arbitrage_inventory_backed_ready", candidate.reason_codes)
        self.assertEqual(candidate.legs[0].margin_mode, "cross")

    def test_snapshot_margin_mode_prefers_directional_target_runtime_value(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "66000")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP"),
            directional_target=_position_target(
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="isolated",
                current_qty="0",
                target_qty="0",
            ),
        )

        self.assertEqual(snapshot.margin_mode, "isolated")

    def test_smart_arbitrage_negative_basis_inventory_backed_does_not_fall_back_to_margin_mode(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "inventory_backed",
                "smart_arbitrage_inventory_reservation_enabled": True,
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
            positions=[],
            account_mode="cross",
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(candidate.route_action, "advisory_only")
        self.assertEqual(candidate.execution_mode, "inventory_reverse_carry")
        self.assertIn("smart_arbitrage_inventory_backed_spot_balance_unavailable", candidate.reason_codes)
        self.assertNotIn("smart_arbitrage_margin_short_ready", candidate.reason_codes)
        self.assertFalse(candidate.legs)

    def test_smart_arbitrage_uses_derived_primary_pair_when_registry_is_empty(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "opening")
        self.assertEqual(candidate.pair_id, "btc_usdt__btc_usdt_swap")
        self.assertEqual(candidate.execution_mode, "spot_carry")
        self.assertEqual(len(candidate.legs), 2)

    def test_smart_arbitrage_selects_highest_scoring_pair_from_registry(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_quarterly",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-260626",
                    },
                ),
            }
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100.8"),
                "BTC-USDT-260626": _market_snapshot("BTC-USDT-260626", "102"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")
        evaluated_pairs = candidate.metrics["evaluated_pairs"]

        self.assertEqual(candidate.pair_id, "btc_quarterly")
        self.assertEqual(candidate.execution_mode, "spot_carry")
        self.assertGreaterEqual(len(evaluated_pairs), 2)
        self.assertTrue(any(item["pair_id"] == "btc_quarterly" for item in evaluated_pairs))

    def test_smart_arbitrage_active_margin_reverse_carry_uses_margin_scoped_spot_inventory(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "smart_arbitrage",
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_basis_exit_bps": 5.0,
            }
        )
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_margin_spot_short",
                symbol="BTC-USDT",
                side="sell",
                qty="0.5",
                price="100",
                product_type="spot",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="primary",
                position_intent="open_short",
            )
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_margin_hedge_long",
                symbol="BTC-USDT-SWAP",
                side="buy",
                qty="0.5",
                price="99",
                product_type="derivatives",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="hedge",
                position_intent="open_long",
            )
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    quantity=Decimal("0.5"),
                    side="short",
                    margin_mode="cross",
                ),
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.5"),
                    side="long",
                    margin_mode="cross",
                ),
            ],
            account_mode="cross",
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.5",
            target_qty="0.5",
            position_intent="hold",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0.5"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(candidate.state_phase, "active")
        self.assertEqual(candidate.execution_mode, "margin_reverse_carry")
        self.assertEqual(candidate.metrics["current_account_spot_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.metrics["current_sleeve_spot_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.metrics["current_sleeve_cash_spot_qty"], Decimal("0"))
        self.assertEqual(candidate.metrics["current_sleeve_margin_spot_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.delta_position_qty, Decimal("0"))
        self.assertEqual(applied.strategy_family, "smart_arbitrage")
        self.assertEqual(len(applied.strategy_execution_legs), 0)

    def test_smart_arbitrage_uses_target_margin_scope_for_sleeve_inventory_identity(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "smart_arbitrage",
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "isolated",
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_basis_exit_bps": 5.0,
            }
        )
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="isolated",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_isolated_spot_short",
                symbol="BTC-USDT",
                side="sell",
                qty="0.5",
                price="100",
                product_type="spot",
                margin_mode="isolated",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="primary",
                position_intent="open_short",
            )
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_isolated_hedge_long",
                symbol="BTC-USDT-SWAP",
                side="buy",
                qty="0.5",
                price="99",
                product_type="derivatives",
                margin_mode="isolated",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="hedge",
                position_intent="open_long",
            )
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT",
                    symbol="BTC-USDT",
                    quantity=Decimal("0.5"),
                    side="short",
                    margin_mode="isolated",
                ),
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.5"),
                    side="long",
                    margin_mode="isolated",
                ),
            ],
            account_mode="isolated",
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="isolated",
            current_qty="0.5",
            target_qty="0.5",
            position_intent="hold",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0.5"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state_phase, "active")
        self.assertEqual(candidate.execution_mode, "margin_reverse_carry")
        self.assertEqual(candidate.metrics["current_sleeve_spot_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.metrics["current_sleeve_margin_spot_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.metrics["current_sleeve_derivatives_qty"], Decimal("0.5"))
        self.assertEqual(candidate.metrics["foreign_spot_qty"], Decimal("0"))
        self.assertEqual(candidate.metrics["foreign_derivatives_qty"], Decimal("0"))
        self.assertEqual(candidate.delta_position_qty, Decimal("0"))

    def test_smart_arbitrage_can_select_multiple_pairs_up_to_configured_limit(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
                "smart_arbitrage_max_concurrent_pairs": 2,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "eth_usdt_swap",
                        "spot_symbol": "ETH-USDT",
                        "hedge_symbol": "ETH-USDT-SWAP",
                    },
                ),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
                    "ETH-USDT": _market_snapshot("ETH-USDT", "200"),
                    "ETH-USDT-SWAP": _market_snapshot("ETH-USDT-SWAP", "203"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)
        sleeve_intent = next(item for item in snapshot.sleeve_intents if item.family == "smart_arbitrage")
        budget_assignment = next(
            item for item in snapshot.allocation_decision.budget_assignments if item.family == "smart_arbitrage"
        )
        budget_snapshot = next(item for item in snapshot.allocation_decision.budget_snapshots if item.family == "smart_arbitrage")

        self.assertEqual(candidate.pair_id, "multi_pair")
        self.assertTrue(candidate.metrics["aggregate_candidate"])
        self.assertEqual(candidate.metrics["pair_count_selected"], 2)
        self.assertEqual(len(candidate.legs), 4)
        self.assertIsNone(candidate.target_position_qty)
        self.assertIsNone(candidate.delta_position_qty)
        self.assertEqual({leg.symbol for leg in candidate.legs}, {"BTC-USDT", "BTC-USDT-SWAP", "ETH-USDT", "ETH-USDT-SWAP"})
        self.assertEqual(sleeve_intent.symbol, "BTC-USDT-SWAP")
        self.assertIsNone(sleeve_intent.target_notional)
        self.assertEqual(budget_assignment.effective_quote_budget_limit, Decimal("200"))
        self.assertEqual(budget_assignment.effective_notional_cap, Decimal("200"))
        self.assertEqual(budget_snapshot.requested_notional, Decimal("202.5"))
        self.assertEqual(budget_snapshot.approved_notional, Decimal("200"))
        self.assertEqual(len(applied.strategy_execution_legs), 4)

    def test_smart_arbitrage_does_not_parallel_open_pairs_with_overlapping_symbol_scope(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
                "smart_arbitrage_max_concurrent_pairs": 2,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_quarterly",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-260626",
                    },
                ),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
                    "BTC-USDT-260626": _market_snapshot("BTC-USDT-260626", "102"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.metrics["pair_count_selected"], 1)
        self.assertEqual(len(candidate.legs), 2)
        self.assertIn(candidate.pair_id, {"btc_quarterly", "btc_usdt__btc_usdt_swap"})

    def test_smart_arbitrage_blocks_opening_when_pair_disallows_margin_reverse_carry(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                        "execution_modes": ("spot_carry", "inventory_reverse_carry"),
                    },
                ),
            }
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
            positions=[],
            account_mode="cross",
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "101"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(candidate.route_action, "advisory_only")
        self.assertFalse(candidate.legs)
        self.assertIn("smart_arbitrage_margin_reverse_carry_not_allowed", candidate.reason_codes)

    def test_smart_arbitrage_blocks_opening_when_pair_disallows_spot_carry(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                        "execution_modes": ("inventory_reverse_carry",),
                    },
                ),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "blocked")
        self.assertEqual(candidate.route_action, "advisory_only")
        self.assertFalse(candidate.legs)
        self.assertIn("smart_arbitrage_spot_carry_not_allowed", candidate.reason_codes)

    def test_smart_arbitrage_blocks_opening_when_execution_modes_config_is_invalid(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                        "execution_modes": ("spotcarry_typo",),
                    },
                ),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state, "blocked")
        self.assertIn("smart_arbitrage_spot_carry_not_allowed", candidate.reason_codes)
        self.assertIn("smart_arbitrage_pair_execution_modes_invalid", candidate.metrics["pair_configuration_error_codes"])

    def test_smart_arbitrage_keeps_existing_positive_pair_actionable_when_config_now_disallows_opening(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "smart_arbitrage",
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_basis_exit_bps": 5.0,
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                        "execution_modes": ("inventory_reverse_carry",),
                    },
                ),
            }
        )
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_positive_spot_long",
                symbol="BTC-USDT",
                side="buy",
                qty="0.5",
                price="100",
                product_type="spot",
                margin_mode="cash",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="inventory",
                position_intent="open_long",
            )
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_positive_hedge_short",
                symbol="BTC-USDT-SWAP",
                side="sell",
                qty="0.5",
                price="101",
                product_type="derivatives",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="hedge",
                position_intent="open_short",
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway(
                {
                    "BTC-USDT": _market_snapshot("BTC-USDT", "100.5"),
                    "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
                }
            ),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="-0.5",
            target_qty="-0.5",
            position_intent="hold",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="-0.5"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")

        self.assertEqual(candidate.state_phase, "active")
        self.assertEqual(candidate.execution_mode, "spot_carry")
        self.assertEqual(candidate.route_action, "hold_current")
        self.assertIn("smart_arbitrage_existing_pair_mode_not_allowed_by_config", candidate.reason_codes)
        self.assertEqual(len(candidate.legs), 2)
        self.assertTrue(all(abs(to_decimal(leg.delta_position_qty or Decimal("0"))) <= EPSILON_DECIMAL_12 for leg in candidate.legs))

    def test_allocator_blocks_directional_derivatives_target_while_arbitrage_pair_is_active(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 1.0,
                "smart_arbitrage_quote_budget_per_trade": 100.0,
                "smart_arbitrage_max_pair_notional": 100.0,
            }
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "101"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0.03",
            rebalance_reason="directional_entry",
            position_intent="open_long",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "smart_arbitrage")
        self.assertEqual(snapshot.allocation_decision.approved_families, ["smart_arbitrage"])
        self.assertIn(
            "allocator_directional_blocked_by_active_smart_arbitrage",
            snapshot.allocation_decision.blocked_reason_codes,
        )
        self.assertEqual(applied.strategy_family, "smart_arbitrage")
        self.assertEqual(len(applied.strategy_execution_legs), 2)

    def test_auto_parallel_pauses_dca_after_hard_loss_without_inventory(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "dca_enabled": True,
                "dca_interval_seconds": 0.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
                "strategy_sleeve_auto_soft_loss_usdt": 10.0,
                "strategy_sleeve_auto_hard_loss_usdt": 20.0,
            }
        )
        sleeve_id = build_strategy_sleeve_id(
            family="dca",
            primary_symbol="BTC-USDT",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT",),
        )
        sleeve_pnl_repo = InMemorySleevePnLRepository()
        sleeve_pnl_repo.save_record(
            SleevePnLRecord(
                record_id="sleeve_pnl_dca_loss",
                strategy_sleeve_id=sleeve_id,
                strategy_family="dca",
                allocation_id="alloc_dca_loss",
                symbol="BTC-USDT",
                event_type="fill_realization",
                fill_id="fill_dca_loss",
                realized_pnl=Decimal("-22"),
                fee_amount=Decimal("0"),
                funding_fee_amount=Decimal("0"),
                inventory_move_qty=Decimal("0"),
                attribution_type="direct_fill",
                product_type="spot",
                margin_mode="cash",
                event_timestamp=utc_now(),
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
            sleeve_pnl_repo=sleeve_pnl_repo,
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0",
                target_qty="0",
            ),
        )

        dca_candidate = next(item for item in snapshot.candidates if item.family == "dca")
        dca_control = next(item for item in snapshot.automation_decisions if item.family == "dca")
        self.assertEqual(dca_control.automation_state, "contracted")
        self.assertTrue(dca_control.automatic_enabled)
        self.assertTrue(dca_control.approved_for_execution)
        self.assertTrue(dca_control.budget_zero_suppressed)
        self.assertEqual(dca_control.execution_control_mode, "budget_zero_suppressed")
        self.assertEqual(dca_control.execution_behavior, "suppressed_after_approval")
        self.assertIn("hard_loss_budget_block", dca_control.budget_reason_codes)
        self.assertIn("approved_but_budget_zero_suppressed", dca_control.composition_reason_codes)
        self.assertEqual(snapshot.allocation_decision.operator_summary, "当前 allocator v2 识别到已批准但被预算压零的 sleeve；本轮没有新的可执行 delta。")
        self.assertIn("allocator_sleeve_suppressed_after_approval", snapshot.allocation_decision.blocked_reason_codes)
        self.assertEqual(dca_candidate.route_action, "advisory_only")
        self.assertFalse(dca_candidate.selectable)
        self.assertEqual(snapshot.selected_family, "directional")

    def test_auto_parallel_contracts_inventory_sleeves_under_reconciliation_pressure(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.05,
                "max_abs_position_qty": 1.0,
                "strategy_sleeve_auto_reconciliation_contraction_multiplier": 0.4,
            }
        )
        event_store = InMemoryEventStore()
        for price in ("100", "101", "100", "99"):
            event_store.append(
                build_envelope(
                    topic=topics.MARKET_SNAPSHOTS,
                    key="BTC-USDT",
                    payload_model=_market_snapshot("BTC-USDT", price),
                    source_component="test",
                )
            )
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="spot_grid",
            primary_symbol="BTC-USDT",
            product_scope="spot",
            margin_scope="cash",
            symbol_scope=("BTC-USDT",),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_grid_inventory",
                symbol="BTC-USDT",
                side="buy",
                qty="0.2",
                price="100",
                product_type="spot",
                margin_mode="cash",
                strategy_family="spot_grid",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="inventory",
            )
        )
        reconciliation_repo = InMemoryReconciliationRepository()
        reconciliation_repo.save_report(
            ReconciliationReport(
                reconciliation_id="recon_contract_grid",
                as_of_ts=utc_now(),
                product_type="spot",
                margin_mode="cash",
                allowed_symbols=["BTC-USDT"],
                order_diff={},
                fill_diff={},
                balance_diff={},
                position_diff={},
                severity="WARNING",
                only_reduce_required=True,
                only_reduce_reasons=["exchange_snapshot_stale"],
            )
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=event_store,
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "99")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
            reconciliation_repo=reconciliation_repo,
        )
        base_target = _position_target(
            symbol="BTC-USDT",
            product_type="spot",
            margin_mode="cash",
            current_qty="0.2",
            target_qty="0.2",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0.2"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=base_target,
        )
        grid_control = next(item for item in snapshot.automation_decisions if item.family == "spot_grid")
        grid_candidate = next(item for item in snapshot.candidates if item.family == "spot_grid")

        self.assertEqual(grid_control.automation_state, "contracted")
        self.assertLess(grid_control.budget_multiplier, Decimal("1"))
        self.assertEqual(grid_control.permission_mode, "approved")
        self.assertIn("reconciliation_contraction_active", grid_control.budget_reason_codes)
        self.assertEqual(grid_candidate.metrics["auto_legacy_automation_state"], "contracted")
        self.assertEqual(grid_candidate.metrics["auto_budget_multiplier"], Decimal("0.4"))

    def test_auto_parallel_distinguishes_permission_denied_from_budget_zero_suppression(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "dca_enabled": True,
                "dca_interval_seconds": 0.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
                "strategy_sleeve_auto_execution_enabled": False,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT": _market_snapshot("BTC-USDT", "100")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT", product_type="spot", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT", regime="range"),
            directional_target=_position_target(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                current_qty="0",
                target_qty="0",
            ),
        )

        dca_control = next(item for item in snapshot.automation_decisions if item.family == "dca")
        dca_intent = next(item for item in snapshot.sleeve_intents if item.family == "dca")

        self.assertFalse(dca_control.approved_for_execution)
        self.assertEqual(dca_control.permission_mode, "advisory_only")
        self.assertEqual(dca_control.execution_control_mode, "permission_denied")
        self.assertEqual(dca_control.execution_behavior, "advisory_only")
        self.assertFalse(dca_control.budget_zero_suppressed)
        self.assertIn("auto_execution_disabled_by_profile", dca_control.permission_reason_codes)
        self.assertEqual(dca_intent.route_action, "advisory_only")
        self.assertEqual(dca_intent.execution_control_mode, "permission_denied")
        self.assertEqual(dca_intent.execution_behavior, "advisory_only")
        self.assertEqual(dca_intent.control_trace["execution_control_mode"], "permission_denied")
        self.assertEqual(dca_intent.control_trace["execution_behavior"], "advisory_only")
        self.assertEqual(
            dca_intent.control_trace["composition"]["execution_control_mode"],
            "permission_denied",
        )
        self.assertEqual(
            dca_intent.control_trace["composition"]["execution_behavior"],
            "advisory_only",
        )
        self.assertEqual(dca_intent.control_trace["permission"]["approved_for_execution"], False)
        self.assertEqual(dca_intent.control_trace["budget"]["budget_zero_suppressed"], False)

    def test_smart_arbitrage_uses_sleeve_inventory_truth_when_unwinding_pair(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "smart_arbitrage",
                "smart_arbitrage_enabled": True,
                "smart_arbitrage_basis_entry_bps": 5.0,
                "smart_arbitrage_basis_exit_bps": 5.0,
                "smart_arbitrage_estimated_cost_bps": 0.0,
            }
        )
        execution_repo = InMemoryExecutionRepository()
        sleeve_id = build_strategy_sleeve_id(
            family="smart_arbitrage",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT", "BTC-USDT-SWAP"),
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_arb_spot",
                symbol="BTC-USDT",
                side="buy",
                qty="0.5",
                price="100",
                product_type="spot",
                margin_mode="cash",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="primary",
            )
        )
        execution_repo.save_fill(
            _fill_event(
                fill_id="fill_arb_hedge",
                symbol="BTC-USDT-SWAP",
                side="sell",
                qty="0.5",
                price="101",
                product_type="derivatives",
                margin_mode="cross",
                strategy_family="smart_arbitrage",
                strategy_sleeve_id=sleeve_id,
                strategy_leg_role="hedge",
                position_intent="open_short",
            )
        )
        account_snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[
                ExchangeBalance(currency="BTC", total=Decimal("0.7"), available=Decimal("0.7")),
                ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000")),
            ],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.5"),
                    side="short",
                )
            ],
            account_mode="cross",
        )
        gateway = _FakeMarketGateway(
            {
                "BTC-USDT": _market_snapshot("BTC-USDT", "100"),
                "BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "100.01"),
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=gateway,
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=execution_repo,
            account_service=_StaticAccountService(account_snapshot),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="-0.5",
            target_qty="-0.5",
            position_intent="hold",
        )

        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="-0.5"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=base_target,
        )
        candidate = next(item for item in snapshot.candidates if item.family == "smart_arbitrage")
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "smart_arbitrage")
        self.assertEqual(candidate.metrics["current_account_spot_qty"], Decimal("0.7"))
        self.assertEqual(candidate.metrics["current_sleeve_spot_qty"], Decimal("0.5"))
        self.assertEqual(candidate.metrics["foreign_spot_qty"], Decimal("0.2"))
        self.assertEqual(candidate.metrics["current_account_derivatives_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.metrics["current_sleeve_derivatives_qty"], Decimal("-0.5"))
        self.assertEqual(candidate.metrics["target_account_spot_qty"], Decimal("0.2"))
        self.assertEqual(candidate.metrics["target_account_derivatives_qty"], Decimal("0"))
        spot_leg = next(item for item in applied.strategy_execution_legs if item.product_type == "spot")
        hedge_leg = next(item for item in applied.strategy_execution_legs if item.product_type == "derivatives")
        self.assertEqual(spot_leg.current_position_qty, Decimal("0.7"))
        self.assertEqual(spot_leg.target_position_qty, Decimal("0.2"))
        self.assertEqual(spot_leg.delta_position_qty, Decimal("-0.5"))
        self.assertEqual(hedge_leg.current_position_qty, Decimal("-0.5"))
        self.assertEqual(hedge_leg.target_position_qty, Decimal("0"))
        self.assertEqual(hedge_leg.delta_position_qty, Decimal("0.5"))

    def test_registry_active_families_exclude_retired_overlay_families(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "directional",
                "strategy_family_auto_selection_enabled": False,
                "strategy_family_independent_enabled": False,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "65000")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        snapshot = coordinator.evaluate(
            context=_decision_context(symbol="BTC-USDT-SWAP", product_type="derivatives", current_position_qty="0"),
            baseline=_baseline(symbol="BTC-USDT-SWAP", regime="trend"),
            directional_target=_position_target(
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                current_qty="0",
                target_qty="0",
                position_intent="hold",
            ),
        )

        families = {item.family: item for item in snapshot.candidates}
        self.assertNotIn("protective", families)
        self.assertNotIn("opportunistic", families)
        self.assertIn("independent", families)
        self.assertEqual(families["independent"].state, "disabled")
        self.assertIn("strategy_family_independent_disabled", families["independent"].reason_codes)

    def test_retired_overlay_modes_are_rejected_by_runtime_settings(self) -> None:
        for mode in ("protective", "opportunistic"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    AATSSettings.model_validate({"strategy_hedge_overlay_mode": mode})

    def test_independent_family_engine_emits_real_business_candidate_when_enabled(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "directional",
                "strategy_family_auto_selection_enabled": False,
                "strategy_short_bias_enabled": True,
                "strategy_hedge_overlay_enabled": True,
                "strategy_hedge_overlay_mode": "independent",
                "strategy_hedge_independent_enabled": True,
                "strategy_hedge_independent_long_entry_threshold": 0.60,
                "strategy_hedge_independent_short_entry_threshold": 0.60,
                "strategy_hedge_independent_long_close_threshold": 0.48,
                "strategy_hedge_independent_short_close_threshold": 0.48,
                "strategy_hedge_independent_long_scale_in_threshold": 0.72,
                "strategy_hedge_independent_short_scale_in_threshold": 0.72,
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": False,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "65000")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        context = _decision_context(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            current_position_qty="0",
        ).model_copy(
            update={
                "current_long_position_qty": Decimal("0"),
                "current_short_position_qty": Decimal("0"),
                "current_net_position_qty": Decimal("0"),
                "current_gross_position_qty": Decimal("0"),
                "current_exposure_side": "flat",
            }
        )
        baseline = _baseline(symbol="BTC-USDT-SWAP", regime="trend", confidence=1.0).model_copy(
            update={
                "direction_bias": "long",
                "volatility_state": "high",
                "composite_alpha_score": 1.0,
                "factor_scores": {
                    "microstructure_alpha": 0.8,
                    "momentum_alpha": 0.8,
                    "trend_alpha": 0.8,
                },
            }
        )
        snapshot = coordinator.evaluate(
            context=context,
            baseline=baseline,
            directional_target=_position_target(
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                current_qty="0",
                target_qty="0",
                position_intent="hold",
            ).model_copy(
                update={
                    "expected_signal_edge_bps": 18.0,
                    "expected_cost_bps": 4.0,
                    "expected_net_edge_bps": 14.0,
                }
            ),
        )

        self.assertEqual(snapshot.selected_family, "directional")
        independent = next(item for item in snapshot.candidates if item.family == "independent")
        self.assertEqual(independent.execution_mode, "independent_books")
        self.assertFalse(independent.selectable)
        self.assertTrue(independent.execution_compatible)
        self.assertNotIn("strategy_family_independent_placeholder_not_migrated", independent.reason_codes)
        self.assertFalse(bool(independent.metrics.get("skeleton_mode")))
        self.assertEqual(independent.metrics["execution_owner"], "independent")
        self.assertNotIn("legacy_execution_owner", independent.metrics)
        self.assertEqual(independent.state, "opening")
        self.assertEqual(independent.legs[0].family, "independent")
        self.assertEqual(independent.legs[0].pos_side, "long")
        self.assertEqual(independent.legs[0].action, "open")

    def test_independent_family_cutover_selects_independent_and_updates_top_level_semantics(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "derivatives_position_mode": "hedge",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "strategy_family_active": "directional",
                "strategy_family_auto_selection_enabled": False,
                "strategy_short_bias_enabled": True,
                "strategy_hedge_overlay_enabled": True,
                "strategy_hedge_overlay_mode": "independent",
                "strategy_hedge_independent_enabled": True,
                "strategy_hedge_independent_long_entry_threshold": 0.60,
                "strategy_hedge_independent_short_entry_threshold": 0.60,
                "strategy_hedge_independent_long_close_threshold": 0.48,
                "strategy_hedge_independent_short_close_threshold": 0.48,
                "strategy_hedge_independent_long_scale_in_threshold": 0.72,
                "strategy_hedge_independent_short_scale_in_threshold": 0.72,
                "strategy_family_independent_enabled": True,
                "strategy_family_independent_live_execution_enabled": True,
            }
        )
        coordinator = StrategyCoordinatorService(
            settings=settings,
            event_store=InMemoryEventStore(),
            market_gateway=_FakeMarketGateway({"BTC-USDT-SWAP": _market_snapshot("BTC-USDT-SWAP", "65000")}),
            portfolio_repo=InMemoryPortfolioRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
        )
        context = _decision_context(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            current_position_qty="0",
        ).model_copy(
            update={
                "current_long_position_qty": Decimal("0"),
                "current_short_position_qty": Decimal("0"),
                "current_net_position_qty": Decimal("0"),
                "current_gross_position_qty": Decimal("0"),
                "current_exposure_side": "flat",
            }
        )
        baseline = _baseline(symbol="BTC-USDT-SWAP", regime="trend", confidence=1.0).model_copy(
            update={
                "direction_bias": "long",
                "volatility_state": "high",
                "composite_alpha_score": 1.0,
                "factor_scores": {
                    "microstructure_alpha": 0.8,
                    "momentum_alpha": 0.8,
                    "trend_alpha": 0.8,
                },
            }
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0",
            target_qty="0",
            position_intent="hold",
        ).model_copy(
            update={
                "expected_signal_edge_bps": 18.0,
                "expected_cost_bps": 4.0,
                "expected_net_edge_bps": 14.0,
            }
        )

        snapshot = coordinator.evaluate(
            context=context,
            baseline=baseline,
            directional_target=base_target,
        )
        applied = coordinator.apply_selected_target(base_target=base_target, snapshot=snapshot)

        self.assertEqual(snapshot.selected_family, "independent")
        self.assertEqual(snapshot.selected_family_action, "open_independent_book")
        self.assertIn("strategy_family_independent_live_cutover", snapshot.selection_reason_codes)
        self.assertEqual(snapshot.approved_families, ["independent"])
        self.assertEqual(applied.strategy_family, "independent")
        self.assertEqual(applied.strategy_family_action, "open_independent_book")
        self.assertEqual(applied.strategy_route_action, "override_target")
        self.assertEqual(applied.position_intent, "open_long")
        self.assertTrue(applied.strategy_execution_legs)
        self.assertEqual(applied.strategy_execution_legs[0].family, "independent")
        self.assertTrue(applied.strategy_execution_legs[0].execution_chain_id)
        self.assertIsNotNone(applied.family_execution_summary)
        self.assertEqual(applied.family_execution_summary.summary_mode, "single_leg")
        self.assertEqual(applied.family_execution_summary.position_intents, ["open_long"])
        self.assertEqual(applied.family_execution_summary.directions, ["long"])
        self.assertIsNotNone(applied.family_execution_summary.book_expectancy_summary)
        self.assertEqual(applied.family_execution_summary.book_expectancy_summary.source, "independent_book")
        self.assertTrue(applied.family_execution_summary.diagnostic_metric_flags["emit_expected_vs_realized_metrics"])
        self.assertEqual(
            [item.leg for item in applied.family_execution_summary.book_runtime_states],
            ["long", "short"],
        )
        self.assertIsNotNone(applied.book_expectancy_summary)
        self.assertEqual(applied.book_expectancy_summary.source, "independent_book")
        self.assertEqual([item.leg for item in applied.book_runtime_states], ["long", "short"])
        self.assertTrue(applied.book_runtime_states[0].execution_chain_id)
        self.assertTrue(applied.diagnostic_metric_flags["emit_expected_vs_realized_metrics"])
        assert applied.decision_outcome is not None
        self.assertEqual(applied.decision_outcome.selected_strategy_family, "independent")
        self.assertEqual(applied.decision_outcome.selected_strategy_family_action, "open_independent_book")
        self.assertEqual(applied.decision_outcome.final_action, "enter")
        self.assertIsNotNone(applied.decision_outcome.family_execution_summary)
        self.assertEqual(applied.decision_outcome.family_execution_summary.position_intents, ["open_long"])
        self.assertIsNotNone(applied.decision_outcome.family_execution_summary.book_expectancy_summary)
        self.assertIsNotNone(applied.decision_outcome.book_expectancy_summary)
        self.assertEqual(applied.decision_outcome.book_expectancy_summary.source, "independent_book")
        self.assertTrue(applied.decision_outcome.diagnostic_metric_flags["emit_expected_vs_realized_metrics"])
        self.assertEqual(
            [item.leg for item in applied.decision_outcome.book_runtime_states],
            ["long", "short"],
        )
        self.assertTrue(applied.decision_outcome.book_runtime_states[0].execution_chain_id)

    def test_family_execution_summary_preserves_multi_leg_cutover_without_forcing_single_intent(self) -> None:
        summary = StrategyCoordinatorService._family_execution_summary(
            selected_family="independent",
            family_action="open_independent_book",
            route_action="override_target",
            strategy_execution_legs=[
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="buy",
                    position_mode="long_short_mode",
                    pos_side="long",
                    action="open",
                    family="independent",
                    role="primary",
                    strategy_sleeve_id="independent_long",
                    allocation_id="alloc_independent",
                    margin_mode="cross",
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("0.01"),
                    delta_position_qty=Decimal("0.01"),
                    execution_mode="independent_long_book",
                ),
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="sell",
                    position_mode="long_short_mode",
                    pos_side="short",
                    action="open",
                    family="independent",
                    role="primary",
                    strategy_sleeve_id="independent_short",
                    allocation_id="alloc_independent",
                    margin_mode="cross",
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("-0.01"),
                    delta_position_qty=Decimal("-0.01"),
                    execution_mode="independent_short_book",
                ),
            ],
        )

        assert summary is not None
        self.assertEqual(summary.summary_mode, "multi_leg")
        self.assertEqual(summary.leg_count, 2)
        self.assertEqual(summary.position_intents, ["open_long", "open_short"])
        self.assertEqual(summary.directions, ["long", "short"])
        self.assertEqual(summary.leg_actions, ["open"])
        self.assertEqual(summary.execution_modes, ["independent_long_book", "independent_short_book"])

    def test_family_execution_summary_preserves_overlay_parent_signal_fields(self) -> None:
        summary = StrategyCoordinatorService._family_execution_summary(
            selected_family="protective",
            family_action="protect",
            route_action="override_target",
            strategy_execution_legs=[
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="sell",
                    position_mode="long_short_mode",
                    pos_side="short",
                    action="open",
                    family="protective",
                    role="hedge",
                    strategy_sleeve_id="protective_short",
                    allocation_id="alloc_protective",
                    margin_mode="cross",
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("-0.02"),
                    delta_position_qty=Decimal("-0.02"),
                    execution_mode="protective_overlay",
                ),
            ],
            selected_candidate=StrategyCandidate(
                family="protective",
                state="ready",
                enabled=True,
                selectable=True,
                execution_compatible=True,
                route_action="override_target",
                family_action="protect",
                headline="protective",
                metrics={
                    "overlay_parent_exposure": {
                        "parent_family": "directional",
                        "symbol": "BTC-USDT-SWAP",
                        "target_leverage": 2.0,
                        "margin_mode": "cross",
                        "target_long_qty": Decimal("0"),
                        "target_short_qty": Decimal("0"),
                        "current_long_qty": Decimal("0.02"),
                        "current_short_qty": Decimal("0"),
                        "target_qty": Decimal("0"),
                        "current_qty": Decimal("0.02"),
                        "effective_qty": Decimal("0.02"),
                        "target_signal": "flat",
                        "current_signal": "long",
                        "effective_signal": "long",
                        "signal_source": "inventory",
                        "source_of_truth": "inventory",
                        "lifecycle_state": "inventory_only",
                        "target_active": False,
                        "inventory_active": True,
                        "source": "directional_target_with_inventory_continuity",
                    },
                    "parent_target_signal": "flat",
                    "parent_current_signal": "long",
                    "parent_effective_signal": "long",
                    "parent_exposure_signal_source": "inventory",
                    "parent_source_of_truth": "inventory",
                    "parent_target_qty": Decimal("0"),
                    "parent_current_qty": Decimal("0.02"),
                    "parent_effective_qty": Decimal("0.02"),
                },
            ),
        )

        assert summary is not None
        self.assertIsNotNone(summary.overlay_parent_exposure)
        self.assertEqual(summary.parent_target_signal, "flat")
        self.assertEqual(summary.parent_current_signal, "long")
        self.assertEqual(summary.parent_effective_signal, "long")
        self.assertEqual(summary.signal_source, "inventory")
        self.assertEqual(summary.parent_source_of_truth, "inventory")
        self.assertEqual(summary.parent_target_qty, Decimal("0"))
        self.assertEqual(summary.parent_current_qty, Decimal("0.02"))
        self.assertEqual(summary.parent_effective_qty, Decimal("0.02"))
        self.assertEqual(summary.overlay_parent_exposure.target_signal, "flat")
        self.assertEqual(summary.overlay_parent_exposure.current_signal, "long")
        self.assertEqual(summary.overlay_parent_exposure.effective_signal, "long")
        self.assertEqual(summary.overlay_parent_exposure.parent_family, "directional")
        self.assertEqual(summary.overlay_parent_exposure.symbol, "BTC-USDT-SWAP")
        self.assertEqual(summary.overlay_parent_exposure.target_leverage, 2.0)
        self.assertEqual(summary.overlay_parent_exposure.margin_mode, "cross")
        self.assertEqual(summary.overlay_parent_exposure.source_of_truth, "inventory")
        self.assertEqual(summary.overlay_parent_exposure.target_qty, Decimal("0"))
        self.assertEqual(summary.overlay_parent_exposure.current_qty, Decimal("0.02"))
        self.assertEqual(summary.overlay_parent_exposure.effective_qty, Decimal("0.02"))

    def test_family_execution_summary_preserves_independent_close_reason(self) -> None:
        summary = StrategyCoordinatorService._family_execution_summary(
            selected_family="independent",
            family_action="close_failed_thesis_independent_book",
            route_action="override_target",
            strategy_execution_legs=[
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="sell",
                    position_mode="long_short_mode",
                    pos_side="long",
                    action="close",
                    family="independent",
                    role="primary",
                    strategy_sleeve_id="independent_long",
                    allocation_id="alloc_independent",
                    margin_mode="cross",
                    current_position_qty=Decimal("0.02"),
                    target_position_qty=Decimal("0"),
                    delta_position_qty=Decimal("-0.02"),
                    execution_mode="independent_long_book",
                ),
            ],
            selected_candidate=StrategyCandidate(
                family="independent",
                state="unwinding",
                enabled=True,
                selectable=True,
                execution_compatible=True,
                route_action="override_target",
                family_action="close_failed_thesis_independent_book",
                headline="independent close",
                metrics={"close_reason": "failed_thesis"},
            ),
        )

        assert summary is not None
        self.assertEqual(summary.close_reason, "failed_thesis")

    def test_family_execution_summary_preserves_independent_book_runtime_states(self) -> None:
        summary = StrategyCoordinatorService._family_execution_summary(
            selected_family="independent",
            family_action="open_independent_book",
            route_action="override_target",
            strategy_execution_legs=[
                StrategyLegIntent(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="buy",
                    position_mode="long_short_mode",
                    pos_side="long",
                    action="open",
                    family="independent",
                    role="primary",
                    strategy_sleeve_id="independent_long",
                    allocation_id="alloc_independent",
                    margin_mode="cross",
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("0.01"),
                    delta_position_qty=Decimal("0.01"),
                    execution_mode="independent_long_book",
                ),
            ],
            selected_candidate=StrategyCandidate(
                family="independent",
                state="opening",
                enabled=True,
                selectable=True,
                execution_compatible=True,
                route_action="override_target",
                family_action="open_independent_book",
                headline="independent open",
                book_runtime_states=[
                    StrategyBookRuntimeState(
                        leg="long",
                        current_qty=Decimal("0"),
                        target_qty=Decimal("0.01"),
                        state="opening",
                        book_action="open",
                    ),
                    StrategyBookRuntimeState(
                        leg="short",
                        current_qty=Decimal("0.01"),
                        target_qty=Decimal("0"),
                        state="holding",
                        book_action="hold",
                    ),
                ],
            ),
        )

        assert summary is not None
        self.assertEqual([item.leg for item in summary.book_runtime_states], ["long", "short"])

    def test_final_action_for_selected_family_maps_independent_thesis_actions(self) -> None:
        self.assertEqual(
            StrategyCoordinatorService._final_action_for_selected_family(
                family_action="de_risk_independent_book",
                route_action="override_target",
                strategy_execution_legs=[],
            ),
            "reduce",
        )
        self.assertEqual(
            StrategyCoordinatorService._final_action_for_selected_family(
                family_action="close_failed_thesis_independent_book",
                route_action="override_target",
                strategy_execution_legs=[],
            ),
            "exit",
        )
        self.assertEqual(
            StrategyCoordinatorService._final_action_for_selected_family(
                family_action="close_stale_thesis_independent_book",
                route_action="override_target",
                strategy_execution_legs=[],
            ),
            "exit",
        )

    def test_independent_full_close_failed_thesis_allocation_target_stays_zero(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "derivatives", "margin_mode": "cross"})
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.007",
            target_qty="0",
            rebalance_reason="independent_strategy",
            position_intent="close_long",
        ).model_copy(update={"current_notional": Decimal("0"), "target_notional": Decimal("0")})
        intent = _independent_full_close_intent(
            decision_id="decision_failed_close",
            family_action="close_failed_thesis_independent_book",
            book_action="close_failed_thesis",
            close_reason="failed_thesis",
        )
        assignment = SleeveBudgetAssignment(
            budget_profile_id="budget_profile_test",
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family="independent",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            effective_margin_budget_limit=Decimal("600"),
            effective_max_symbol_notional=Decimal("600"),
            effective_notional_cap=Decimal("600"),
            effective_quote_budget_limit=Decimal("600"),
        )

        scaled_intent, budget_snapshot = allocator._apply_budget_assignment(
            intent=intent,
            base_target=base_target,
            assignment=assignment,
            allocation_id="alloc_failed_close",
        )

        self.assertEqual(scaled_intent.target_position_qty, Decimal("0"))
        self.assertEqual(scaled_intent.delta_position_qty, Decimal("-0.007"))
        self.assertEqual(budget_snapshot.requested_notional, Decimal("700"))
        self.assertEqual(budget_snapshot.approved_notional, Decimal("700"))
        self.assertFalse(budget_snapshot.clamped)
        self.assertIn("allocator_budget_cap_bypassed_for_independent_full_close", budget_snapshot.reason_codes)

    def test_independent_full_close_stale_thesis_allocation_target_stays_zero(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "derivatives", "margin_mode": "cross"})
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.007",
            target_qty="0",
            rebalance_reason="independent_strategy",
            position_intent="close_long",
        ).model_copy(update={"current_notional": Decimal("0"), "target_notional": Decimal("0")})
        intent = _independent_full_close_intent(
            decision_id="decision_stale_close",
            family_action="close_stale_thesis_independent_book",
            book_action="close_stale_thesis",
            close_reason="stale_thesis",
        )
        assignment = SleeveBudgetAssignment(
            budget_profile_id="budget_profile_test",
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family="independent",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            effective_margin_budget_limit=Decimal("600"),
            effective_max_symbol_notional=Decimal("600"),
            effective_notional_cap=Decimal("600"),
            effective_quote_budget_limit=Decimal("600"),
        )

        scaled_intent, _budget_snapshot = allocator._apply_budget_assignment(
            intent=intent,
            base_target=base_target,
            assignment=assignment,
            allocation_id="alloc_stale_close",
        )

        self.assertEqual(scaled_intent.target_position_qty, Decimal("0"))
        self.assertEqual(scaled_intent.delta_position_qty, Decimal("-0.007"))

    def test_independent_full_close_derisk_promoted_to_close_allocation_target_stays_zero(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "derivatives", "margin_mode": "cross"})
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.007",
            target_qty="0",
            rebalance_reason="independent_strategy",
            position_intent="close_long",
        ).model_copy(update={"current_notional": Decimal("0"), "target_notional": Decimal("0")})
        intent = _independent_full_close_intent(
            decision_id="decision_derisk_close",
            family_action="de_risk_independent_book",
            book_action="de_risk",
            close_reason="execution_health_degraded",
        )
        assignment = SleeveBudgetAssignment(
            budget_profile_id="budget_profile_test",
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family="independent",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            effective_margin_budget_limit=Decimal("600"),
            effective_max_symbol_notional=Decimal("600"),
            effective_notional_cap=Decimal("600"),
            effective_quote_budget_limit=Decimal("600"),
        )

        scaled_intent, _budget_snapshot = allocator._apply_budget_assignment(
            intent=intent,
            base_target=base_target,
            assignment=assignment,
            allocation_id="alloc_derisk_close",
        )

        self.assertEqual(scaled_intent.target_position_qty, Decimal("0"))
        self.assertEqual(scaled_intent.delta_position_qty, Decimal("-0.007"))

    def test_independent_full_close_requested_notional_does_not_fallback_to_max_symbol_notional(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "derivatives", "margin_mode": "cross"})
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.007",
            target_qty="0",
            rebalance_reason="independent_strategy",
            position_intent="close_long",
        ).model_copy(update={"current_notional": Decimal("0"), "target_notional": Decimal("0")})
        intent = _independent_full_close_intent(
            decision_id="decision_requested_notional",
            family_action="close_failed_thesis_independent_book",
            book_action="close_failed_thesis",
            close_reason="failed_thesis",
        )
        assignment = SleeveBudgetAssignment(
            budget_profile_id="budget_profile_test",
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family="independent",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            effective_margin_budget_limit=Decimal("10000"),
            effective_max_symbol_notional=Decimal("10000"),
            effective_notional_cap=Decimal("10000"),
            effective_quote_budget_limit=Decimal("10000"),
        )

        requested_notional = allocator._requested_notional(
            intent=intent,
            base_target=base_target,
            assignment=assignment,
        )

        self.assertEqual(requested_notional, Decimal("700"))

    def test_independent_full_close_is_exempt_from_portfolio_redistribution(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "max_total_open_notional": 600,
            }
        )
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.007",
            target_qty="0",
            rebalance_reason="independent_strategy",
            position_intent="close_long",
        ).model_copy(update={"current_notional": Decimal("0"), "target_notional": Decimal("0")})
        intent = _independent_full_close_intent(
            decision_id="decision_portfolio_close",
            family_action="close_failed_thesis_independent_book",
            book_action="close_failed_thesis",
            close_reason="failed_thesis",
        )
        assignment = SleeveBudgetAssignment(
            budget_profile_id="budget_profile_test",
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family="independent",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            effective_margin_budget_limit=Decimal("10000"),
            effective_max_symbol_notional=Decimal("10000"),
            effective_notional_cap=Decimal("10000"),
            effective_quote_budget_limit=Decimal("10000"),
        )

        scaled_intent, budget_snapshot = allocator._apply_budget_assignment(
            intent=intent,
            base_target=base_target,
            assignment=assignment,
            allocation_id="alloc_portfolio_close",
        )
        redistributed_intents, redistributed_snapshots, budget_cut_reason_codes = allocator._apply_portfolio_budget_redistribution(
            approved=[scaled_intent],
            budget_snapshots=[budget_snapshot],
            base_target=base_target,
        )

        self.assertEqual(budget_cut_reason_codes, [])
        self.assertEqual(len(redistributed_intents), 1)
        self.assertEqual(redistributed_intents[0].target_position_qty, Decimal("0"))
        self.assertEqual(redistributed_intents[0].delta_position_qty, Decimal("-0.007"))
        self.assertEqual(redistributed_snapshots[0].approved_notional, Decimal("700"))
        self.assertIn(
            "allocator_portfolio_cap_exempt_for_independent_full_close",
            redistributed_snapshots[0].reason_codes,
        )

    def test_independent_mixed_full_close_preserves_closing_leg_while_scaling_open_leg(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "derivatives", "margin_mode": "cross"})
        allocator = PortfolioAllocatorV2Phase2(settings=settings)
        sleeve_id = build_strategy_sleeve_id(
            family="independent",
            primary_symbol="BTC-USDT-SWAP",
            product_scope="derivatives",
            margin_scope="cross",
            symbol_scope=("BTC-USDT-SWAP",),
        )
        long_close_leg = StrategyLegIntent(
            symbol="BTC-USDT-SWAP",
            execution_chain_id="independent:decision_mixed:long:close_failed_thesis",
            product_type="derivatives",
            side="sell",
            position_mode="long_short_mode",
            pos_side="long",
            action="close",
            family="independent",
            role="primary",
            margin_mode="cross",
            target_leverage=3.0,
            current_position_qty=Decimal("0.007"),
            target_position_qty=Decimal("0"),
            delta_position_qty=Decimal("-0.007"),
            reference_price=Decimal("100000"),
            execution_compatible=True,
            execution_mode="independent_long_book",
            strategy_sleeve_id=sleeve_id,
            book_action="close_failed_thesis",
            close_reason="failed_thesis",
        )
        short_open_leg = StrategyLegIntent(
            symbol="BTC-USDT-SWAP",
            execution_chain_id="independent:decision_mixed:short:open",
            product_type="derivatives",
            side="sell",
            position_mode="long_short_mode",
            pos_side="short",
            action="open",
            family="independent",
            role="primary",
            margin_mode="cross",
            target_leverage=3.0,
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("-0.003"),
            delta_position_qty=Decimal("-0.003"),
            reference_price=Decimal("100000"),
            execution_compatible=True,
            execution_mode="independent_short_book",
            strategy_sleeve_id=sleeve_id,
            book_action="open",
        )
        intent = StrategySleeveIntent(
            decision_id="decision_mixed",
            family="independent",
            strategy_sleeve_id=sleeve_id,
            state="active",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            inventory_policy="inventory_accumulation",
            route_action="override_target",
            family_action="close_failed_thesis_independent_book",
            headline="independent mixed close/open",
            selectable=True,
            execution_compatible=True,
            current_position_qty=Decimal("0.007"),
            target_position_qty=Decimal("-0.003"),
            delta_position_qty=Decimal("-0.010"),
            target_notional=Decimal("0"),
            requested_delta_position_qty=Decimal("-0.010"),
            legs=[long_close_leg, short_open_leg],
        )
        assignment = SleeveBudgetAssignment(
            budget_profile_id="budget_profile_test",
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family="independent",
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            effective_margin_budget_limit=Decimal("200"),
            effective_max_symbol_notional=Decimal("200"),
            effective_notional_cap=Decimal("200"),
            effective_quote_budget_limit=Decimal("200"),
        )
        base_target = _position_target(
            symbol="BTC-USDT-SWAP",
            product_type="derivatives",
            margin_mode="cross",
            current_qty="0.007",
            target_qty="-0.003",
            rebalance_reason="independent_strategy",
            position_intent="reverse_to_short",
        ).model_copy(update={"current_notional": Decimal("0"), "target_notional": Decimal("0")})

        scaled_intent, budget_snapshot = allocator._apply_budget_assignment(
            intent=intent,
            base_target=base_target,
            assignment=assignment,
            allocation_id="alloc_mixed",
        )

        self.assertEqual(budget_snapshot.requested_notional, Decimal("1000"))
        self.assertEqual(budget_snapshot.approved_notional, Decimal("900"))
        self.assertTrue(budget_snapshot.clamped)
        self.assertEqual(scaled_intent.legs[0].delta_position_qty, Decimal("-0.007"))
        self.assertEqual(scaled_intent.legs[0].target_position_qty, Decimal("0"))
        self.assertEqual(scaled_intent.legs[1].delta_position_qty, Decimal("-0.002"))
        self.assertEqual(scaled_intent.legs[1].target_position_qty, Decimal("-0.002"))


if __name__ == "__main__":
    unittest.main()
