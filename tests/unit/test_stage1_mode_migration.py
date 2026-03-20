from __future__ import annotations

import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import (
    AIDecisionIntent,
    DecisionOutcome,
    normalize_ai_operating_mode,
)
from aats.storage.event_store import InMemoryEventStore
from aats.services.ai_service.inference import AIInferenceService
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator


class TestStage1ModeNormalization(unittest.TestCase):
    def test_legacy_modes_normalize_to_canonical_values(self) -> None:
        self.assertEqual(normalize_ai_operating_mode("baseline_only"), "baseline_only")
        self.assertEqual(normalize_ai_operating_mode("ai_advisory"), "ai_assisted")
        self.assertEqual(normalize_ai_operating_mode("ai_blended"), "ai_assisted")
        self.assertEqual(normalize_ai_operating_mode("ai_primary"), "ai_decision_maker")

    def test_unknown_mode_defaults_to_baseline_only(self) -> None:
        self.assertEqual(normalize_ai_operating_mode("unknown_mode"), "baseline_only")
        self.assertEqual(normalize_ai_operating_mode(None), "baseline_only")

    def test_settings_exposes_canonical_ai_operating_mode(self) -> None:
        settings = AATSSettings.model_validate({"ai_operating_mode": "ai_primary"})
        self.assertEqual(settings.ai_operating_mode, "ai_primary")
        self.assertEqual(settings.canonical_ai_operating_mode, "ai_decision_maker")

    def test_settings_exposes_canonical_ai_decision_threshold_aliases(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "ai_primary_min_confidence": 0.71,
                "ai_primary_max_uncertainty": 0.22,
                "ai_primary_min_directional_edge": 0.31,
            }
        )
        self.assertEqual(settings.ai_decision_min_confidence, 0.71)
        self.assertEqual(settings.ai_decision_max_uncertainty, 0.22)
        self.assertEqual(settings.ai_decision_min_directional_edge, 0.31)

    def test_settings_accepts_canonical_ai_decision_threshold_names(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "ai_decision_min_confidence": 0.67,
                "ai_decision_max_uncertainty": 0.19,
                "ai_decision_min_directional_edge": 0.27,
            }
        )
        self.assertEqual(settings.ai_decision_min_confidence, 0.67)
        self.assertEqual(settings.ai_decision_max_uncertainty, 0.19)
        self.assertEqual(settings.ai_decision_min_directional_edge, 0.27)

    def test_inference_status_exposes_canonical_modes_without_breaking_legacy_modes(self) -> None:
        settings = AATSSettings.model_validate({"ai_operating_mode": "ai_blended", "ai_provider": "disabled"})
        service = AIInferenceService(
            settings=settings,
            event_store=InMemoryEventStore(),
            prompt_builder=PromptBuilder(),
            validator=AssessmentValidator(),
        )

        status = service.status()

        self.assertEqual(status["configured_operating_mode"], "ai_blended")
        self.assertEqual(status["canonical_configured_operating_mode"], "ai_assisted")
        self.assertEqual(status["effective_operating_mode"], "ai_blended")
        self.assertEqual(status["canonical_effective_operating_mode"], "ai_assisted")


class TestStage1DecisionSchemas(unittest.TestCase):
    def test_ai_decision_intent_schema_draft_can_be_instantiated(self) -> None:
        intent = AIDecisionIntent(
            decision_id="dec_1",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            direction="long",
            action="enter",
            target_qty=Decimal("0.25"),
            confidence=0.81,
            economically_actionable=True,
            reason_codes=["ai_breakout_setup"],
            fallback_used=False,
            degraded=False,
            provider_name="openai",
            provider_request_id="req_1",
            requested_profile_id="trend_strict",
            requested_profile_reason_codes=["signal_quality_moderate"],
        )

        self.assertEqual(intent.action, "enter")
        self.assertEqual(intent.target_qty, Decimal("0.25"))
        self.assertEqual(intent.requested_profile_id, "trend_strict")

    def test_decision_outcome_schema_draft_can_be_instantiated(self) -> None:
        outcome = DecisionOutcome(
            decision_id="dec_2",
            symbol="BTC-USDT-SWAP",
            ai_operating_mode="ai_decision_maker",
            decision_source="ai",
            decision_authority="final_decision",
            final_direction="long",
            final_action="enter",
            final_target_qty=Decimal("0.22"),
            decision_blocked_reasons=[],
            guardrail_flags=[],
            policy_blocked=False,
            risk_capped=False,
            active_profile_id="trend_normal",
            profile_control_source="env_default",
            ai_fallback_used=False,
            ai_degraded=False,
        )

        self.assertEqual(outcome.decision_source, "ai")
        self.assertEqual(outcome.ai_operating_mode, "ai_decision_maker")
        self.assertEqual(outcome.final_target_qty, Decimal("0.22"))


if __name__ == "__main__":
    unittest.main()
