from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"


class TestOperatorTruthCockpitWiring(unittest.TestCase):
    def test_overview_fetches_strategy_runtime_for_cockpit(self) -> None:
        store = (STATIC_DIR / "modules" / "store.js").read_text(encoding="utf-8")
        overview_start = store.index("overview: [")
        strategy_start = store.index("strategy:", overview_start)
        overview_block = store[overview_start:strategy_start]

        self.assertIn('"strategyRuntime"', overview_block)
        self.assertIn('"/strategy/runtime"', overview_block)

    def test_overview_declares_operator_truth_cockpit(self) -> None:
        source = (STATIC_DIR / "modules" / "views" / "overview-view.js").read_text(encoding="utf-8")

        self.assertIn("运行真相驾驶舱", source)
        self.assertIn("交易显微镜", source)
        self.assertIn("影子基准：未验证", source)
        self.assertIn("buildOperatorTruthCockpit", source)
        self.assertIn("terminal_no_fill_explanation", source)
        self.assertIn("无成交终局", source)
        self.assertIn("终端无成交解释", source)
        self.assertIn("claimedSubmitGate", source)
        self.assertIn("恢复仍被 CLAIMED 提交阻断", source)
        self.assertIn("已接受新基线不等于清除 CLAIMED 提交", source)
        self.assertIn("data.strategyRuntime", source)
        self.assertIn("data.aiRuntime", source)

    def test_cockpit_aggregates_required_truth_sources(self) -> None:
        source = (STATIC_DIR / "modules" / "views" / "overview-view.js").read_text(encoding="utf-8")

        for key in [
            "aiRuntime",
            "strategyRuntime",
            "latestDecision",
            "executionLatest",
            "blockers",
            "metrics",
        ]:
            self.assertIn(f'"{key}"', source)

        for field in [
            "configured_operating_mode",
            "effective_operating_mode",
            "manual_override_active",
            "provider_ready",
            "shadow_mode_enabled",
            "strategy_profile_auto_control_effective",
            "entry_execution_guard",
            "latest_bundle_status",
            "terminalNoFill",
        ]:
            self.assertIn(field, source)

    def test_cockpit_has_source_specific_drilldowns(self) -> None:
        source = (STATIC_DIR / "modules" / "views" / "overview-view.js").read_text(encoding="utf-8")

        for target_view in ["strategy", "execution", "risk", "aiAnalysis"]:
            self.assertIn(f'"navigate-view", "{target_view}"', source)

    def test_runtime_source_terms_are_localized(self) -> None:
        terms = (STATIC_DIR / "modules" / "terms.js").read_text(encoding="utf-8")

        self.assertIn('not_loaded: "未装载"', terms)
        self.assertIn('local_stub: "本地占位状态"', terms)
        self.assertIn('remote_decision: "远端决策进程"', terms)
        self.assertIn('claimed_submit_commands_require_exchange_reconciliation', terms)
        self.assertIn('external_operator_confirmation_required_before_resolve_stuck_submission', terms)


if __name__ == "__main__":
    unittest.main()
