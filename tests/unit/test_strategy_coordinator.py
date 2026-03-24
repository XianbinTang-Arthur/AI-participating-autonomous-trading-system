from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import unittest

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import BaselineAssessment, DecisionContext, DecisionOutcome, PositionTarget
from aats.schemas.market import MarketSnapshot
from aats.services.strategy_engines.coordinator import StrategyCoordinatorService
from aats.storage.event_store import InMemoryEventStore
from aats.storage.portfolio_repo import InMemoryPortfolioRepository


class _FakeMarketGateway:
    def __init__(self, snapshots: dict[str, MarketSnapshot]) -> None:
        self._snapshots = snapshots

    def latest_snapshot(self, symbol: str):
        return self._snapshots.get(symbol)


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
        kline_15m={"close": price_decimal},
        kline_1h={"close": price_decimal},
        recent_trades=[],
        orderbook_depth={},
    )


def _decision_context(*, symbol: str, product_type: str, current_position_qty: str) -> DecisionContext:
    now = utc_now()
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
        current_position_qty=Decimal(current_position_qty),
        current_open_orders=[],
        product_type=product_type,
        current_exposure_side="flat" if Decimal(current_position_qty) == 0 else "long",
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
        margin_mode=margin_mode,
        expected_signal_edge_bps=12.0,
        expected_cost_bps=4.0,
        expected_net_edge_bps=8.0,
        decision_outcome=_decision_outcome(symbol),
    )


class TestStrategyCoordinator(unittest.TestCase):
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
        self.assertGreater(abs(applied.target_position_qty), Decimal("0"))
        self.assertEqual(applied.target_notional, applied.target_position_qty * Decimal("99"))

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

        self.assertEqual(snapshot.selected_family, "smart_arbitrage")
        self.assertEqual(snapshot.selected_route_action, "advisory_only")
        self.assertEqual(applied.strategy_family, "smart_arbitrage")
        self.assertEqual(applied.strategy_route_action, "protective_fallback")
        self.assertEqual(applied.target_position_qty, Decimal("0"))
        self.assertEqual(applied.decision_outcome.selected_strategy_route_action, "protective_fallback")

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

        self.assertEqual(snapshot.selected_family, "dca")
        self.assertEqual(selected.state, "inactive")
        self.assertIn("dca_interval_not_elapsed", selected.reason_codes)


if __name__ == "__main__":
    unittest.main()
