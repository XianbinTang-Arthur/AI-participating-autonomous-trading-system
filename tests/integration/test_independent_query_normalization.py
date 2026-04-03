from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.decision import DecisionContext, DecisionOutcome, PositionTarget


class TestIndependentQueryNormalizationIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_decision_endpoints_normalize_legacy_independent_runtime_states_and_replay_snapshots(self) -> None:
        runtime = await self._runtime(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        decision_id = "decision_independent_legacy_runtime_state_query_surface"
        now = utc_now()
        decision_context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            as_of_ts=now,
            market_snapshot_ref="evt_market_snapshot_independent_legacy_runtime_state_query",
            feature_snapshot_ref="evt_feature_snapshot_independent_legacy_runtime_state_query",
            portfolio_snapshot_ref="evt_portfolio_snapshot_independent_legacy_runtime_state_query",
            health_snapshot_ref="evt_health_snapshot_independent_legacy_runtime_state_query",
            mode="guarded_live",
            current_position_qty=Decimal("0.01"),
            product_type="derivatives",
            margin_mode="cross",
            current_exposure_side="flat",
        )
        family_execution_summary = {
            "summary_mode": "multi_leg",
            "family": "independent",
            "route_action": "override_target",
            "family_action": "hold_family",
            "leg_count": 2,
            "position_intents": ["hold", "hold"],
            "directions": ["long", "short"],
            "leg_actions": ["blocked", "blocked"],
            "execution_modes": ["independent_long_book", "independent_short_book"],
            "book_runtime_states": [
                {
                    "leg": "long",
                    "current_qty": "0",
                    "target_qty": "0",
                    "state": "blocked",
                    "book_state": "cooldown",
                    "book_action": "blocked",
                    "prior_book_state": "cooldown",
                    "blocked_reasons": ["independent_long_book_score_stability_below_threshold"],
                },
                {
                    "leg": "short",
                    "current_qty": "0.01",
                    "target_qty": "0.01",
                    "state": "blocked",
                    "book_state": "suspended",
                    "book_action": "blocked",
                    "prior_book_state": "suspended",
                    "blocked_reasons": ["independent_short_book_trial_guard_active"],
                },
            ],
            "long_replay_snapshot": {
                "leg": "long",
                "state": "blocked",
                "book_state": "cooldown",
                "book_action": "blocked",
                "prior_book_state": "cooldown",
            },
            "short_replay_snapshot": {
                "leg": "short",
                "state": "blocked",
                "book_state": "suspended",
                "book_action": "blocked",
                "prior_book_state": "suspended",
            },
        }
        decision_outcome = DecisionOutcome(
            decision_id=decision_id,
            symbol="BTC-USDT-SWAP",
            decision_source="baseline",
            decision_authority="reference_only",
            finalized=True,
            final_direction="flat",
            final_action="hold",
            final_target_qty=Decimal("0.01"),
            selected_strategy_family="independent",
            selected_strategy_family_action="hold_family",
            selected_strategy_route_action="override_target",
            family_execution_summary=family_execution_summary,
        )
        position_target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0.01"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0"),
            current_notional=Decimal("100"),
            target_notional=Decimal("100"),
            rebalance_reason="independent_legacy_runtime_state_query_surface",
            urgency="low",
            max_slippage_tolerance_bps=20,
            source_mix={"independent": 1.0},
            decision_expiry_ts=now + timedelta(minutes=5),
            product_type="derivatives",
            current_exposure_side="flat",
            target_exposure_side="flat",
            position_intent="hold",
            target_leverage=1.0,
            margin_mode="cross",
            strategy_family="independent",
            strategy_family_action="hold_family",
            strategy_route_action="override_target",
            family_execution_summary=family_execution_summary,
            decision_outcome=decision_outcome,
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.DECISION_CONTEXTS,
                key="BTC-USDT-SWAP",
                payload_model=decision_context,
                source_component="test",
            )
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT-SWAP",
            payload_model=position_target,
            source_component="test",
        )
        runtime.event_store.append(target_event)
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id=decision_id,
                decision_context_ref=runtime.event_store.latest(topics.DECISION_CONTEXTS).event_id,
                position_target_ref=target_event.event_id,
            )
        )

        with TestClient(self._app(runtime)) as client:
            latest = client.get("/decision/latest").json()
            detail = client.get(f"/decision/{decision_id}").json()
            recent = client.get("/decision/recent?limit=10").json()

        recent_row = next(item for item in recent["decisions"] if item["decision_id"] == decision_id)
        long_state = detail["position_target"]["book_runtime_states"][0]
        short_state = detail["position_target"]["book_runtime_states"][1]
        self.assertEqual(long_state["book_state"], "flat")
        self.assertIsNone(long_state["guard_state"])
        self.assertEqual(long_state["prior_book_state"], "flat")
        self.assertIsNone(long_state["prior_guard_state"])
        self.assertEqual(short_state["book_state"], "holding")
        self.assertEqual(short_state["guard_state"], "suspended")
        self.assertEqual(short_state["prior_book_state"], "holding")
        self.assertEqual(short_state["prior_guard_state"], "suspended")
        self.assertEqual(
            detail["position_target"]["family_execution_summary"]["book_runtime_states"][0]["book_state"],
            "flat",
        )
        self.assertEqual(
            detail["position_target"]["family_execution_summary"]["book_runtime_states"][1]["guard_state"],
            "suspended",
        )
        self.assertEqual(
            detail["decision_outcome"]["family_execution_summary"]["book_runtime_states"][0]["book_state"],
            "flat",
        )
        self.assertEqual(
            detail["ai_decision_audit"]["family_execution_summary"]["book_runtime_states"][1]["guard_state"],
            "suspended",
        )
        self.assertEqual(latest["summary"]["book_runtime_states"][0]["book_state"], "flat")
        self.assertEqual(recent_row["book_runtime_states"][1]["guard_state"], "suspended")

    async def _runtime(self, **overrides):
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "operator_unsafe_write_without_auth": True,
                **overrides,
            }
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )
        return runtime

    @staticmethod
    def _app(runtime) -> FastAPI:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(router)
        app.state.runtime = runtime
        return app


if __name__ == "__main__":
    unittest.main()
