from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
import unittest

from aats.schemas.market import MarketSnapshot
from aats.services.strategy_engines.independent.gates import (
    anomaly_cost_fuse_threshold_bps,
    evaluate_open_eligibility,
)
from aats.services.strategy_engines.independent.models import IndependentBookExpectancy
from aats.services.trade_costs import TradeCostService
from tests.support.strategy_family import make_context, make_derivatives_hedge_settings


@dataclass(frozen=True, slots=True)
class _LiveReplayCase:
    label: str
    decision_id: str
    symbol: str
    leg: str
    order_side: str
    exchange: str
    product_type: str
    margin_mode: str
    expected_slippage_bps: Decimal
    snapshot_ts: str
    best_bid: Decimal
    best_ask: Decimal
    last_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    expected_signal_edge_bps: float
    expected_cost_bps: float
    expected_net_edge_bps: float
    orderbook_depth: dict[str, list[object]] | None = None


def _btc_live_replay_short_cases() -> tuple[_LiveReplayCase, ...]:
    # Extracted from live PostgreSQL on 2026-04-01 ET. The live market snapshot
    # payloads in this window only retained top-of-book sizes, so replay
    # calibration intentionally uses top-of-book depth rather than full ladder depth.
    return (
        _LiveReplayCase(
            label="2026-04-01 13:23:30 ET",
            decision_id="decision_536d21f5554b4b938a079e47b74c1f5f",
            symbol="BTC-USDT-SWAP",
            leg="short",
            order_side="sell",
            exchange="OKX",
            product_type="derivatives",
            margin_mode="cross",
            expected_slippage_bps=Decimal("5.6"),
            snapshot_ts="2026-04-01T13:23:30.240539-04:00",
            best_bid=Decimal("68459.6"),
            best_ask=Decimal("68459.7"),
            last_price=Decimal("68459.6"),
            bid_size=Decimal("17.36"),
            ask_size=Decimal("332.01"),
            expected_signal_edge_bps=43.25976328016417,
            expected_cost_bps=10.6,
            expected_net_edge_bps=28.15976328016417,
        ),
        _LiveReplayCase(
            label="2026-04-01 13:26:03 ET",
            decision_id="decision_18b621e591dc4af5af9423568794c076",
            symbol="BTC-USDT-SWAP",
            leg="short",
            order_side="sell",
            exchange="OKX",
            product_type="derivatives",
            margin_mode="cross",
            expected_slippage_bps=Decimal("5.6"),
            snapshot_ts="2026-04-01T13:26:03.515453-04:00",
            best_bid=Decimal("68495.1"),
            best_ask=Decimal("68495.2"),
            last_price=Decimal("68495.1"),
            bid_size=Decimal("0.22"),
            ask_size=Decimal("1814.36"),
            expected_signal_edge_bps=42.717376373455714,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.617376373455713,
        ),
        _LiveReplayCase(
            label="2026-04-01 13:28:28 ET",
            decision_id="decision_d05a776a50ea4804905457c3c2b15a61",
            symbol="BTC-USDT-SWAP",
            leg="short",
            order_side="sell",
            exchange="OKX",
            product_type="derivatives",
            margin_mode="cross",
            expected_slippage_bps=Decimal("5.6"),
            snapshot_ts="2026-04-01T13:28:28.179327-04:00",
            best_bid=Decimal("68496.5"),
            best_ask=Decimal("68496.6"),
            last_price=Decimal("68496.5"),
            bid_size=Decimal("0.09"),
            ask_size=Decimal("1411.33"),
            expected_signal_edge_bps=42.69886642728371,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.59886642728371,
        ),
        _LiveReplayCase(
            label="2026-04-01 14:09:44 ET",
            decision_id="decision_5b1c53ba25524f92b7aa2853fc034fa1",
            symbol="BTC-USDT-SWAP",
            leg="short",
            order_side="sell",
            exchange="OKX",
            product_type="derivatives",
            margin_mode="cross",
            expected_slippage_bps=Decimal("5.6"),
            snapshot_ts="2026-04-01T14:09:44.630305-04:00",
            best_bid=Decimal("67982.9"),
            best_ask=Decimal("67983"),
            last_price=Decimal("67983"),
            bid_size=Decimal("45.64"),
            ask_size=Decimal("1508.42"),
            expected_signal_edge_bps=42.49839930352322,
            expected_cost_bps=10.6,
            expected_net_edge_bps=27.39839930352322,
        ),
    )


