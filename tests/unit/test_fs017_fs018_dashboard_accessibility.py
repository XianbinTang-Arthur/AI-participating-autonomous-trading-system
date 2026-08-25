"""FS-017/FS-018 Dashboard accessibility regression contracts.

These checks intentionally validate source-level browser contracts without
claiming target-browser, keyboard, screen-reader, or axe verification.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"


def _css_block(source: str, marker: str) -> str:
    """Return a balanced CSS block starting at ``marker``."""
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for index in range(brace_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated CSS block: {marker}")


class TestFs017DetailDrawerModalContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_DIR / "dashboard-shell.html").read_text(encoding="utf-8")
        cls.app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        cls.execution_js = (
            STATIC_DIR / "modules" / "actions" / "execution-actions.js"
        ).read_text(encoding="utf-8")
        cls.risk_js = (
            STATIC_DIR / "modules" / "actions" / "risk-actions.js"
        ).read_text(encoding="utf-8")
        cls.css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    def test_shell_uses_a_named_native_modal_dialog(self) -> None:
        match = re.search(
            r"<dialog\s+id=\"detailDrawer\"(?P<attributes>[^>]*)>",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "详情抽屉必须使用原生 <dialog>")
        attributes = match.group("attributes")
        self.assertIn('role="dialog"', attributes)
        self.assertIn('aria-modal="true"', attributes)
        self.assertIn('aria-labelledby="drawerTitle"', attributes)
        self.assertIn('aria-describedby="drawerSummary"', attributes)
        self.assertNotIn("aria-hidden", attributes)
        self.assertNotIn('id="drawerBackdrop"', self.html)

    def test_close_button_is_explicit_and_accessibly_named(self) -> None:
        match = re.search(
            r"<button\s+id=\"closeDrawerButton\"(?P<attributes>[^>]*)>",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        attributes = match.group("attributes")
        self.assertIn('type="button"', attributes)
        self.assertIn('aria-label="关闭明细面板"', attributes)

    def test_open_close_lifecycle_uses_native_modal_and_focus_contract(self) -> None:
        self.assertNotIn("drawerBackdrop", self.app_js)
        self.assertIn('drawer.showModal()', self.app_js)
        self.assertIn('addEventListener("cancel"', self.app_js)
        self.assertIn("event.preventDefault();", self.app_js)
        self.assertIn("handleDrawerBackdropClick", self.app_js)
        self.assertIn("drawer.getBoundingClientRect()", self.app_js)
        self.assertIn("nodes.closeDrawerButton?.focus({ preventScroll: true })", self.app_js)
        self.assertIn("drawerReturnFocusElement", self.app_js)
        self.assertIn("returnTarget.isConnected", self.app_js)
        self.assertIn("returnTarget.focus({ preventScroll: true })", self.app_js)
        self.assertNotIn('setAttribute("aria-hidden"', self.app_js)

    def test_every_detail_action_forwards_the_trigger_element(self) -> None:
        for contract in (
            '"inspect-decision": (value, target) => inspectDecision(value, target)',
            '"inspect-decision-history": (_value, target) => inspectDecisionHistory(target)',
            '"inspect-strategy-attribution": (_value, target) => inspectStrategyAttribution(target)',
            '"inspect-trial-review-details": (_value, target) => inspectTrialReviewDetails(target)',
        ):
            self.assertIn(contract, self.app_js)
        for contract in (
            '"inspect-order": (value, target) => inspectOrder(value, target)',
            '"inspect-fill": (value, target) => inspectFill(value, target)',
            '"inspect-lifecycle-attribution": (value, target) => inspectLifecycleAttribution(value, target)',
        ):
            self.assertIn(contract, self.execution_js)
        for contract in (
            '"inspect-reconciliation": (value, target) => inspectReconciliation(value, target)',
            '"inspect-shadow": (_value, target) => inspectPhase1Shadow(target)',
        ):
            self.assertIn(contract, self.risk_js)
        self.assertGreaterEqual(
            self.app_js.count(", triggerElement);"),
            4,
            "app.js 的四类异步详情必须把原始按钮传给 openDrawer",
        )
        self.assertGreaterEqual(self.execution_js.count(", triggerElement);"), 3)
        self.assertGreaterEqual(self.risk_js.count("triggerElement,"), 2)

    def test_css_uses_dialog_backdrop_without_overriding_closed_display(self) -> None:
        self.assertIn(".detail-drawer::backdrop", self.css)
        self.assertIn(".detail-drawer[open].is-open", self.css)
        self.assertNotIn(".drawer-backdrop", self.css)
        drawer_block = _css_block(self.css, ".detail-drawer {")
        self.assertNotRegex(drawer_block, r"\bdisplay\s*:")


class TestFs018ReducedMotionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        cls.app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def test_reduced_motion_disables_animation_transition_and_smooth_scroll(self) -> None:
        block = _css_block(self.css, "@media (prefers-reduced-motion: reduce)")
        self.assertIn("animation: none !important", block)
        self.assertIn("transition: none !important", block)
        self.assertIn("scroll-behavior: auto !important", block)
        self.assertIn(".detail-drawer", self.css)
        self.assertIn("transform: none", block)

    def test_all_known_infinite_loading_animations_remain_covered(self) -> None:
        for animation in (
            "refresh-pulse 1s ease-in-out infinite",
            "button-spin 0.8s linear infinite",
            "section-refresh-shimmer 1.9s ease-in-out infinite",
            "skeleton-shimmer 1.45s ease-in-out infinite",
        ):
            self.assertIn(animation, self.css)
        block = _css_block(self.css, "@media (prefers-reduced-motion: reduce)")
        self.assertRegex(block, r"\*::before,\s*\n\s*\*::after")

    def test_javascript_smooth_scroll_respects_the_same_preference(self) -> None:
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)")', self.app_js)
        self.assertIn('return reduceMotion ? "auto" : "smooth"', self.app_js)
        self.assertEqual(
            self.app_js.count("behavior: preferredScrollBehavior()"),
            3,
            "所有显式 smooth scroll 调用都必须读取 reduced-motion 偏好",
        )
        self.assertNotIn('behavior: "smooth"', self.app_js)


if __name__ == "__main__":
    unittest.main()
