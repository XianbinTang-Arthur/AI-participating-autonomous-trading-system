"""P1.4-P2.7 新 alpha reason_codes glossary 注入的回归测试.

验证 prompt_builder 把 basis/funding/oi/ls 新 code 的语义解释注入给 AI.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from aats.schemas.ai_brief import AIDecisionBrief
from aats.services.ai_service.prompt_builder import REASON_CODE_GLOSSARY, PromptBuilder


def _brief(*, reason_codes: list[str]) -> AIDecisionBrief:
    return AIDecisionBrief(
        decision_id="decision_glossary_test",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        product_type="derivatives",
        margin_mode="cross",
        regime_indicator="trend",
        regime_confidence=0.7,
        composite_alpha_score=0.25,
        momentum_score=0.1,
        volatility_state="medium",
        volatility_value=0.02,
        current_position_qty=Decimal("0"),
        current_exposure_side="flat",
        current_open_order_count=0,
        baseline_direction_bias="long",
        baseline_confidence=0.6,
        baseline_reason_codes=reason_codes,
        fee_bps=Decimal("3"),
        max_slippage_tolerance_bps=Decimal("20"),
        expected_slippage_proxy_bps=Decimal("2"),
        min_net_edge_bps=Decimal("5"),
        safe_to_trade=True,
        review_required=False,
        halted=False,
        reconciliation_halt_required=False,
        market_snapshot_fresh=True,
        account_snapshot_fresh=True,
        execution_condition="normal",
    )


class TestPromptBuilderReasonCodeGlossary(unittest.TestCase):
    def test_glossary_includes_only_codes_present_in_brief(self) -> None:
        brief = _brief(
            reason_codes=[
                "baseline_multi_factor_alpha",
                "regime_trend",
                "alpha_basis_contrarian_long",
                "alpha_funding_short_bias",
            ]
        )
        prompt = PromptBuilder().build(brief=brief, operating_mode="ai_decision_maker")
        payload = json.loads(prompt)

        glossary = payload["baseline_reason_code_glossary"]
        self.assertEqual(
            set(glossary.keys()),
            {"alpha_basis_contrarian_long", "alpha_funding_short_bias"},
        )
        self.assertIn("basis", glossary["alpha_basis_contrarian_long"].lower())
        self.assertIn("funding", glossary["alpha_funding_short_bias"].lower())

    def test_glossary_is_empty_when_no_new_codes_present(self) -> None:
        brief = _brief(
            reason_codes=[
                "baseline_multi_factor_alpha",
                "regime_range",
                "alpha_momentum_support",
                "microstructure_neutral",
            ]
        )
        prompt = PromptBuilder().build(brief=brief, operating_mode="ai_decision_maker")
        payload = json.loads(prompt)

        self.assertEqual(payload["baseline_reason_code_glossary"], {})

    def test_glossary_covers_all_eight_new_p1_p2_codes(self) -> None:
        expected_codes = {
            "alpha_basis_contrarian_long",
            "alpha_basis_contrarian_short",
            "alpha_funding_long_bias",
            "alpha_funding_short_bias",
            "alpha_oi_long_confirming",
            "alpha_oi_short_confirming",
            "alpha_ls_contrarian_long",
            "alpha_ls_contrarian_short",
        }
        self.assertEqual(expected_codes, set(REASON_CODE_GLOSSARY.keys()))
        for code, explanation in REASON_CODE_GLOSSARY.items():
            self.assertTrue(explanation.strip(), f"{code} has empty explanation")

    def test_instructions_reference_glossary_usage_policy(self) -> None:
        brief = _brief(reason_codes=["alpha_basis_contrarian_long"])
        prompt = PromptBuilder().build(brief=brief, operating_mode="ai_decision_maker")
        payload = json.loads(prompt)

        requirements = payload["instructions"]["requirements"]
        glossary_rules = [req for req in requirements if "glossary" in req.lower()]
        self.assertEqual(len(glossary_rules), 1)
        self.assertIn("do not echo", glossary_rules[0].lower())

    def test_glossary_does_not_alter_unrelated_payload_fields(self) -> None:
        brief = _brief(reason_codes=["alpha_oi_long_confirming"])
        prompt = PromptBuilder().build(brief=brief, operating_mode="ai_decision_maker")
        payload = json.loads(prompt)

        self.assertEqual(payload["task"], "ai_primary_market_assessment")
        self.assertEqual(payload["operating_mode"], "ai_decision_maker")
        self.assertEqual(
            payload["decision_brief"]["baseline_reason_codes"],
            ["alpha_oi_long_confirming"],
        )
        self.assertIn("regime", payload["response_contract"])


if __name__ == "__main__":
    unittest.main()
