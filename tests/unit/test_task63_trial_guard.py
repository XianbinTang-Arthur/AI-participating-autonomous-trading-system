from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.trial_guard import ForwardTrialGuardService
from aats.storage.event_store import InMemoryEventStore


class TestForwardTrialGuardService(unittest.TestCase):
    def test_trial_guard_halts_when_daily_loss_threshold_breached(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
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

    def test_trial_guard_stays_warming_up_before_minimum_fill_count(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
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

    def test_trial_guard_counts_funding_fee_into_daily_loss_limit(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "trial_guard_enabled": True,
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


if __name__ == "__main__":
    unittest.main()
