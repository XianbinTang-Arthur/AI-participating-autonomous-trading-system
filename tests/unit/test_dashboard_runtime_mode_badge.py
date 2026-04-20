"""P0-b Task 2.1 regression guard: 顶栏 runtime mode badge 的 HTML/JS/CSS 接线.

对应 spec: docs/governance/p0b_observability_implementation_spec_2026_04_20.md §2.1
对应 governance: docs/governance/runtime_trading_mode_semantics.md

确保:
1. dashboard-shell.html 有 ``runtimeModeBadge`` 节点和语义 modal
2. shell-renderer.js 实现 ``renderRuntimeModeBadge`` 并在每次 renderShell 调用
3. app.css 三种 tone class 都存在
4. CORE_SPECS 包含 ``aiRuntime``/``/ai/runtime`` 数据源 (没这个 badge 就空转)
5. app.js 注册了 show/close modal 的 dispatchAction handler
"""
from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"


class TestRuntimeModeBadgeWiring(unittest.TestCase):
    def test_dashboard_shell_declares_badge_and_dialog(self) -> None:
        html = (STATIC_DIR / "dashboard-shell.html").read_text(encoding="utf-8")
        self.assertIn('id="runtimeModeBadge"', html, "顶栏缺 badge 节点")
        self.assertIn('id="runtimeModeBadgeBody"', html, "badge 缺 body span")
        self.assertIn('id="runtimeModeInfoDialog"', html, "缺语义说明 modal")
        # data-action 必须和 LOCAL_DISPATCH_ACTIONS 里注册的名一致
        self.assertIn('data-action="show-runtime-mode-info"', html)
        self.assertIn('data-action="close-runtime-mode-info"', html)
        # 关键内容检查:spec 要求 badge 解释 baseline_only 是按设计
        self.assertIn("baseline_only", html)
        self.assertIn("reference_only", html)
        self.assertIn("ai_decision_maker", html)

    def test_shell_renderer_exposes_runtime_mode_badge_render(self) -> None:
        source = (STATIC_DIR / "modules" / "shell-renderer.js").read_text(encoding="utf-8")
        self.assertIn("renderRuntimeModeBadge", source, "shell-renderer 缺 renderRuntimeModeBadge 函数")
        self.assertIn(
            "effective_operating_mode",
            source,
            "renderRuntimeModeBadge 必须读 ai_runtime.effective_operating_mode",
        )
        # 验证三种 tone class 都有对应分支
        self.assertIn("runtime-mode-badge--baseline-only", source)
        self.assertIn("runtime-mode-badge--ai-assisted", source)
        self.assertIn("runtime-mode-badge--ai-decision-maker", source)
        # 必须在 renderShell 内被调用
        render_shell_block = source.split("function renderShell()", 1)[1].split("}", 1)[0]
        self.assertIn("renderRuntimeModeBadge", render_shell_block)

    def test_app_css_defines_badge_tone_classes(self) -> None:
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        self.assertIn(".runtime-mode-badge--baseline-only", css)
        self.assertIn(".runtime-mode-badge--ai-assisted", css)
        self.assertIn(".runtime-mode-badge--ai-decision-maker", css)
        self.assertIn(".runtime-mode-dialog", css)

    def test_core_specs_pulls_ai_runtime(self) -> None:
        store = (STATIC_DIR / "modules" / "store.js").read_text(encoding="utf-8")
        # aiRuntime 必须在 CORE_SPECS 里,否则 badge 在非 AI view 下为空
        core_block_start = store.index("export const CORE_SPECS = [")
        core_block_end = store.index("];", core_block_start)
        core_block = store[core_block_start:core_block_end]
        self.assertIn('"aiRuntime"', core_block, "CORE_SPECS 缺 aiRuntime")
        self.assertIn('/ai/runtime', core_block)

    def test_app_js_registers_runtime_mode_dispatch_actions(self) -> None:
        app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('"show-runtime-mode-info"', app_js, "app.js 缺 show-runtime-mode-info handler")
        self.assertIn('"close-runtime-mode-info"', app_js, "app.js 缺 close-runtime-mode-info handler")
        self.assertIn("showRuntimeModeInfoDialog", app_js)
        self.assertIn("closeRuntimeModeInfoDialog", app_js)


if __name__ == "__main__":
    unittest.main()
