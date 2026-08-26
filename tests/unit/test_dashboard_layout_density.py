from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"
VIEWS_DIR = STATIC_DIR / "modules" / "views"


def _css_block(css: str, selector: str) -> str:
    start = css.index(selector)
    body_start = css.index("{", start)
    depth = 0
    for index in range(body_start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start : index + 1]
    raise AssertionError(f"CSS block was not closed: {selector}")


class TestDashboardLayoutDensityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        cls.strategy = (VIEWS_DIR / "strategy-view.js").read_text(encoding="utf-8")
        cls.risk = (VIEWS_DIR / "risk-view.js").read_text(encoding="utf-8")
        cls.ai_analysis = (VIEWS_DIR / "ai-analysis-view.js").read_text(encoding="utf-8")
        cls.rdp = (VIEWS_DIR / "rdp-control-panel.js").read_text(encoding="utf-8")

    def test_shared_density_tokens_drive_shell_cards_grids_and_tables(self) -> None:
        for token in (
            "--space-page-inline",
            "--space-page-top",
            "--space-page-bottom",
            "--space-section",
            "--space-grid",
            "--space-card",
            "--space-card-gap",
            "--space-item",
        ):
            self.assertIn(token, self.css)

        self.assertIn("padding: var(--space-page-top) var(--space-page-inline) var(--space-page-bottom)", _css_block(self.css, ".console-shell {"))
        self.assertIn("gap: var(--space-grid)", _css_block(self.css, ".panel-grid {"))
        self.assertIn("gap: var(--space-card-gap)", _css_block(self.css, ".surface-card {"))
        self.assertIn("padding: 10px 12px", _css_block(self.css, ".data-table th,"))

    def test_independent_column_flow_has_explicit_desktop_and_compact_contracts(self) -> None:
        self.assertIn("grid-template-columns: minmax(0, 7fr) minmax(0, 5fr)", _css_block(self.css, ".layout-flow--7-5 {"))
        self.assertIn("grid-template-columns: minmax(0, 5fr) minmax(0, 7fr)", _css_block(self.css, ".layout-flow--5-7 {"))
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", _css_block(self.css, ".layout-flow--three {"))

        compact = _css_block(self.css, "@media (max-width: 1100px)")
        self.assertRegex(
            compact,
            re.compile(r"\.layout-flow--7-5,.*?grid-template-columns:\s*minmax\(0, 1fr\)", re.DOTALL),
        )
        self.assertIn(".strategy-overview-flow > .layout-flow__column", compact)
        self.assertIn(".risk-recovery-flow > .layout-flow__column", compact)
        self.assertIn(".ai-analysis-flow > .layout-flow__column", compact)
        self.assertIn("display: contents", compact)

    def test_strategy_summary_stacks_history_below_the_shorter_summary_column(self) -> None:
        self.assertIn('class="layout-flow layout-flow--7-5 strategy-overview-flow"', self.strategy)
        hero = self.strategy.index("${sections.strategyHero}")
        history = self.strategy.index("${sections.strategyHistory}", hero)
        workbench = self.strategy.index("${sections.strategyDecisionWorkbench}", history)
        self.assertLess(hero, history)
        self.assertLess(history, workbench)
        self.assertIn('class="strategy-overview-flow__history"', self.strategy)
        self.assertIn('class="strategy-overview-flow__workbench"', self.strategy)

    def test_risk_recovery_and_normal_review_use_independent_columns(self) -> None:
        self.assertIn('class="layout-flow layout-flow--three risk-recovery-flow"', self.risk)
        for class_name in (
            "risk-recovery-flow__recovery",
            "risk-recovery-flow__reconciliation",
            "risk-recovery-flow__margin",
            "risk-recovery-flow__account",
            "risk-recovery-flow__exposure",
            "risk-recovery-flow__position-mode",
            "risk-review-flow__blockers",
            "risk-review-flow__metrics",
            "risk-review-flow__bills",
        ):
            self.assertIn(f'class="{class_name}"', self.risk)

    def test_ai_analysis_pairs_related_cards_without_equal_height_rows(self) -> None:
        self.assertIn('class="layout-flow layout-flow--5-7 ai-analysis-flow"', self.ai_analysis)
        self.assertIn('class="ai-analysis-flow__hero"', self.ai_analysis)
        self.assertIn('class="ai-analysis-flow__profile"', self.ai_analysis)
        self.assertIn('class="ai-analysis-flow__latest"', self.ai_analysis)
        self.assertIn('class="ai-analysis-flow__performance"', self.ai_analysis)
        self.assertNotIn('<div class="span-4">${workspace.aiHero}</div>', self.ai_analysis)

    def test_rdp_v3_uses_one_compact_workspace_with_independent_primary_columns(self) -> None:
        self.assertIn("export function renderRdpControlPanelV3", self.rdp)
        self.assertIn("rdp-v3-primary-grid", self.rdp)
        self.assertIn("renderRuns(workspace.execution", self.rdp)
        self.assertIn("renderResearch(workspace.research", self.rdp)
        self.assertIn("renderRelease(workspace.release", self.rdp)
        self.assertIn("renderLifecycle(workspace.lifecycle", self.rdp)
        self.assertIn(
            "grid-template-columns: minmax(330px, 5fr) minmax(420px, 7fr)",
            _css_block(self.css, ".rdp-v3-primary-grid {"),
        )


if __name__ == "__main__":
    unittest.main()
