from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from aats.services.operator.audit_replay_queries import AuditReplayQueryFacade
from aats.services.reconciliation_service.replay import ReplayResult


class TestAuditReplayQueries(TestCase):
    def test_replay_summary_prefers_replayed_target_margin_mode(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                )
            ),
            _independent_expected_vs_realized_summary=lambda **_: None,
        )

        facade = AuditReplayQueryFacade(owner)
        summary = facade._replay_summary(
            ReplayResult(
                replayed_event_count=1,
                stored_snapshot_count=1,
                divergence_count=0,
                selected_decision_id="decision_replay_margin_mode",
            ),
            symbol="BTC-USDT-SWAP",
            regime="trend",
            active_profile_id="derivatives_live",
            margin_mode="isolated",
        )

        self.assertEqual(summary["margin_mode"], "isolated")

    def test_replay_summary_includes_overlay_parent_exposure_summary(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    trading_product_type="derivatives",
                    margin_mode="cross",
                    allowed_symbols=("BTC-USDT-SWAP",),
                )
            ),
            _independent_expected_vs_realized_summary=lambda **_: None,
        )

        facade = AuditReplayQueryFacade(owner)
        summary = facade._replay_summary(
            ReplayResult(
                replayed_event_count=1,
                stored_snapshot_count=1,
                divergence_count=0,
                selected_decision_id="decision_overlay_parent",
            ),
            symbol="BTC-USDT-SWAP",
            regime="trend",
            active_profile_id="derivatives_live",
            margin_mode="cross",
            overlay_parent_exposure_summary={
                "parent_family": "directional",
                "symbol": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "source_of_truth": "inventory",
                "lifecycle_state": "inventory_only",
                "target_signal": "flat",
                "current_signal": "long",
                "effective_signal": "long",
                "target_qty": "0",
                "current_qty": "0.03",
                "effective_qty": "0.03",
                "target_active": False,
                "inventory_active": True,
            },
        )

        self.assertIsNotNone(summary["overlay_parent_exposure_summary"])
        self.assertEqual(summary["overlay_parent_exposure_summary"]["source_of_truth"], "inventory")
        self.assertEqual(summary["overlay_parent_exposure_summary"]["effective_signal"], "long")
