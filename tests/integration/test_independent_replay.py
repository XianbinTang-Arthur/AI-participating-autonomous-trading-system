from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from aats.schemas.decision import DecisionContext, DecisionOutcome, PositionTarget


def _adaptive_family_execution_summary(*, live_applied: bool) -> dict[str, object]:
    return {
        "summary_mode": "multi_leg",
        "family": "independent",
        "route_action": "override_target",
        "family_action": "open_independent_book",
        "leg_count": 2,
        "position_intents": ["open_long", "inactive"],
        "directions": ["long", "short"],
        "leg_actions": ["open", "inactive"],
        "execution_modes": ["independent_long_book", "independent_short_book"],
        "book_runtime_states": [
            {
                "leg": "long",
                "current_qty": "0",
                "target_qty": "0.02",
                "state": "opening",
                "score": 0.82,
                "score_raw": 0.82,
                "score_adjusted": 0.82,
                "book_state": "probing",
                "holding_phase": "entry",
                "health_state": "ok",
                "eligibility_state": "eligible",
                "book_action": "open",
                "reason_codes": ["independent_long_book_signal_above_entry_threshold"],
                "blocked_reasons": [],
                "size_multiplier": 0.73,
                "capital_multiplier": 0.73,
                "current_scale_in_count": 0,
                "current_de_risk_count": 0,
                "threshold_snapshot": {
                    "leg": "long",
                    "shadow_only": not live_applied,
                    "rollout_enabled": True,
                    "live_applied": live_applied,
                    "health_enforcement_enabled": True,
                    "size_down_entry_enabled": True,
                    "long_short_asymmetry_enabled": True,
                    "entry_threshold": 0.60,
                    "close_threshold": 0.48,
                    "scale_in_threshold": 0.90,
                    "thesis_age_seconds": 1800.0,
                    "de_risk_net_edge_bps": 2.0,
                    "adaptive_entry_threshold": 0.66,
                    "adaptive_close_threshold": 0.50,
                    "adaptive_scale_in_threshold": 0.96,
                    "adaptive_thesis_age_seconds": 1500.0,
                    "adaptive_de_risk_net_edge_bps": 2.6,
                    "effective_entry_threshold": 0.66 if live_applied else 0.60,
                    "effective_close_threshold": 0.50 if live_applied else 0.48,
                    "effective_scale_in_threshold": 0.96 if live_applied else 0.90,
                    "effective_thesis_age_seconds": 1500.0 if live_applied else 1800.0,
                    "effective_de_risk_net_edge_bps": 2.6 if live_applied else 2.0,
                    "capital_multiplier": 0.73,
                    "confidence_multiplier": 0.92,
                    "volatility_multiplier": 0.88,
                    "liquidity_multiplier": 0.95,
                    "health_multiplier": 1.0,
                    "direction_bias_multiplier": 1.0,
                    "reason_codes": [
                        "adaptive_shadow_confidence_adjusted",
                        "independent_long_book_size_down_entry_enabled",
                    ],
                },
            },
            {
                "leg": "short",
                "current_qty": "0",
                "target_qty": "0",
                "state": "inactive",
                "score": 0.11,
                "score_raw": 0.11,
                "score_adjusted": 0.11,
                "book_state": "flat",
                "holding_phase": None,
                "health_state": "ok",
                "eligibility_state": None,
                "book_action": "inactive",
                "reason_codes": ["independent_short_book_signal_below_entry_threshold"],
                "blocked_reasons": [],
                "current_scale_in_count": 0,
                "current_de_risk_count": 0,
                "threshold_snapshot": {
                    "leg": "short",
                    "shadow_only": not live_applied,
                    "rollout_enabled": True,
                    "live_applied": live_applied,
                    "health_enforcement_enabled": True,
                    "size_down_entry_enabled": True,
                    "long_short_asymmetry_enabled": True,
                    "entry_threshold": 0.60,
                    "close_threshold": 0.48,
                    "scale_in_threshold": 0.90,
                    "thesis_age_seconds": 1800.0,
                    "de_risk_net_edge_bps": 2.0,
                    "adaptive_entry_threshold": 0.68,
                    "adaptive_close_threshold": 0.50,
                    "adaptive_scale_in_threshold": 0.97,
                    "adaptive_thesis_age_seconds": 1500.0,
                    "adaptive_de_risk_net_edge_bps": 2.7,
                    "effective_entry_threshold": 0.68 if live_applied else 0.60,
                    "effective_close_threshold": 0.50 if live_applied else 0.48,
                    "effective_scale_in_threshold": 0.97 if live_applied else 0.90,
                    "effective_thesis_age_seconds": 1500.0 if live_applied else 1800.0,
                    "effective_de_risk_net_edge_bps": 2.7 if live_applied else 2.0,
                    "capital_multiplier": 0.61,
                    "confidence_multiplier": 0.89,
                    "volatility_multiplier": 0.87,
                    "liquidity_multiplier": 0.93,
                    "health_multiplier": 1.0,
                    "direction_bias_multiplier": 0.85,
                    "reason_codes": [
                        "adaptive_shadow_confidence_adjusted",
                        "independent_short_book_asymmetry_penalty_applied",
                    ],
                },
            },
        ],
    }


class TestIndependentReplayIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_replay_validation_surfaces_independent_adaptive_summary(self) -> None:
        runtime = await self._runtime(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_family_independent_enabled=True,
            strategy_hedge_independent_adaptive_rollout_enabled=True,
            strategy_hedge_independent_health_enforcement_enabled=True,
            strategy_hedge_independent_size_down_entry_enabled=True,
            strategy_hedge_independent_long_short_asymmetry_enabled=True,
        )
        decision_id = "decision_independent_replay_adaptive"
        self._append_independent_decision(runtime, decision_id=decision_id, live_applied=True)
        app = self._app(runtime)

        with TestClient(app) as client:
            validation = client.post(f"/replay/validate/{decision_id}").json()
            recent = client.get("/replay/recent-validations?limit=5").json()

        self.assertIsNotNone(validation["independent_adaptive_summary"])
        self.assertTrue(validation["independent_adaptive_summary"]["live_applied"])
        self.assertTrue(validation["independent_adaptive_summary"]["rollout_enabled"])
        self.assertEqual(
            validation["independent_adaptive_summary"]["long_leg"]["effective_entry_threshold"],
            0.66,
        )
        self.assertEqual(
            validation["independent_adaptive_summary"]["short_leg"]["direction_bias_multiplier"],
            0.85,
        )
        recent_row = next(item for item in recent["validations"] if item["decision_id"] == decision_id)
        self.assertTrue(recent_row["independent_adaptive_summary"]["live_applied"])
        self.assertIn(
            "independent_short_book_asymmetry_penalty_applied",
            recent_row["independent_adaptive_summary"]["reason_codes"],
        )

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

    @staticmethod
    def _append_independent_decision(runtime, *, decision_id: str, live_applied: bool) -> None:
        now = datetime.now(timezone.utc)
        decision_context = DecisionContext(
            decision_id=decision_id,
            symbol=runtime.settings.default_symbol,
            timeframe=runtime.settings.primary_timeframe,
            as_of_ts=now,
            market_snapshot_ref="evt_market_snapshot_independent_replay",
            feature_snapshot_ref="evt_feature_snapshot_independent_replay",
            portfolio_snapshot_ref="evt_portfolio_snapshot_independent_replay",
            health_snapshot_ref="evt_health_snapshot_independent_replay",
            mode=runtime.settings.mode,
            current_position_qty=Decimal("0"),
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            current_exposure_side="flat",
        )
        family_execution_summary = _adaptive_family_execution_summary(live_applied=live_applied)
        decision_outcome = DecisionOutcome(
            decision_id=decision_id,
            symbol=runtime.settings.default_symbol,
            decision_source="baseline",
            decision_authority="reference_only",
            finalized=True,
            final_direction="long",
            final_action="enter",
            final_target_qty=Decimal("0.02"),
            selected_strategy_family="independent",
            selected_strategy_family_action="open_independent_book",
            selected_strategy_route_action="override_target",
            family_execution_summary=family_execution_summary,
        )
        position_target = PositionTarget(
            decision_id=decision_id,
            symbol=runtime.settings.default_symbol,
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.02"),
            delta_position_qty=Decimal("0.02"),
            current_notional=Decimal("0"),
            target_notional=Decimal("1600"),
            rebalance_reason="independent_replay_adaptive_summary",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"independent": 1.0},
            decision_expiry_ts=now + timedelta(minutes=5),
            product_type=runtime.settings.trading_product_type,
            current_exposure_side="flat",
            target_exposure_side="long",
            position_intent="open_long",
            target_leverage=2.0,
            margin_mode=runtime.settings.margin_mode,
            strategy_family="independent",
            strategy_family_action="open_independent_book",
            strategy_route_action="override_target",
            family_execution_summary=family_execution_summary,
            decision_outcome=decision_outcome,
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key=runtime.settings.default_symbol,
            payload_model=decision_context,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key=runtime.settings.default_symbol,
            payload_model=position_target,
            source_component="test",
        )
        runtime.event_store.append(context_event)
        runtime.event_store.append(target_event)
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id=decision_id,
                decision_context_ref=context_event.event_id,
                position_target_ref=target_event.event_id,
            )
        )


if __name__ == "__main__":
    unittest.main()
