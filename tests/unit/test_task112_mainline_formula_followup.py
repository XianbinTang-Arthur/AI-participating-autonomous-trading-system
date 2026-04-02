from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.execution import FillEvent
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.strategy_runtime import StrategyCandidate, StrategyLegIntent
from aats.services.strategy_engines.base import StrategyEngineInput, StrategyTargetHistory
from aats.services.strategy_engines.dca import DcaStrategyEngine
from aats.services.strategy_engines.spot_grid import SpotGridStrategyEngine
from aats.services.strategy_engines.smart_arbitrage.engine import SmartArbitrageStrategyEngine
from aats.services.strategy_execution_health import compute_strategy_execution_health


def _market_snapshot(symbol: str, price: str) -> MarketSnapshot:
    price_decimal = Decimal(price)
    now = utc_now()
    return MarketSnapshot(
        symbol=symbol,
        exchange="TEST",
        snapshot_ts=now,
        best_bid=price_decimal - Decimal("0.1"),
        best_ask=price_decimal + Decimal("0.1"),
        last_price=price_decimal,
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume_24h=Decimal("1000"),
        kline_15m={"close": price_decimal},
        kline_1h={"close": price_decimal},
        recent_trades=[],
        orderbook_depth={},
    )


def _baseline(symbol: str, *, regime: str = "range") -> BaselineAssessment:
    return BaselineAssessment(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        regime=regime,
        direction_bias="flat",
        trend_strength=0.1,
        volatility_state="medium",
        confidence=0.6,
        composite_alpha_score=0.1,
        suggested_position_scale=0.4,
        volatility_target_scale=1.0,
        factor_scores={},
        holding_horizon="15m",
        invalidation_conditions=[],
        reason_codes=[],
        engine_version="test",
    )


def _context(symbol: str, *, current_position_qty: str, product_type: str = "spot") -> DecisionContext:
    quantity = Decimal(current_position_qty)
    return DecisionContext(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        timeframe="15m",
        as_of_ts=utc_now(),
        market_snapshot_ref="market_ref",
        feature_snapshot_ref="feature_ref",
        portfolio_snapshot_ref="portfolio_ref",
        health_snapshot_ref="health_ref",
        mode="paper",
        current_position_qty=quantity,
        product_type=product_type,  # type: ignore[arg-type]
        current_exposure_side="flat" if quantity == 0 else ("long" if quantity > 0 else "short"),
        current_open_orders=[],
    )


