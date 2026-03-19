from __future__ import annotations

import unittest

from aats.schemas.decision import AIProviderAssessmentOutput, AIProviderAssessmentWithExecutionSuggestionOutput
from aats.services.ai_service.openai_provider import OpenAIProvider


class TestOpenAIProviderSchemaNormalization(unittest.TestCase):
    def test_strict_json_schema_marks_all_properties_required(self) -> None:
        schema = OpenAIProvider._strict_json_schema(AIProviderAssessmentOutput.model_json_schema())

        self.assertEqual(
            schema["required"],
            [
                "regime",
                "directional_edge",
                "expected_volatility",
                "confidence",
                "uncertainty",
                "expected_holding_horizon",
                "invalidation_conditions",
                "risk_tags",
                "rationale_summary",
                "baseline_override_recommended",
                "override_reason_codes",
            ],
        )
        self.assertIn("invalidation_conditions", schema["required"])
        self.assertIn("override_reason_codes", schema["required"])
        self.assertFalse(schema.get("additionalProperties", True))

    def test_schema_name_includes_schema_hash_to_avoid_stale_remote_name_reuse(self) -> None:
        schema = AIProviderAssessmentOutput.model_json_schema()

        name = OpenAIProvider._schema_name(schema)

        self.assertRegex(name, r"^AIProviderAssessmentOutput_[0-9a-f]{10}$")

    def test_extended_schema_name_differs_from_base_schema_name(self) -> None:
        base_name = OpenAIProvider._schema_name(AIProviderAssessmentOutput.model_json_schema())
        full_name = OpenAIProvider._schema_name(
            AIProviderAssessmentWithExecutionSuggestionOutput.model_json_schema()
        )

        self.assertNotEqual(base_name, full_name)


if __name__ == "__main__":
    unittest.main()
