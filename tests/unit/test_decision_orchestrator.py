from __future__ import annotations

import logging
import unittest
from decimal import Decimal

from aats.schemas.decision import PositionSizingBreakdown
from aats.services.decision_engine.target_position import log_position_sizing_breakdown


class TestPositionSizingLogging(unittest.TestCase):
    def test_log_position_sizing_breakdown_emits_finalized_fields(self) -> None:
        sizing_breakdown = PositionSizingBreakdown(
            sizing_mode="balance_aware",
            available_equity=Decimal("390"),
            margin_usage_fraction=Decimal("0.75"),
            target_leverage=5.0,
            leverage_bias=1.0,
            last_price=Decimal("100000"),
            default_order_qty=Decimal("0.004"),
            position_scale=Decimal("1"),
            legacy_reference_qty=Decimal("0.004"),
            balance_reference_qty=Decimal("0.004"),
            resolved_reference_qty=Decimal("0.004"),
            resolved_target_qty=Decimal("0.004"),
            budgeted_notional=Decimal("400"),
        )

        with self.assertLogs("aats.decision_engine", level="INFO") as captured:
            log_position_sizing_breakdown(
                logger=logging.getLogger("aats.decision_engine"),
                decision_id="decision_sizing_log",
                symbol="BTC-USDT-SWAP",
                sizing_breakdown=sizing_breakdown,
                final_action="enter",
                final_direction="long",
                final_target_qty=Decimal("0.004"),
                policy_blocked=False,
                risk_capped=True,
            )

        rendered = "\n".join(captured.output)
        self.assertIn("decision_target_sizing_resolved", rendered)
        self.assertIn('available_equity="390"', rendered)
        self.assertIn('margin_usage_fraction="0.75"', rendered)
        self.assertIn("target_leverage=5.0", rendered)
        self.assertIn('last_price="100000"', rendered)
        self.assertIn('resolved_target_qty="0.004"', rendered)
        self.assertIn('final_target_qty="0.004"', rendered)