def _btc_live_replay_long_cases() -> tuple[_LiveReplayCase, ...]:
    return (
        _LiveReplayCase(
            label="2026-04-01 13:08:27 ET",
            decision_id="decision_7c9461a2b39f47e18add6ab1cfbfa444",
            symbol="BTC-USDT-SWAP",
            leg="long",
            order_side="buy",
            exchange="OKX",
            product_type="derivatives",
            margin_mode="cross",
            expected_slippage_bps=Decimal("5.6"),
            snapshot_ts="2026-04-01T13:08:26.844821-04:00",
            best_bid=Decimal("68934.9"),
            best_ask=Decimal("68935"),
            last_price=Decimal("68934.9"),
            bid_size=Decimal("1550.38"),
            ask_size=Decimal("488.82"),
            expected_signal_edge_bps=43.69272701796539,
            expected_cost_bps=10.6,
            expected_net_edge_bps=28.592727017965387,
        ),
    )


def _replay_settings():
    return make_derivatives_hedge_settings(
        trade_cost_derivatives_taker_fee_bps=5.0,
        trade_cost_derivatives_slippage_bps=5.6,
        strategy_edge_noise_buffer_bps=4.5,
        strategy_hedge_independent_max_acceptable_cost_bps=7.5,
        strategy_hedge_independent_min_safe_net_edge_bps=3.0,
        strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
        strategy_hedge_independent_expected_execution_buffer_bps=2.0,
        strategy_hedge_independent_weak_edge_execution_mode="report_only",
    )


def _market_snapshot(case: _LiveReplayCase) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=case.symbol,
        exchange=case.exchange,
        snapshot_ts=datetime.fromisoformat(case.snapshot_ts),
        best_bid=case.best_bid,
        best_ask=case.best_ask,
        last_price=case.last_price,
        bid_size=case.bid_size,
        ask_size=case.ask_size,
        volume_24h=Decimal("0"),
        kline_15m={"open": case.last_price, "high": case.best_ask, "low": case.best_bid, "close": case.last_price},
        kline_1h={"open": case.last_price, "high": case.best_ask, "low": case.best_bid, "close": case.last_price},
        orderbook_depth=case.orderbook_depth or {"bids": [], "asks": []},
    )


def _replay_expectancy(
    *,
    case: _LiveReplayCase,
    settings,
    quantity: Decimal = Decimal("0.01"),
    use_live_recorded_cost: bool = True,
) -> tuple[IndependentBookExpectancy, object]:
    service = TradeCostService(settings=settings)
    estimate = service.estimate_single_leg_entry(
        model_name=f"{case.symbol.lower()}_{case.leg}_live_replay",
        symbol=case.symbol,
        product_type=case.product_type,
        margin_mode=case.margin_mode,
        execution_style="taker",
        order_type="market",
        side=case.order_side,
        quantity=quantity,
        market_snapshot=_market_snapshot(case),
        expected_slippage_bps=case.expected_slippage_bps,
        include_funding=case.product_type == "derivatives",
    )
    size_impact_bps = float(estimate.execution_drag_components_bps.get("size_impact_bps", Decimal("0")))
    expected_slippage_bps = float(
        estimate.executable_slippage_bps
        + estimate.execution_drag_components_bps.get("size_impact_bps", Decimal("0"))
    )
    expected_cost_bps = case.expected_cost_bps if use_live_recorded_cost else float(estimate.executable_total_drag_bps)
    expected_net_edge_bps = (
        case.expected_net_edge_bps
        if use_live_recorded_cost
        else (case.expected_signal_edge_bps - expected_cost_bps - float(settings.strategy_edge_noise_buffer_bps))
    )
    expectancy = IndependentBookExpectancy(
        leg=case.leg,
        expected_signal_edge_bps=case.expected_signal_edge_bps,
        expected_slippage_bps=expected_slippage_bps,
        expected_cost_bps=expected_cost_bps,
        expected_net_edge_bps=expected_net_edge_bps,
        depth_consumption_ratio=float(estimate.execution_context.get("depth_consumption_ratio", Decimal("0"))),
        size_impact_bps=size_impact_bps,
        cost_confidence=float(estimate.cost_confidence),
    )
    return expectancy, estimate


