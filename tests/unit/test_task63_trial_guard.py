from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.metrics import MetricsRegistry
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.operator import OperatorActionRecord
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.trial_guard import ForwardTrialGuardService
from aats.storage.event_store import InMemoryEventStore


class TestForwardTrialGuardService(unittest.TestCase):
    def test_trial_guard_halts_when_daily_loss_threshold_breached(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 2,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_daily_loss_usdt": 20.0,
                "trial_guard_max_consecutive_losses": 4,
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 3,
                    "fee_to_notional_ratio": Decimal("0.0005"),
                },
                "recent_closed_fills": [
                    {"realized_pnl_delta": Decimal("-8"), "ingestion_timestamp": now},
                    {"realized_pnl_delta": Decimal("-9"), "ingestion_timestamp": now - timedelta(minutes=2)},
                    {"realized_pnl_delta": Decimal("-7"), "ingestion_timestamp": now - timedelta(minutes=5)},
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 0,
                    "slow_submit_to_fill_count": 0,
                }
            },
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["status"], "breached")
        self.assertTrue(snapshot["halted"])
        self.assertEqual(service.kill_switch.status()["reason"], "trial_guard_threshold_breached")
        self.assertTrue(any(item["code"] == "trial_guard_daily_loss_limit" for item in snapshot["breaches"]))
        self.assertTrue(snapshot["hard_stop"]["active"])
        self.assertFalse(snapshot["recovery_requirements"]["resume_allowed"])
        self.assertTrue(any(item["title"] for item in snapshot["breaches"]))

    def test_trial_guard_stays_warming_up_before_minimum_fill_count(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 5,
                "trial_guard_lookback_fills": 10,
            }
        )
        kill_switch = KillSwitch()
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=kill_switch,
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 2,
                    "fee_to_notional_ratio": Decimal("0.0004"),
                },
                "recent_closed_fills": [
                    {"realized_pnl_delta": Decimal("-50"), "ingestion_timestamp": utc_now()},
                    {"realized_pnl_delta": Decimal("-50"), "ingestion_timestamp": utc_now()},
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 2,
                    "slow_submit_to_fill_count": 2,
                }
            },
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["status"], "warming_up")
        self.assertFalse(kill_switch.halted)
        self.assertEqual(snapshot["breaches"], [])
        self.assertFalse(snapshot["hard_stop"]["active"])
        self.assertTrue(snapshot["recovery_requirements"]["resume_allowed"])

    def test_trial_guard_counts_funding_fee_into_daily_loss_limit(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "config_profile": "forward_test_small_capital",
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_daily_loss_usdt": 20.0,
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 1,
                    "fee_to_notional_ratio": Decimal("0.0005"),
                },
                "recent_closed_fills": [
                    {"realized_pnl_delta": Decimal("8"), "ingestion_timestamp": now - timedelta(minutes=30)},
                ],
                "recent_realized_events": [
                    {
                        "event_kind": "fill_realization",
                        "trading_net_realized_delta": Decimal("8"),
                        "event_timestamp": now - timedelta(minutes=30),
                    },
                    {
                        "event_kind": "funding_fee",
                        "funding_fee_delta": Decimal("-40"),
                        "event_timestamp": now - timedelta(minutes=5),
                    },
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 0,
                    "slow_submit_to_fill_count": 0,
                }
            },
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["status"], "breached")
        self.assertEqual(snapshot["daily_trading_net_realized"], Decimal("8"))
        self.assertEqual(snapshot["daily_funding_fee_net"], Decimal("-40"))
        self.assertEqual(snapshot["daily_combined_net_realized"], Decimal("-32"))
        self.assertTrue(any(item["code"] == "trial_guard_daily_loss_limit" for item in snapshot["breaches"]))

    def test_trial_guard_prefers_closed_fill_anomaly_counts_from_profitability_summary(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_high_slippage_ratio": 0.5,
                "trial_guard_max_slow_submit_to_fill_ratio": 0.5,
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 1,
                    "fee_to_notional_ratio": Decimal("0.0005"),
                    "high_slippage_count": 0,
                    "slow_submit_to_fill_count": 0,
                },
                "recent_closed_fills": [
                    {
                        "fill_id": "close_fill_1",
                        "realized_pnl_delta": Decimal("2"),
                        "ingestion_timestamp": now - timedelta(minutes=1),
                    },
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 1,
                    "slow_submit_to_fill_count": 1,
                },
                "rows": [
                    {
                        "fill_id": "open_fill_1",
                        "ingestion_timestamp": now - timedelta(minutes=2),
                        "anomaly_flags": ["high_adverse_slippage", "slow_submit_to_fill"],
                    }
                ],
            },
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["status"], "monitoring")
        self.assertFalse(any(item["code"] == "trial_guard_high_slippage_ratio" for item in snapshot["breaches"]))
        self.assertFalse(any(item["code"] == "trial_guard_slow_fill_ratio" for item in snapshot["breaches"]))

    def test_trial_guard_marks_recovered_after_breach_is_cleared(self) -> None:
        now = utc_now()
        kill_switch = KillSwitch()
        kill_switch.halt(reason="trial_guard_threshold_breached")
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_daily_loss_usdt": 20.0,
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=kill_switch,
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 2,
                    "fee_to_notional_ratio": Decimal("0.0001"),
                },
                "recent_closed_fills": [
                    {"realized_pnl_delta": Decimal("4"), "ingestion_timestamp": now - timedelta(minutes=4)},
                    {"realized_pnl_delta": Decimal("2"), "ingestion_timestamp": now - timedelta(minutes=8)},
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 0,
                    "slow_submit_to_fill_count": 0,
                }
            },
            last_snapshot={"status": "breached"},
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["status"], "recovered")
        self.assertFalse(snapshot["hard_stop"]["active"])
        self.assertTrue(snapshot["recovery_requirements"]["resume_allowed"])

    def test_trial_guard_is_inactive_when_runtime_is_not_trial_observation_flow(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "paper_live",
                "config_profile": "local_demo",
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            event_store=InMemoryEventStore(),
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {"summary": {"closed_fill_count": 0}},
            anomaly_provider=lambda _limit: {"summary": {}},
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["status"], "inactive_for_runtime")
        self.assertTrue(snapshot["enabled"])
        self.assertFalse(snapshot["enabled_for_runtime"])
        self.assertFalse(snapshot["trial_observation_active"])
        self.assertTrue(snapshot["recovery_requirements"]["resume_allowed"])

    def test_manual_reset_restarts_trial_guard_window_from_new_cutoff(self) -> None:
        now = utc_now()
        kill_switch = KillSwitch()
        kill_switch.halt(reason="trial_guard_threshold_breached")
        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_consecutive_losses": 1,
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=kill_switch,
            event_store=event_store,
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 1,
                    "fee_to_notional_ratio": Decimal("0.0005"),
                },
                "recent_closed_fills": [
                    {"realized_pnl_delta": Decimal("-5"), "ingestion_timestamp": now - timedelta(minutes=5)},
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 1,
                    "slow_submit_to_fill_count": 1,
                }
            },
        )

        breached = service.evaluate_now()
        self.assertEqual(breached["status"], "breached")

        effective_after = now
        event_store.append(
            build_envelope(
                topic=topics.OPERATOR_ACTIONS,
                key="trial_guard",
                payload_model=OperatorActionRecord(
                    action="trial_guard_manual_reset",
                    actor_role="admin",
                    reason="test_trial_guard_manual_reset",
                    status="reset_recorded",
                    details={
                        "trial_review_action_type": "reset_trial_guard",
                        "effective_after": effective_after,
                        "product_type": "spot",
                        "margin_mode": "cash",
                        "allowed_symbols": ["BTC-USDT"],
                    },
                ),
                source_component="test",
            )
        )

        reset_snapshot = service.evaluate_now()

        self.assertEqual(reset_snapshot["status"], "warming_up")
        self.assertFalse(reset_snapshot["hard_stop"]["active"])
        self.assertTrue(reset_snapshot["manual_reset_active"])
        self.assertEqual(reset_snapshot["manual_reset_effective_after"], effective_after)
        self.assertEqual(reset_snapshot["fill_count"], 0)
        self.assertIsNone(reset_snapshot["fee_to_notional_ratio"])
        self.assertTrue(reset_snapshot["halted"])
        self.assertTrue(reset_snapshot["recovery_requirements"]["resume_allowed"])

    def test_manual_reset_is_scoped_to_current_runtime(self) -> None:
        now = utc_now()
        event_store = InMemoryEventStore()
        derivative_service = ForwardTrialGuardService(
            settings=AATSSettings.model_validate(
                {
                    "trial_guard_enabled": True,
                    "mode": "guarded_live",
                    "trial_guard_min_closed_fills": 1,
                    "trial_guard_lookback_fills": 10,
                    "trial_guard_max_consecutive_losses": 1,
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ["BTC-USDT-SWAP"],
                }
            ),
            kill_switch=KillSwitch(),
            event_store=event_store,
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {"closed_fill_count": 1},
                "recent_closed_fills": [{"realized_pnl_delta": Decimal("-3"), "ingestion_timestamp": now - timedelta(minutes=5)}],
            },
            anomaly_provider=lambda _limit: {"summary": {}},
        )
        spot_service = ForwardTrialGuardService(
            settings=AATSSettings.model_validate(
                {
                    "trial_guard_enabled": True,
                    "mode": "guarded_live",
                    "trial_guard_min_closed_fills": 1,
                    "trial_guard_lookback_fills": 10,
                    "trial_guard_max_consecutive_losses": 1,
                    "trading_product_type": "spot",
                    "margin_mode": "cash",
                    "default_symbol": "ETH-USDT",
                    "allowed_symbols": ["ETH-USDT"],
                }
            ),
            kill_switch=KillSwitch(),
            event_store=event_store,
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {"closed_fill_count": 1},
                "recent_closed_fills": [{"realized_pnl_delta": Decimal("-2"), "ingestion_timestamp": now - timedelta(minutes=5)}],
            },
            anomaly_provider=lambda _limit: {"summary": {}},
        )

        self.assertEqual(derivative_service.evaluate_now()["status"], "breached")
        self.assertEqual(spot_service.evaluate_now()["status"], "breached")

        event_store.append(
            build_envelope(
                topic=topics.OPERATOR_ACTIONS,
                key="trial_guard",
                payload_model=OperatorActionRecord(
                    action="trial_guard_manual_reset",
                    actor_role="admin",
                    reason="test_scoped_manual_reset",
                    status="reset_recorded",
                    details={
                        "trial_review_action_type": "reset_trial_guard",
                        "effective_after": now,
                        "product_type": "derivatives",
                        "margin_mode": "cross",
                        "allowed_symbols": ["BTC-USDT-SWAP"],
                    },
                ),
                source_component="test",
            )
        )

        self.assertEqual(derivative_service.evaluate_now()["status"], "warming_up")
        self.assertEqual(spot_service.evaluate_now()["status"], "breached")

    def test_manual_reset_recomputes_fee_and_anomaly_metrics_from_filtered_rows(self) -> None:
        now = utc_now()
        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_fee_to_notional_ratio": 0.01,
                "trial_guard_max_high_slippage_ratio": 0.5,
                "trial_guard_max_slow_submit_to_fill_ratio": 0.5,
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ["BTC-USDT"],
                "trading_product_type": "spot",
                "margin_mode": "cash",
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            event_store=event_store,
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 2,
                    "fee_to_notional_ratio": Decimal("0.0001"),
                },
                "recent_closed_fills": [
                    {
                        "realized_pnl_delta": Decimal("-1"),
                        "fee_amount": Decimal("0.1"),
                        "fill_notional": Decimal("100"),
                        "ingestion_timestamp": now - timedelta(minutes=10),
                    },
                    {
                        "realized_pnl_delta": Decimal("1"),
                        "fee_amount": Decimal("2"),
                        "fill_notional": Decimal("100"),
                        "ingestion_timestamp": now - timedelta(minutes=1),
                    },
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {
                    "high_slippage_count": 0,
                    "slow_submit_to_fill_count": 0,
                },
                "rows": [
                    {
                        "ingestion_timestamp": now - timedelta(minutes=10),
                        "anomaly_flags": [],
                    },
                    {
                        "ingestion_timestamp": now - timedelta(minutes=1),
                        "anomaly_flags": ["high_adverse_slippage", "slow_submit_to_fill"],
                    },
                ],
            },
        )

        event_store.append(
            build_envelope(
                topic=topics.OPERATOR_ACTIONS,
                key="trial_guard",
                payload_model=OperatorActionRecord(
                    action="trial_guard_manual_reset",
                    actor_role="admin",
                    reason="test_manual_reset_metric_recompute",
                    status="reset_recorded",
                    details={
                        "trial_review_action_type": "reset_trial_guard",
                        "effective_after": now - timedelta(minutes=2),
                        "product_type": "spot",
                        "margin_mode": "cash",
                        "allowed_symbols": ["BTC-USDT"],
                    },
                ),
                source_component="test",
            )
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["fill_count"], 1)
        self.assertEqual(snapshot["fee_to_notional_ratio"], Decimal("0.02"))
        self.assertEqual(snapshot["high_slippage_ratio"], 1.0)
        self.assertEqual(snapshot["slow_submit_to_fill_ratio"], 1.0)
        self.assertTrue(any(item["code"] == "trial_guard_fee_drag_limit" for item in snapshot["breaches"]))
        self.assertTrue(any(item["code"] == "trial_guard_high_slippage_ratio" for item in snapshot["breaches"]))
        self.assertTrue(any(item["code"] == "trial_guard_slow_fill_ratio" for item in snapshot["breaches"]))

    def test_manual_reset_recomputes_fee_ratio_from_fee_delta_in_quote(self) -> None:
        now = utc_now()
        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
                "mode": "guarded_live",
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 10,
                "trial_guard_max_fee_to_notional_ratio": 0.01,
                "default_symbol": "BTC-USDT",
                "allowed_symbols": ["BTC-USDT"],
                "trading_product_type": "spot",
                "margin_mode": "cash",
            }
        )
        service = ForwardTrialGuardService(
            settings=settings,
            kill_switch=KillSwitch(),
            event_store=event_store,
            metrics=MetricsRegistry(),
            profitability_provider=lambda _limit: {
                "summary": {
                    "closed_fill_count": 1,
                    "fee_to_notional_ratio": Decimal("0.00001"),
                },
                "recent_closed_fills": [
                    {
                        "realized_pnl_delta": Decimal("1"),
                        "symbol": "BTC-USDT",
                        "side": "buy",
                        "venue": "OKX",
                        "fee_amount": Decimal("0.001"),
                        "fee_delta": Decimal("2"),
                        "fee_currency": "BTC",
                        "fill_price": Decimal("2000"),
                        "fill_notional": Decimal("100"),
                        "ingestion_timestamp": now - timedelta(minutes=1),
                    },
                ],
            },
            anomaly_provider=lambda _limit: {
                "summary": {"high_slippage_count": 0, "slow_submit_to_fill_count": 0},
                "rows": [],
            },
        )

        event_store.append(
            build_envelope(
                topic=topics.OPERATOR_ACTIONS,
                key="trial_guard",
                payload_model=OperatorActionRecord(
                    action="trial_guard_manual_reset",
                    actor_role="admin",
                    reason="test_manual_reset_fee_delta_quote_ratio",
                    status="reset_recorded",
                    details={
                        "trial_review_action_type": "reset_trial_guard",
                        "effective_after": now - timedelta(minutes=2),
                        "product_type": "spot",
                        "margin_mode": "cash",
                        "allowed_symbols": ["BTC-USDT"],
                    },
                ),
                source_component="test",
            )
        )

        snapshot = service.evaluate_now()

        self.assertEqual(snapshot["fill_count"], 1)
        self.assertEqual(snapshot["fee_to_notional_ratio"], Decimal("0.02"))
        self.assertTrue(any(item["code"] == "trial_guard_fee_drag_limit" for item in snapshot["breaches"]))


if __name__ == "__main__":
    unittest.main()