def _target(
    *,
    symbol: str,
    current_qty: str,
    target_qty: str,
    product_type: str = "spot",
    margin_mode: str = "cash",
) -> PositionTarget:
    current_decimal = Decimal(current_qty)
    target_decimal = Decimal(target_qty)
    return PositionTarget(
        decision_id=f"decision_{symbol}",
        symbol=symbol,
        current_position_qty=current_decimal,
        target_position_qty=target_decimal,
        delta_position_qty=target_decimal - current_decimal,
        current_notional=Decimal("0"),
        target_notional=Decimal("0"),
        rebalance_reason="test_target",
        urgency="low",
        max_slippage_tolerance_bps=20,
        source_mix={"baseline": 1.0},
        decision_expiry_ts=utc_now(),
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _hedge_fill(
    *,
    fill_id: str,
    side: str,
    pos_side: str,
    price: str,
    fee_amount: str = "0",
    ts: datetime,
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        decision_id="decision_task112",
        intent_id="intent_task112",
        client_order_id=f"clord_{fill_id}",
        exchange_order_id=f"ord_{fill_id}",
        symbol="BTC-USDT-SWAP",
        venue="OKX",
        side=side,  # type: ignore[arg-type]
        fill_qty=Decimal("1"),
        fill_price=Decimal(price),
        fee_amount=Decimal(fee_amount),
        fee_currency="USDT",
        reduce_only=False,
        close_only=False,
        position_mode="long_short_mode",
        pos_side=pos_side,  # type: ignore[arg-type]
        strategy_family="directional",
        product_type="derivatives",
        target_leverage=2.0,
        margin_mode="cross",
        execution_action="enter",
        position_intent="open_long" if pos_side == "long" else "open_short",
        liquidity_role="taker",
        exchange_timestamp=ts,
        ingestion_timestamp=ts,
        order_status_after_fill="FILLED",
    )


def _snapshot(*, fill_id: str, realized_pnl: str, ts: datetime) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        decision_id=f"decision_{fill_id}",
        source_fill_id=fill_id,
        snapshot_ts=ts,
        balances={"USDT": Decimal("1000")},
        positions=[],
        cost_basis={},
        realized_pnl=Decimal(realized_pnl),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal("1000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type="derivatives",
        margin_mode="cross",
    )


def _arbitrage_leg(
    *,
    symbol: str,
    product_type: str,
    side: str,
    margin_mode: str,
    delta_position_qty: str,
    reference_price: str,
    pair_id: str,
) -> StrategyLegIntent:
    return StrategyLegIntent(
        symbol=symbol,
        product_type=product_type,  # type: ignore[arg-type]
        side=side,  # type: ignore[arg-type]
        role="primary" if product_type == "spot" else "hedge",
        margin_mode=margin_mode,  # type: ignore[arg-type]
        delta_position_qty=Decimal(delta_position_qty),
        reference_price=Decimal(reference_price),
        execution_compatible=True,
        pair_id=pair_id,
    )


class _RecordingSleeveInventoryLoader:
    def __init__(self, *, quantity: str = "0") -> None:
        self.quantity = Decimal(quantity)
        self.calls: list[dict[str, object]] = []

    def quantity_for_strategy(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.quantity


class TestTask112MainlineFormulaFollowup(TestCase):
    def test_spot_grid_uses_last_required_anchor_snapshots_when_extra_history_is_present(self) -> None:
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
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.0,
                "max_abs_position_qty": 1.0,
            }
        )
        engine = SpotGridStrategyEngine(settings=settings)
        engine_input = StrategyEngineInput(
            context=_context("BTC-USDT", current_position_qty="0"),
            baseline=_baseline("BTC-USDT"),
            directional_target=_target(symbol="BTC-USDT", current_qty="0", target_qty="0"),
            latest_snapshot=None,
            latest_account_snapshot=None,
            latest_market_snapshot=_market_snapshot("BTC-USDT", "95"),
            recent_market_snapshots={
                "BTC-USDT": [
                    _market_snapshot("BTC-USDT", "50"),
                    _market_snapshot("BTC-USDT", "100"),
                    _market_snapshot("BTC-USDT", "100"),
                    _market_snapshot("BTC-USDT", "100"),
                    _market_snapshot("BTC-USDT", "100"),
                ]
            },
            recent_targets_by_family={"directional": [], "smart_arbitrage": [], "spot_grid": [], "dca": []},
        )

        candidate = engine.evaluate(engine_input)

        self.assertEqual(candidate.metrics["anchor_price"], Decimal("100"))
        self.assertEqual(candidate.metrics["target_sleeve_position_qty"], Decimal("1"))
        self.assertEqual(candidate.metrics["target_account_position_qty"], Decimal("1"))

    def test_spot_grid_inventory_lookup_uses_cash_scope_even_if_settings_margin_mode_is_cross(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "spot_grid_enabled": True,
                "spot_grid_breakout_guard_enabled": False,
                "spot_grid_anchor_lookback_snapshots": 4,
                "spot_grid_band_bps": 500.0,
                "spot_grid_inventory_floor_fraction": 0.0,
                "spot_grid_inventory_ceiling_fraction": 1.0,
                "spot_grid_rebalance_min_fraction_of_max_qty": 0.0,
                "max_abs_position_qty": 1.0,
            }
        )
        loader = _RecordingSleeveInventoryLoader(quantity="0.2")
        engine = SpotGridStrategyEngine(settings=settings, sleeve_inventory_loader=loader)
        engine_input = StrategyEngineInput(
            context=_context("BTC-USDT", current_position_qty="0.5"),
            baseline=_baseline("BTC-USDT"),
            directional_target=_target(symbol="BTC-USDT", current_qty="0.5", target_qty="0.5"),
            latest_snapshot=None,
            latest_account_snapshot=None,
            latest_market_snapshot=_market_snapshot("BTC-USDT", "99"),
            recent_market_snapshots={
                "BTC-USDT": [
                    _market_snapshot("BTC-USDT", "100"),
                    _market_snapshot("BTC-USDT", "101"),
                    _market_snapshot("BTC-USDT", "100"),
                    _market_snapshot("BTC-USDT", "99"),
                ]
            },
            recent_targets_by_family={"directional": [], "smart_arbitrage": [], "spot_grid": [], "dca": []},
        )

        candidate = engine.evaluate(engine_input)

        self.assertEqual(candidate.metrics["current_sleeve_position_qty"], Decimal("0.2"))
        self.assertEqual(candidate.metrics["target_account_position_qty"], Decimal("0.9"))
        self.assertEqual(len(loader.calls), 1)
        self.assertEqual(loader.calls[0]["margin_scope"], "cash")
        self.assertEqual(loader.calls[0]["margin_mode"], "cash")

    def test_dca_uses_latest_matching_target_timestamp_for_interval_gate(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ("BTC-USDT",),
                "dca_enabled": True,
                "dca_interval_seconds": 3600.0,
                "dca_quote_budget_per_cycle": 100.0,
                "max_abs_position_qty": 2.0,
            }
        )
        engine = DcaStrategyEngine(settings=settings)
        latest_target = _target(symbol="BTC-USDT", current_qty="0", target_qty="0.5").model_copy(
            update={
                "strategy_family": "dca",
                "strategy_route_action": "override_target",
            }
        )
        older_target = _target(symbol="BTC-USDT", current_qty="0", target_qty="0.25").model_copy(
            update={
                "strategy_family": "dca",
                "strategy_route_action": "override_target",
            }
        )
        engine_input = StrategyEngineInput(
            context=_context("BTC-USDT", current_position_qty="0.5"),
            baseline=_baseline("BTC-USDT"),
            directional_target=_target(symbol="BTC-USDT", current_qty="0.5", target_qty="0.5"),
            latest_snapshot=None,
            latest_account_snapshot=None,
            latest_market_snapshot=_market_snapshot("BTC-USDT", "100"),
            recent_market_snapshots={"BTC-USDT": []},
            recent_targets_by_family={
                "dca": [
                    StrategyTargetHistory(created_at=utc_now() - timedelta(hours=2), target=older_target),
                    StrategyTargetHistory(created_at=utc_now() - timedelta(minutes=10), target=latest_target),
                ]
            },
        )

        candidate = engine.evaluate(engine_input)

        self.assertEqual(candidate.state, "inactive")
        self.assertIn("dca_interval_not_elapsed", candidate.reason_codes)

    def test_symbol_health_does_not_treat_hedge_leg_open_as_closed_trade(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
            }
        )
        opened_at = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
        hedge_opened_at = opened_at + timedelta(minutes=1)
        snapshot = compute_strategy_execution_health(
            settings=settings,
            symbol="BTC-USDT-SWAP",
            fills=[
                _hedge_fill(fill_id="fill_long_open", side="buy", pos_side="long", price="100", ts=opened_at),
                _hedge_fill(
                    fill_id="fill_short_open",
                    side="sell",
                    pos_side="short",
                    price="101",
                    fee_amount="1",
                    ts=hedge_opened_at,
                ),
            ],
            snapshots=[
                _snapshot(fill_id="fill_long_open", realized_pnl="0", ts=opened_at),
                _snapshot(fill_id="fill_short_open", realized_pnl="0", ts=hedge_opened_at),
            ],
            current_position_qty=Decimal("0"),
            current_long_position_qty=Decimal("1"),
            current_short_position_qty=Decimal("1"),
        )

        self.assertEqual(snapshot.recent_closed_trade_count, 0)
        self.assertEqual(snapshot.recent_fee_drag_ratio, 0.0)
        self.assertIsNone(snapshot.last_position_closed_at)
        self.assertEqual(snapshot.current_position_opened_at, opened_at)

    def test_smart_arbitrage_multi_pair_metrics_use_requested_notional_weights(self) -> None:
        engine = SmartArbitrageStrategyEngine(
            settings=AATSSettings.model_validate({}),
            market_snapshot_loader=lambda _symbol: None,
        )
        pair_one = StrategyCandidate(
            family="smart_arbitrage",
            state="opening",
            enabled=True,
            selectable=True,
            execution_compatible=True,
            route_action="override_target",
            headline="pair_one",
            recommended_symbol="BTC-USDT-SWAP",
            score=10.0,
            confidence=0.7,
            urgency="medium",
            pair_id="pair_one",
            opportunity_kind="positive_basis",
            execution_mode="spot_carry",
            state_phase="opening",
            metrics={
                "spot_symbol": "BTC-USDT",
                "derivatives_symbol": "BTC-USDT-SWAP",
                "basis_bps": Decimal("20"),
                "net_basis_bps": Decimal("10"),
                "ideal_cost_bps": Decimal("2"),
                "executable_cost_bps": Decimal("3"),
                "ideal_edge_bps": Decimal("18"),
                "executable_edge_bps": Decimal("10"),
                "breakeven_basis_bps": Decimal("3"),
                "entry_threshold_bps": Decimal("5"),
                "exit_threshold_bps": Decimal("2"),
                "cost_confidence": Decimal("0.6"),
            },
            legs=[
                _arbitrage_leg(
                    symbol="BTC-USDT",
                    product_type="spot",
                    side="buy",
                    margin_mode="cash",
                    delta_position_qty="1",
                    reference_price="100",
                    pair_id="pair_one",
                ),
                _arbitrage_leg(
                    symbol="BTC-USDT-SWAP",
                    product_type="derivatives",
                    side="sell",
                    margin_mode="cross",
                    delta_position_qty="-1",
                    reference_price="100",
                    pair_id="pair_one",
                ),
            ],
        )
        pair_two = StrategyCandidate(
            family="smart_arbitrage",
            state="opening",
            enabled=True,
            selectable=True,
            execution_compatible=True,
            route_action="override_target",
            headline="pair_two",
            recommended_symbol="ETH-USDT-SWAP",
            score=20.0,
            confidence=0.8,
            urgency="high",
            pair_id="pair_two",
            opportunity_kind="positive_basis",
            execution_mode="spot_carry",
            state_phase="opening",
            metrics={
                "spot_symbol": "ETH-USDT",
                "derivatives_symbol": "ETH-USDT-SWAP",
                "basis_bps": Decimal("40"),
                "net_basis_bps": Decimal("20"),
                "ideal_cost_bps": Decimal("6"),
                "executable_cost_bps": Decimal("8"),
                "ideal_edge_bps": Decimal("34"),
                "executable_edge_bps": Decimal("20"),
                "breakeven_basis_bps": Decimal("8"),
                "entry_threshold_bps": Decimal("5"),
                "exit_threshold_bps": Decimal("2"),
                "cost_confidence": Decimal("0.9"),
            },
            legs=[
                _arbitrage_leg(
                    symbol="ETH-USDT",
                    product_type="spot",
                    side="buy",
                    margin_mode="cash",
                    delta_position_qty="3",
                    reference_price="100",
                    pair_id="pair_two",
                ),
                _arbitrage_leg(
                    symbol="ETH-USDT-SWAP",
                    product_type="derivatives",
                    side="sell",
                    margin_mode="cross",
                    delta_position_qty="-3",
                    reference_price="100",
                    pair_id="pair_two",
                ),
            ],
        )

        aggregate = engine._aggregate_candidates(
            candidates=[pair_one, pair_two],
            selected_pairs=[pair_one, pair_two],
        )

        self.assertEqual(aggregate.metrics["aggregate_requested_notional"], Decimal("400"))
        self.assertEqual(aggregate.metrics["basis_bps"], Decimal("35"))
        self.assertEqual(aggregate.metrics["net_basis_bps"], Decimal("17.5"))
        self.assertEqual(aggregate.metrics["ideal_cost_bps"], Decimal("5"))
        self.assertEqual(aggregate.metrics["executable_cost_bps"], Decimal("6.75"))
        self.assertEqual(aggregate.metrics["executable_edge_bps"], Decimal("17.5"))
