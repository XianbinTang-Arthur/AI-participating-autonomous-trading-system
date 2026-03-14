from __future__ import annotations

from aats.schemas.decision import BaselineAssessment, DecisionContext


class PromptBuilder:
    def build(self, context: DecisionContext, baseline: BaselineAssessment) -> str:
        return (
            f"decision_id={context.decision_id} "
            f"symbol={context.symbol} "
            f"timeframe={context.timeframe} "
            f"baseline_regime={baseline.regime} "
            f"baseline_bias={baseline.direction_bias}"
        )

