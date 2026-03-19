from __future__ import annotations

import unittest

from aats.schemas.decision import AIProviderAssessmentOutput
from aats.services.ai_service.openai_provider import OpenAIProvider
from aats.services.ai_service.provider import AIProviderError


class TestOpenAISchemaGuard(unittest.TestCase):
    def test_strict_json_schema_requires_every_property(self) -> None:
        strict = OpenAIProvider._strict_json_schema(AIProviderAssessmentOutput.model_json_schema())

        self.assertEqual(
            strict["required"],
            list(strict["properties"].keys()),
        )
        self.assertNotIn("default", strict["properties"]["baseline_override_recommended"])
        self.assertNotIn("default", strict["properties"]["override_reason_codes"])

    def test_schema_guard_rejects_missing_required_keys(self) -> None:
        bad_schema = {
            "type": "object",
            "properties": {
                "foo": {"type": "string"},
                "bar": {"type": "string"},
            },
            "required": ["foo"],
        }

        with self.assertRaises(AIProviderError) as ctx:
            OpenAIProvider._assert_openai_compatible_schema(bad_schema)

        self.assertIn("openai_schema_invalid", str(ctx.exception))
        self.assertIn("required_missing_keys=bar", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
