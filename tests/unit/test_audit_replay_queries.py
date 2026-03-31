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