class TestIndependentLiveReplayCalibration(unittest.TestCase):
    def test_replay_helper_honors_case_execution_metadata(self) -> None:
        settings = _replay_settings()
        case = _LiveReplayCase(
            label="synthetic ETH spot helper regression",
            decision_id="synthetic_eth_spot_helper_regression",
            symbol="ETH-USDT",
            leg="long",
            order_side="buy",
            exchange="OKX",
            product_type="spot",
            margin_mode="cash",
            expected_slippage_bps=Decimal("1.2"),
            snapshot_ts="2026-04-01T13:00:00-04:00",
            best_bid=Decimal("3480.1"),
            best_ask=Decimal("3480.2"),
            last_price=Decimal("3480.1"),
            bid_size=Decimal("85"),
            ask_size=Decimal("62"),
            expected_signal_edge_bps=18.0,
            expected_cost_bps=6.2,
            expected_net_edge_bps=7.3,
        )

        _, estimate = _replay_expectancy(
            case=case,
            settings=settings,
            quantity=Decimal("0.15"),
            use_live_recorded_cost=False,
        )

        self.assertEqual(estimate.executable_slippage_bps, Decimal("1.2"))
        self.assertEqual(estimate.funding_cost_bps, Decimal("0"))
        self.assertNotIn("funding_account_proxy_total", estimate.cost_source_flags)

    def test_recent_btc_live_short_window_samples_do_not_trip_cost_fuse(self) -> None:
        settings = _replay_settings()
        context = make_context(product_type="derivatives", current_exposure_side="flat")

        for case in _btc_live_replay_short_cases():
            with self.subTest(case=case.label, decision_id=case.decision_id):
                expectancy, estimate = _replay_expectancy(case=case, settings=settings)
                eligibility = evaluate_open_eligibility(
                    settings=settings,
                    context=context,
                    leg="short",
                    expectancy=expectancy,
                )
                fuse_margin_bps = float(eligibility.effective_max_cost_bps or 0.0) - case.expected_cost_bps

                self.assertNotIn(
                    "independent_short_book_expected_cost_above_max_acceptable",
                    eligibility.hard_block_reasons,
                )
                self.assertGreater(expectancy.depth_consumption_ratio or 0.0, 0.0)
                self.assertLess(expectancy.depth_consumption_ratio or 0.0, 0.12)
                self.assertGreaterEqual(expectancy.size_impact_bps, 0.05)
                self.assertLess(expectancy.size_impact_bps, 1.5)
                self.assertGreater(fuse_margin_bps, 2.0)
                self.assertLess(fuse_margin_bps, 3.0)
                self.assertEqual(estimate.cost_confidence, 0.65)

    def test_recent_btc_live_long_window_sample_no_longer_trips_cost_fuse(self) -> None:
        settings = _replay_settings()
        context = make_context(product_type="derivatives", current_exposure_side="flat")

        for case in _btc_live_replay_long_cases():
            with self.subTest(case=case.label, decision_id=case.decision_id):
                expectancy, estimate = _replay_expectancy(case=case, settings=settings)
                eligibility = evaluate_open_eligibility(
                    settings=settings,
                    context=context,
                    leg="long",
                    expectancy=expectancy,
                )
                fuse_margin_bps = float(eligibility.effective_max_cost_bps or 0.0) - case.expected_cost_bps

                self.assertNotIn(
                    "independent_long_book_expected_cost_above_max_acceptable",
                    eligibility.hard_block_reasons,
                )
                self.assertGreater(expectancy.depth_consumption_ratio or 0.0, 0.0)
                self.assertLess(expectancy.depth_consumption_ratio or 0.0, 0.001)
                self.assertGreater(expectancy.size_impact_bps, 0.0)
                self.assertLess(expectancy.size_impact_bps, 0.05)
                self.assertGreater(fuse_margin_bps, 2.7)
                self.assertLess(fuse_margin_bps, 2.9)
                self.assertEqual(estimate.cost_confidence, 0.65)

    def test_thinnest_live_snapshot_tightens_fuse_without_reblocking_real_order_size(self) -> None:
        settings = _replay_settings()
        case = min(_btc_live_replay_short_cases(), key=lambda item: item.bid_size)
        replay_expectancy, _ = _replay_expectancy(case=case, settings=settings)
        baseline_expectancy = IndependentBookExpectancy(
            leg="short",
            expected_signal_edge_bps=case.expected_signal_edge_bps,
            expected_slippage_bps=5.6,
            expected_cost_bps=case.expected_cost_bps,
            expected_net_edge_bps=case.expected_net_edge_bps,
            depth_consumption_ratio=0.0,
            size_impact_bps=0.0,
            cost_confidence=replay_expectancy.cost_confidence,
        )

        replay_fuse = anomaly_cost_fuse_threshold_bps(settings=settings, expectancy=replay_expectancy)
        baseline_fuse = anomaly_cost_fuse_threshold_bps(settings=settings, expectancy=baseline_expectancy)

        self.assertIsNotNone(replay_fuse)
        self.assertIsNotNone(baseline_fuse)
        self.assertLess(float(replay_fuse or 0.0), float(baseline_fuse or 0.0))
        self.assertLess(float((baseline_fuse or 0.0) - (replay_fuse or 0.0)), 1.0)
        self.assertGreater(float(replay_fuse or 0.0), case.expected_cost_bps)

    def test_live_replay_confidence_penalty_tightens_fuse_by_configured_weight(self) -> None:
        settings = _replay_settings()
        case = _btc_live_replay_short_cases()[0]
        replay_expectancy, _ = _replay_expectancy(case=case, settings=settings)
        low_confidence_expectancy = replace(replay_expectancy, cost_confidence=0.45)

        replay_fuse = anomaly_cost_fuse_threshold_bps(settings=settings, expectancy=replay_expectancy)
        low_confidence_fuse = anomaly_cost_fuse_threshold_bps(
            settings=settings,
            expectancy=low_confidence_expectancy,
        )
        expected_penalty_delta = (0.60 - 0.45) * float(settings.strategy_hedge_independent_max_acceptable_cost_bps) * 0.75

        self.assertIsNotNone(replay_fuse)
        self.assertIsNotNone(low_confidence_fuse)
        self.assertLess(float(low_confidence_fuse or 0.0), float(replay_fuse or 0.0))
        self.assertAlmostEqual(
            float((replay_fuse or 0.0) - (low_confidence_fuse or 0.0)),
            expected_penalty_delta,
            places=6,
        )
        self.assertGreater(float(low_confidence_fuse or 0.0), case.expected_cost_bps)

    def test_thin_live_snapshot_depth_penalty_scales_with_real_replay_depth_ratio(self) -> None:
        settings = _replay_settings()
        case = min(_btc_live_replay_short_cases(), key=lambda item: item.bid_size)
        replay_expectancy, _ = _replay_expectancy(
            case=case,
            settings=settings,
            quantity=Decimal("0.03"),
            use_live_recorded_cost=False,
        )
        no_depth_penalty_expectancy = replace(replay_expectancy, depth_consumption_ratio=0.25)

        replay_fuse = anomaly_cost_fuse_threshold_bps(settings=settings, expectancy=replay_expectancy)
        no_depth_penalty_fuse = anomaly_cost_fuse_threshold_bps(
            settings=settings,
            expectancy=no_depth_penalty_expectancy,
        )
        expected_penalty_delta = (
            (replay_expectancy.depth_consumption_ratio or 0.0) - 0.25
        ) * max(float(settings.strategy_hedge_independent_max_acceptable_cost_bps) * 1.25, 2.0)

        self.assertIsNotNone(replay_fuse)
        self.assertIsNotNone(no_depth_penalty_fuse)
        self.assertGreater(replay_expectancy.depth_consumption_ratio or 0.0, 0.25)
        self.assertLess(float(replay_fuse or 0.0), float(no_depth_penalty_fuse or 0.0))
        self.assertAlmostEqual(
            float((no_depth_penalty_fuse or 0.0) - (replay_fuse or 0.0)),
            expected_penalty_delta,
            places=6,
        )

    def test_thinnest_live_snapshot_blocks_stress_size_at_current_max_position(self) -> None:
        settings = _replay_settings()
        case = min(_btc_live_replay_short_cases(), key=lambda item: item.bid_size)
        expectancy, _ = _replay_expectancy(
            case=case,
            settings=settings,
            quantity=Decimal("0.02"),
            use_live_recorded_cost=False,
        )

        fuse = anomaly_cost_fuse_threshold_bps(settings=settings, expectancy=expectancy)

        self.assertIsNotNone(fuse)
        self.assertGreater(expectancy.depth_consumption_ratio or 0.0, 0.2)
        self.assertGreater(expectancy.size_impact_bps, 1.8)
        self.assertLessEqual(float(fuse or 0.0), expectancy.expected_cost_bps)


if __name__ == "__main__":
    unittest.main()
