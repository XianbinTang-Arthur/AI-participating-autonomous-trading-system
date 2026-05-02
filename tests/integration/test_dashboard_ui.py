from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import _protected_dashboard_panel_payload, _strategy_view_strategy_runtime_payload, auth_router
from aats.api.ui import ui_router
from aats.bootstrap.settings import AATSSettings


def _assert_imports_helper(testcase: unittest.TestCase, text: str, helpers: list[str], src_suffix: str) -> None:
    """Assert that ``text`` contains an ``import { ... } from "<src>"`` statement
    that pulls in all of ``helpers`` (in any order, possibly with extra names,
    possibly with ``as`` aliasing).

    This is intentionally looser than ``assertIn("import { foo, bar } from ...")``
    so that re-ordering, adding new helpers, or aliasing one of them does not
    break the test. The real intent is just "this consumer imports these
    symbols from this module", not "the import statement is character-perfect".
    """
    pattern = re.compile(
        r'import\s*\{([^}]*)\}\s*from\s*"([^"]*)"',
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        body, src = match.group(1), match.group(2)
        if not src.endswith(src_suffix):
            continue
        imported = {part.split(" as ")[0].strip() for part in body.split(",") if part.strip()}
        if all(helper in imported for helper in helpers):
            return
    testcase.fail(
        f"expected an import of {helpers!r} from a module ending in {src_suffix!r}, "
        f"but found none. text head:\n{text[:200]}"
    )


def _run_node_module(script: str, *, encoding: str | None = None) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".mjs",
        dir=repo_root,
        delete=False,
    ) as handle:
        handle.write(script)
        temp_script = Path(handle.name)
    try:
        return subprocess.run(
            ["node", str(temp_script)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding=encoding,
            check=False,
        )
    finally:
        temp_script.unlink(missing_ok=True)


def _render_strategy_view_with_hidden_strings(strings: list[str]) -> subprocess.CompletedProcess[str]:
    hidden_strings = json.dumps(strings, ensure_ascii=False)
    script = f"""
import {{ renderStrategyView }} from './aats/api/static/modules/views/strategy-view.js';

const hiddenStrings = {hidden_strings};
const html = renderStrategyView({{
  strategyRuntime: {{
    summary: {{ operator_summary: hiddenStrings.join(' | ') }},
    latest_snapshot: {{
      candidates: hiddenStrings.map((text, index) => ({{
        family: 'smart_arbitrage',
        state: 'blocked',
        route_action: 'advisory_only',
        pair_id: `pair_${{index}}`,
        headline: text,
        reason_codes: [text],
      }})),
      automation_decisions: [],
    }},
    latest_allocation_decision: {{ operator_summary: hiddenStrings.join(' | '), reason_codes: hiddenStrings }},
    recent_sleeve_intents: hiddenStrings.map((text, index) => ({{
      strategy_sleeve_id: `intent_${{index}}`,
      family: 'smart_arbitrage',
      state: 'blocked',
      route_action: 'advisory_only',
      pair_id: `pair_${{index}}`,
      headline: text,
      reason_codes: [text],
      control_reason_codes: [text],
    }})),
    configured_parameters: {{
      directional: {{ product_type: 'derivatives', shorting_runtime_supported: true, short_bias_enabled: true }},
      smart_arbitrage: {{
        enabled: true,
        pair_registry_error_codes: ['smart_arbitrage_pair_execution_modes_invalid'],
        pair_definitions: [{{ pair_id: 'btc_usdt_swap', spot_symbol: 'BTC-USDT', hedge_symbol: 'BTC-USDT-SWAP' }}],
      }},
    }},
    latest_bundle: {{}},
    latest_applied_target: {{}},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {{ smart_arbitrage: {{ enabled: true, runtime_supported: true, execution_compatible: true }} }},
  }},
  latestDecision: {{
    baseline_assessment: {{ direction_bias: 'short', confidence: 0.64 }},
    ai_assessment: {{ directional_edge: -0.12 }},
    position_target: {{ position_intent: 'hold', target_position_qty: 0, current_position_qty: 0, delta_position_qty: 0, guardrail_flags: [] }},
    policy_decision: {{ execution_allowed: true, rejection_reasons: [] }},
    risk_decision: {{ approved: true, rejection_reasons: [], constraints_applied: [] }},
  }},
  strategyAttribution: {{ summary: {{}}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] }},
  trialReviewSummary: {{ summary: {{}}, sections: {{}} }},
}});

console.log(JSON.stringify({{
  hidesAll: hiddenStrings.every((text) => !html.includes(text)),
}}));
"""
    return _run_node_module(script, encoding="utf-8")


class TestDashboardUI(unittest.TestCase):
    def test_dashboard_blockers_panel_reuses_blocker_control_payload(self) -> None:
        class Query:
            def __init__(self) -> None:
                self.blocker_control_calls = 0
                self.blocker_history_calls = 0

            def blocker_control(self) -> dict[str, object]:
                self.blocker_control_calls += 1
                return {
                    "blockers": [
                        {
                            "blocker": "phase1_shadow_recovery_required",
                            "subsystem": "account_state",
                            "affects_execution": True,
                            "submit_only": False,
                            "recommended_next_step": "先完成 Phase1 shadow 恢复。",
                            "title": "Phase1 shadow 需要恢复",
                            "description": "shadow 状态未收敛。",
                            "impact": "自动执行保持阻断。",
                            "priority": 10,
                            "root_cause": True,
                            "derived_from": ["phase1_shadow"],
                            "resolution_mode": "manual_only",
                            "actions": [{"action_id": "inspect", "label": "检查"}],
                        }
                    ]
                }

            def blocker_history(self, *, limit: int, offset: int) -> dict[str, object]:
                self.blocker_history_calls += 1
                self.assert_limit = limit
                self.assert_offset = offset
                return {"history": [{"blocker": "phase1_shadow_recovery_required"}]}

            def blockers(self) -> list[dict[str, object]]:
                raise AssertionError("blockers panel should derive from blocker_control")

        query = Query()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    runtime=SimpleNamespace(kill_switch=SimpleNamespace(halted=True))
                )
            )
        )

        payload = _protected_dashboard_panel_payload(
            request=request,
            query=query,
            panel_key="blockers",
            recent_decisions_limit=8,
            recent_orders_limit=8,
            recent_fills_limit=8,
            recent_replay_validations_limit=8,
            recent_ai_assessments_limit=8,
            recent_ai_shadow_decisions_limit=8,
            recent_ai_shadow_evaluations_limit=8,
        )

        self.assertTrue(payload["blocked"])
        self.assertTrue(payload["halted"])
        blocker = payload["blockers"][0]
        self.assertEqual(blocker["blocker"], "phase1_shadow_recovery_required")
        self.assertEqual(blocker["recommended_action"], "先完成 Phase1 shadow 恢复。")
        self.assertEqual(blocker["recommended_next_step"], "先完成 Phase1 shadow 恢复。")
        self.assertTrue(blocker["affects_account_synchronization"])
        self.assertEqual(blocker["actions"], [{"action_id": "inspect", "label": "检查"}])
        self.assertEqual(payload["recent_history"], [{"blocker": "phase1_shadow_recovery_required"}])
        self.assertEqual(query.blocker_control_calls, 1)
        self.assertEqual(query.blocker_history_calls, 1)

    def test_dashboard_bundle_dispatches_position_lifecycle_attribution_panel(self) -> None:
        calls: list[int] = []

        class Query:
            def position_lifecycle_attribution(self, *, limit: int) -> dict[str, object]:
                calls.append(limit)
                return {"lifecycles": [], "summary": {"limit": limit}}

        common = {
            "request": SimpleNamespace(),
            "query": Query(),
            "panel_key": "positionLifecycleAttribution",
            "recent_decisions_limit": 8,
            "recent_orders_limit": 8,
            "recent_fills_limit": 8,
            "recent_replay_validations_limit": 8,
            "recent_ai_assessments_limit": 8,
            "recent_ai_shadow_decisions_limit": 8,
            "recent_ai_shadow_evaluations_limit": 8,
        }

        strategy_payload = _protected_dashboard_panel_payload(view="strategy", **common)
        execution_payload = _protected_dashboard_panel_payload(view="execution", **common)

        self.assertEqual(strategy_payload["summary"]["limit"], 6)
        self.assertEqual(execution_payload["summary"]["limit"], 8)
        self.assertEqual(calls, [6, 8])

    def test_dashboard_routes_serve_html_and_assets_when_auth_is_disabled(self) -> None:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(ui_router)
        app.state.runtime = SimpleNamespace(settings=AATSSettings.model_validate({}))

        with TestClient(app) as client:
            responses = {
                "root": client.get("/"),
                "overview": client.get("/ui"),
                "strategy": client.get("/ui/strategy"),
                "exit_execution": client.get("/ui/exit-execution"),
                "ai_redirect": client.get("/ui/ai", follow_redirects=False),
                "ai_analysis": client.get("/ui/ai-analysis"),
                "ai_config": client.get("/ui/ai-config"),
                "css": client.get("/ui/app.css"),
                "js": client.get("/ui/app.js"),
                "dashboard_refresh_js": client.get("/ui/modules/dashboard-refresh.js"),
                "flash_js": client.get("/ui/modules/flash.js"),
                "navigation_state_js": client.get("/ui/modules/navigation-state.js"),
                "shell_renderer_js": client.get("/ui/modules/shell-renderer.js"),
                "store_js": client.get("/ui/modules/store.js"),
                "refresh_interactivity_js": client.get("/ui/modules/refresh-interactivity.js"),
                "view_router_js": client.get("/ui/modules/view-router.js"),
                "risk_actions_js": client.get("/ui/modules/actions/risk-actions.js"),
                "execution_actions_js": client.get("/ui/modules/actions/execution-actions.js"),
                "admin_actions_js": client.get("/ui/modules/actions/admin-actions.js"),
                "ai_view_js": client.get("/ui/modules/views/ai-view.js"),
                "ai_analysis_js": client.get("/ui/modules/views/ai-analysis-view.js"),
                "ai_config_js": client.get("/ui/modules/views/ai-config-view.js"),
                "exit_execution_js": client.get("/ui/modules/views/exit-execution-view.js"),
                "strategy_js": client.get("/ui/modules/views/strategy-view.js"),
                "risk_js": client.get("/ui/modules/views/risk-view.js"),
                # Slice #5 refactor：reconciliationActionCopy / renderReconciliationControls
                # 已从 risk-view.js 搬到共享模块 reconciliation-controls.js
                "reconciliation_controls_js": client.get(
                    "/ui/modules/reconciliation-controls.js"
                ),
            }
            login = client.get("/login", follow_redirects=False)

        for key, response in responses.items():
            if key == "ai_redirect":
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/ui/ai-analysis")
                continue
            self.assertEqual(response.status_code, 200)

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/ui")

        root_text = responses["root"].text
        self.assertNotIn("AI 工作台", root_text)
        self.assertIn("/ui/exit-execution", root_text)
        self.assertIn("/ui/ai-analysis", root_text)
        self.assertIn("/ui/ai-config", root_text)
        self.assertIn('data-view="aiAnalysis"', root_text)
        self.assertIn('data-view="exitExecution"', root_text)

        app_js_text = responses["js"].text
        view_router_text = responses["view_router_js"].text
        navigation_state_text = responses["navigation_state_js"].text
        dashboard_refresh_text = responses["dashboard_refresh_js"].text
        flash_text = responses["flash_js"].text
        shell_renderer_text = responses["shell_renderer_js"].text
        risk_actions_text = responses["risk_actions_js"].text
        execution_actions_text = responses["execution_actions_js"].text
        admin_actions_text = responses["admin_actions_js"].text
        js_text = "\n".join(
            [
                app_js_text,
                view_router_text,
                navigation_state_text,
                dashboard_refresh_text,
                flash_text,
                shell_renderer_text,
                risk_actions_text,
                execution_actions_text,
                admin_actions_text,
            ]
        )
        self.assertIn("renderAIAnalysisView", js_text)
        self.assertIn("renderExitExecutionView", js_text)
        self.assertIn("fetchDashboardBundle", js_text)
        self.assertIn("readableFamilyExecutionSummary", js_text)
        self.assertIn("createDashboardRefreshController", js_text)
        self.assertIn("createDashboardShellRenderer", js_text)
        self.assertIn("createNavigationStateController", js_text)
        self.assertIn("createRiskActionHandlers", js_text)
        self.assertIn("createExecutionActionHandlers", js_text)
        self.assertIn("createAdminActions", js_text)
        self.assertIn("hydrateViewStateFromLocation", js_text)
        self.assertIn("syncActiveViewLocationState", js_text)
        self.assertIn("syncRefreshDisabledButtons", js_text)
        self.assertIn("currentRefreshInteractivityRoots", js_text)
        self.assertIn("const navigationState = createNavigationStateController({ state, viewLinks });", js_text)
        self.assertIn("refreshController = createDashboardRefreshController({", js_text)
        # The "已排队一次新的刷新请求" notice was removed in C6 of the round-4
        # dashboard UI review: with the sticky-flash design (8s lazy TTL), the
        # notice would clobber an action's outcome flash for the entire window
        # the user is staring at the just-finished refresh, even though the
        # drain happens within the same finally{} block in <1s. The shimmer
        # already communicates "refresh in progress" and the refresh button
        # is intentionally NOT locked during a normal background refresh, so
        # the queued-drain is fully transparent to the user. See the comment
        # in dashboard-refresh.js's isPrimaryInFlight branch.
        self.assertNotIn("已排队一次新的刷新请求", js_text)
        self.assertIn("当前已在${VIEW_LABELS[nextView] || \"当前页面\"}，已为你重新拉取最新数据。", js_text)
        # flash.js helper module: producers go through setFlash / ensureNotBusy
        # so the sticky-flash _expiresAt mutation never accidentally carries
        # across producer calls (see C6 of round-4 dashboard UI review and the
        # docstring of modules/flash.js).
        #
        # The export-signature assertions are deliberately string-matched: we
        # _do_ want a regression to fire if someone changes setFlash's
        # parameter shape, since every consumer in the codebase relies on it.
        self.assertIn("export function setFlash(state, tone, message)", flash_text)
        self.assertIn("export function clearFlash(state)", flash_text)
        self.assertIn("export function isFlashLive(state)", flash_text)
        self.assertIn("export function ensureNotBusy(state, renderBanners)", flash_text)
        # The consumer-side import assertions, on the other hand, are kept
        # loose via _assert_imports_helper: we only care that the helpers are
        # imported from flash.js by name, NOT that the import statement is
        # character-perfect. Re-ordering, adding more helpers, or aliasing
        # should not break the test — see the helper docstring above.
        _assert_imports_helper(self, dashboard_refresh_text, ["setFlash", "isFlashLive"], "flash.js")
        _assert_imports_helper(self, shell_renderer_text, ["clearFlash", "getFlashTtl"], "flash.js")
        _assert_imports_helper(self, app_js_text, ["ensureNotBusy", "setFlash"], "flash.js")
        _assert_imports_helper(self, risk_actions_text, ["ensureNotBusy", "setFlash"], "flash.js")
        _assert_imports_helper(self, execution_actions_text, ["setFlash"], "flash.js")
        _assert_imports_helper(self, admin_actions_text, ["ensureNotBusy", "setFlash"], "flash.js")
        # Round 5 cleanup: app.js used to carry duplicate copies of 7 helper
        # functions that already lived (and were actually used) inside
        # risk-actions.js. Guard against the same dead-code drift in the
        # future. See round-5 review C1 for the original finding.
        self.assertNotIn("function defaultBlockerActionReason(actionId)", app_js_text)
        self.assertNotIn("function blockerActionPendingLabel(actionId)", app_js_text)
        self.assertNotIn("function blockerActionSuccessMessage(actionId)", app_js_text)
        self.assertNotIn("function blockerActionConfirmMessage(actionId)", app_js_text)
        self.assertNotIn("async function applyExitExecutionHistoryWorkspaceFilters", app_js_text)
        self.assertNotIn("async function resetExitExecutionHistoryWorkspaceFilters", app_js_text)
        self.assertNotIn("async function paginateExitExecutionHistory", app_js_text)
        self.assertNotIn("refreshBackgroundPanels", js_text)
        self.assertNotIn("backgroundGenerations", js_text)
        self.assertNotIn("backgroundControllers", js_text)
        self.assertNotIn("cancelBackgroundRefresh", js_text)
        self.assertIn('document.addEventListener("visibilitychange", handleVisibilityChange);', js_text)
        self.assertIn('if (document.visibilityState !== "visible") return;', js_text)
        self.assertNotIn('ai: "/ui/ai"', js_text)

        self.assertIn("createDashboardRefreshController", app_js_text)
        self.assertIn("createDashboardShellRenderer", app_js_text)
        self.assertIn("createNavigationStateController", app_js_text)
        self.assertIn("createRiskActionHandlers", app_js_text)
        self.assertIn("createExecutionActionHandlers", app_js_text)
        self.assertIn("createAdminActions", app_js_text)
        self.assertIn("const navigationState = createNavigationStateController({ state, viewLinks });", app_js_text)
        self.assertIn("const shellRenderer = createDashboardShellRenderer({", app_js_text)
        self.assertIn("refreshController = createDashboardRefreshController({", app_js_text)
        self.assertIn("const riskActionHandlers = createRiskActionHandlers({", app_js_text)
        self.assertIn("const executionActionHandlers = createExecutionActionHandlers({", app_js_text)
        self.assertIn("const adminActions = createAdminActions({", app_js_text)
        self.assertIn("const domainHandler = riskActionHandlers[action] || executionActionHandlers[action] || adminActionHandlers[action] || rdpActionHandlers[action];", app_js_text)
        self.assertNotIn("const VIEW_ROUTES = {", app_js_text)
        self.assertNotIn("const VIEW_META = {", app_js_text)
        self.assertNotIn("function scheduleRefresh() {", app_js_text)
        self.assertNotIn("function resolveViewFromLocation()", app_js_text)
        self.assertNotIn("function renderPageChrome()", app_js_text)
        self.assertNotIn("function renderStatusRibbon()", app_js_text)
        self.assertNotIn("async function triggerResume(target = null)", app_js_text)
        self.assertNotIn("async function inspectOrder(orderId)", app_js_text)
        self.assertNotIn("async function createOperatorUser()", app_js_text)

        self.assertIn('aiAnalysis: "/ui/ai-analysis"', view_router_text)
        self.assertIn('exitExecution: "/ui/exit-execution"', view_router_text)
        self.assertIn("VIEW_META", view_router_text)
        self.assertIn("VIEW_LABELS", view_router_text)
        self.assertIn("resolveKnownView", view_router_text)
        self.assertIn("resolveViewFromLocation", view_router_text)

        self.assertIn("EXIT_EXECUTION_HISTORY_ACTION_FILTERS", navigation_state_text)
        self.assertIn("EXIT_EXECUTION_HISTORY_WINDOW_FILTERS", navigation_state_text)
        self.assertIn("buildExitExecutionViewPath", navigation_state_text)
        self.assertIn("syncExitExecutionNavigationLinks", navigation_state_text)
        self.assertIn("coerceReplayParentFilter", navigation_state_text)
        self.assertIn("normalizeExitExecutionHistoryFilterValue", navigation_state_text)

        self.assertIn("buildDashboardBundleRequestPlan", dashboard_refresh_text)
        self.assertIn("const refreshPlan = buildDashboardBundleRequestPlan(refreshingView, state);", dashboard_refresh_text)
        self.assertIn("void refreshDeferredPanels({", dashboard_refresh_text)
        self.assertIn("setPendingPanels(refreshPlan.deferredPanels, refreshGeneration);", dashboard_refresh_text)

        self.assertIn("export function createDashboardShellRenderer", shell_renderer_text)
        self.assertIn("function renderShell()", shell_renderer_text)
        self.assertIn("function renderStatusRibbon()", shell_renderer_text)
        self.assertIn("function renderLoadingView()", shell_renderer_text)
        self.assertIn("function renderBanners()", shell_renderer_text)
        self.assertIn("function patchRenderedSections(", shell_renderer_text)

        self.assertIn("export function createRiskActionHandlers", risk_actions_text)
        self.assertIn('"trigger-blocker-action": (value, target) => triggerBlockerAction(value, target)', risk_actions_text)
        self.assertIn('"trigger-resume": (_value, target) => triggerResume(target)', risk_actions_text)
        self.assertIn('"apply-exit-execution-history-workspace": (_value, target) => applyExitExecutionHistoryWorkspaceFilters(target)', risk_actions_text)

        self.assertIn("export function createExecutionActionHandlers", execution_actions_text)
        self.assertIn("pageLoadStep = 12", execution_actions_text)
        self.assertIn('"inspect-lifecycle-attribution": (value) => inspectLifecycleAttribution(value)', execution_actions_text)
        self.assertIn('"load-more-orders": () => adjustPageLimit("recentOrders", pageLoadStep)', execution_actions_text)
        self.assertIn('"load-more-fills": () => adjustPageLimit("recentFills", pageLoadStep)', execution_actions_text)

        self.assertIn("export function createAdminActions", admin_actions_text)
        self.assertIn('"toggle-user": (value) => toggleOperatorUser(value)', admin_actions_text)
        self.assertIn('"delete-user": (value) => deleteOperatorUser(value)', admin_actions_text)
        self.assertIn("createOperatorUser", admin_actions_text)

        store_text = responses["store_js"].text
        self.assertIn('["profileControlSummary", "/reports/profile-control-summary"]', store_text)
        self.assertIn('["trialReviewSummary", "/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4"]', store_text)
        self.assertIn('["trialReviewHistory", "/reports/trial-review-history?limit=5&offset=0"]', store_text)
        self.assertIn("aiAnalysis", store_text)
        self.assertIn('["aiRecent", `/ai/recent?limit=${limits.recentAIAssessments}&offset=0`]', store_text)
        self.assertIn('["aiShadowRecent", `/ai/shadow/recent?limit=${limits.recentAIShadowDecisions}&offset=0`]', store_text)
        self.assertIn('["aiShadowEvaluations", `/ai/shadow/evaluations?limit=${limits.recentAIShadowEvaluations}&offset=0`]', store_text)
        self.assertNotIn("viewBackgroundSpecs", store_text)
        self.assertIn('["positions", "/positions"]', store_text)
        self.assertIn('["strategyRuntime", "/strategy/runtime"]', store_text)
        self.assertIn('["strategyAttribution", "/reports/strategy-attribution?limit=200"]', store_text)
        self.assertIn('["positionLifecycleAttribution", "/reports/position-lifecycle-attribution?limit=6"]', store_text)
        self.assertIn('["exitExecutionActionHistoryPage", riskExitExecutionHistoryPath]', store_text)
        self.assertIn('["exitExecutionActionHistoryPage", exitExecutionWorkspaceHistoryPath]', store_text)
        self.assertIn("DEFAULT_EXIT_EXECUTION_HISTORY_PAGING", store_text)
        self.assertIn("DEFERRED_VIEW_PANELS", store_text)
        self.assertIn("dashboardBundlePanelKeys", store_text)
        self.assertIn("buildDashboardBundleRequestPlan", store_text)
        self.assertIn('["positionLifecycleAttribution", "/reports/position-lifecycle-attribution?limit=8"]', store_text)
        self.assertIn('["guardedLivePreflight", "/system/guarded-live-preflight"]', store_text)
        self.assertIn('["guardedLiveRunPacket", "/reports/guarded-live-run-packet"]', store_text)
        self.assertNotIn("/system/guarded-live/preflight", store_text)
        self.assertNotIn("/system/guarded-live/run-packet", store_text)
        self.assertIn('params.append("panel", key);', store_text)
        self.assertIn('recentAIAssessments: String(limits.recentAIAssessments)', store_text)
        self.assertIn('recentAIShadowDecisions: String(limits.recentAIShadowDecisions)', store_text)
        self.assertIn('recentAIShadowEvaluations: String(limits.recentAIShadowEvaluations)', store_text)
        self.assertIn("buildDashboardBundlePath", store_text)
        self.assertIn('/dashboard/bundle?', store_text)
        self.assertNotIn('  ai: [', store_text)

        ai_analysis_text = responses["ai_analysis_js"].text
        self.assertIn("renderAISections", ai_analysis_text)
        self.assertIn("renderAIAnalysisSectionCards", ai_analysis_text)
        self.assertIn("档位控制证据", ai_analysis_text)
        self.assertIn("风险预算乘数", ai_analysis_text)
        self.assertIn("自动切档闸门", ai_analysis_text)
        self.assertNotIn("前往 AI 工作台", ai_analysis_text)
        self.assertNotIn("前往 AI 配置", ai_analysis_text)

        ai_config_text = responses["ai_config_js"].text
        self.assertIn("运行模式切换", ai_config_text)
        self.assertIn("自动换档控制", ai_config_text)
        self.assertIn("运行参数概览", ai_config_text)
        self.assertIn("紧急安全切档", ai_config_text)
        self.assertIn("持有与冷却", ai_config_text)
        self.assertIn("策略层 shadow", ai_config_text)
        self.assertIn("执行层 shadow", ai_config_text)
        self.assertIn("手动切档", ai_config_text)
        self.assertIn("自动切档", ai_config_text)
        self.assertNotIn("前往 AI 工作台", ai_config_text)
        self.assertNotIn("查看 AI 分析", ai_config_text)

        strategy_text = responses["strategy_js"].text
        self.assertIn("查看风险与恢复", strategy_text)
        self.assertIn("记录本次复盘", strategy_text)
        self.assertIn("记为继续小资金试盘", strategy_text)
        self.assertIn("记为缩小试盘规模", strategy_text)
        self.assertIn("记为暂停试盘并复盘", strategy_text)
        self.assertIn("提交放量评审", strategy_text)
        self.assertIn("当前不在试盘观察流程", strategy_text)
        self.assertIn("最近处理记录", strategy_text)
        self.assertIn("试盘守护硬停机", strategy_text)
        self.assertNotIn("自动跳档状态", strategy_text)
        self.assertIn("系统自动试盘结论", strategy_text)
        self.assertIn("样本仍少，先继续观察", strategy_text)
        self.assertIn("strategyRuntimeSummary", strategy_text)
        self.assertNotIn("renderStrategyCandidateTable", strategy_text)
        self.assertNotIn("renderAllocatorBudgetSnapshotTable", strategy_text)
        self.assertNotIn("renderAllocatorConflictResolutionTable", strategy_text)
        self.assertNotIn("renderAllocatorNettingDecisionTable", strategy_text)
        self.assertIn("strategyFamilyEnablement", strategy_text)
        self.assertIn("策略归因", strategy_text)
        self.assertIn("整笔仓位复盘", strategy_text)
        self.assertIn("自动预算与启停", strategy_text)
        self.assertIn("strategyAttribution", strategy_text)
        self.assertNotIn('href="#strategy-reference"', strategy_text)
        self.assertNotIn("展开配置与成本参考", strategy_text)
        self.assertNotIn("预算快照", strategy_text)
        self.assertNotIn("冲突解算", strategy_text)
        self.assertNotIn("净额决策", strategy_text)

        risk_text = responses["risk_js"].text
        self.assertIn("启盘前自检", risk_text)
        self.assertIn("小资金运行包", risk_text)
        self.assertIn("guardedLivePreflight", risk_text)
        self.assertIn("guardedLiveRunPacket", risk_text)
        self.assertIn("你现在先做什么", risk_text)
        self.assertIn("当前主任务", risk_text)
        self.assertIn("为什么先做这一步", risk_text)
        self.assertIn("做完后会怎样", risk_text)
        self.assertIn("轻度差异，建议观察", risk_text)
        self.assertIn("系统仍处于人工确认流程", risk_text)
        self.assertIn('action.client_action === "navigate-view" && action.value === "risk"', risk_text)
        self.assertIn("进入独立工作台", risk_text)
        self.assertNotIn("继续保持暂停", risk_text)

        # Slice #5 refactor：rebaseline / validate 动作按钮已从 risk-view.js
        # 搬到 reconciliation-controls.js 共享模块。下面两条断言跟随搬迁，
        # 其余 "轻度差异，建议观察" / "系统仍处于人工确认流程" 这类 risk-view
        # 自己的叙述文案仍留在 risk-view.js 里。
        reconciliation_controls_text = responses["reconciliation_controls_js"].text
        self.assertIn("重新对账（刷新交易所状态）", reconciliation_controls_text)
        self.assertIn("接受当前状态为新基线", reconciliation_controls_text)

        exit_execution_text = responses["exit_execution_js"].text
        self.assertIn("renderExitExecutionView", exit_execution_text)
        self.assertIn("退出任务独立工作台", exit_execution_text)
        self.assertIn("完整处理列表", exit_execution_text)
        self.assertIn("renderExitExecutionWorkspace", exit_execution_text)

        refresh_interactivity_text = responses["refresh_interactivity_js"].text
        self.assertIn("syncRefreshDisabledButtons", refresh_interactivity_text)
        self.assertIn("当前区域正在刷新，请等待刷新完成后再操作。", refresh_interactivity_text)

    def test_snapshot_loading_meta_keeps_panel_pending_until_fresh_snapshot_arrives(self) -> None:
        script = """
import {
  createState,
  mergedPendingPanels,
  syncSnapshotPendingPanelState,
} from './aats/api/static/modules/store.js';

const state = createState();
state.pendingPanels.primaryPanel = 7;

syncSnapshotPendingPanelState(state, 'rdpWorkbenchItems', {
  source: 'dashboard_snapshot',
  loading: true,
  refreshing: true,
});

const withLoading = mergedPendingPanels(state);

syncSnapshotPendingPanelState(state, 'rdpWorkbenchItems', {
  source: 'dashboard_snapshot',
  loading: false,
  refreshing: false,
});

const afterFresh = mergedPendingPanels(state);

console.log(JSON.stringify({
  snapshotPendingWhileLoading: Boolean(withLoading.rdpWorkbenchItems),
  primaryPendingPreserved: withLoading.primaryPanel === 7,
  snapshotPendingCleared: !Object.prototype.hasOwnProperty.call(afterFresh, 'rdpWorkbenchItems'),
  primaryPendingStillPreserved: afterFresh.primaryPanel === 7,
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["snapshotPendingWhileLoading"])
        self.assertTrue(payload["primaryPendingPreserved"])
        self.assertTrue(payload["snapshotPendingCleared"])
        self.assertTrue(payload["primaryPendingStillPreserved"])

    def test_dashboard_app_bundle_wires_exit_execution_review_actions(self) -> None:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(ui_router)
        app.state.runtime = SimpleNamespace(settings=AATSSettings.model_validate({}))

        with TestClient(app) as client:
            app_js = client.get("/ui/app.js")
            navigation_state_js = client.get("/ui/modules/navigation-state.js")
            risk_actions_js = client.get("/ui/modules/actions/risk-actions.js")
            risk_js = client.get("/ui/modules/views/risk-view.js")
            exit_execution_js = client.get("/ui/modules/views/exit-execution-view.js")
            # Slice #36 refactor：exit-execution review helper 已从 risk-view.js 搬到
            # 共享模块 exit-execution-helpers.js
            exit_execution_helpers_js = client.get(
                "/ui/modules/exit-execution-helpers.js"
            )

        self.assertEqual(app_js.status_code, 200)
        self.assertEqual(navigation_state_js.status_code, 200)
        self.assertEqual(risk_actions_js.status_code, 200)
        self.assertEqual(risk_js.status_code, 200)
        self.assertEqual(exit_execution_js.status_code, 200)
        self.assertEqual(exit_execution_helpers_js.status_code, 200)

        app_js_text = app_js.text
        navigation_state_text = navigation_state_js.text
        risk_actions_text = risk_actions_js.text
        self.assertIn("createRiskActionHandlers", app_js_text)
        self.assertIn("handleExitExecutionHistoryFilterEvent", app_js_text)
        self.assertIn("applyExitExecutionHistoryFilters", app_js_text)
        self.assertIn("hydrateViewStateFromLocation", app_js_text)
        self.assertIn("syncActiveViewLocationState", app_js_text)
        self.assertIn("createNavigationStateController", app_js_text)
        self.assertIn("buildExitExecutionViewPath", navigation_state_text)
        self.assertIn("syncActiveViewLocationState", navigation_state_text)
        self.assertIn("trigger-exit-execution-refresh", risk_actions_text)
        self.assertIn("trigger-exit-execution-retry-limit-lookup", risk_actions_text)
        self.assertIn("trigger-exit-execution-safe-cancel", risk_actions_text)
        self.assertIn("/system/exit-execution/refresh", risk_actions_text)
        self.assertIn("/system/exit-execution/retry-limit-lookup", risk_actions_text)
        self.assertIn("/system/exit-execution/safe-cancel", risk_actions_text)
        self.assertIn("apply-exit-execution-history-workspace", risk_actions_text)
        self.assertIn("paginate-exit-execution-history", risk_actions_text)

        risk_js_text = risk_js.text
        self.assertIn("退出任务人工处理", risk_js_text)
        self.assertIn("mergedExitExecutionReviewItems", risk_js_text)
        self.assertIn("trigger-exit-execution-refresh", risk_js_text)
        self.assertIn("trigger-exit-execution-retry-limit-lookup", risk_js_text)
        self.assertIn("trigger-exit-execution-safe-cancel", risk_js_text)
        self.assertIn("risk-exit-workspace", risk_js_text)
        self.assertIn("renderExitExecutionWorkspace", risk_js_text)
        self.assertIn("进入独立工作台", risk_js_text)

        # Slice #36 refactor：renderExitExecutionActionFilterOptions 及
        # data-exit-history-filter 已整体搬到 exit-execution-helpers.js
        exit_execution_helpers_text = exit_execution_helpers_js.text
        self.assertIn('data-exit-history-filter="action"', exit_execution_helpers_text)
        self.assertIn("renderExitExecutionActionFilterOptions", exit_execution_helpers_text)

        exit_execution_js_text = exit_execution_js.text
        self.assertIn("renderExitExecutionView", exit_execution_js_text)
        self.assertIn("退出任务独立工作台", exit_execution_js_text)
        self.assertIn("renderExitExecutionWorkspace", exit_execution_js_text)

    def test_strategy_view_surfaces_protective_overlay_config_and_state(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        shorting_runtime_supported: true,
        short_bias_enabled: true,
        runtime_shorting_blockers: [],
        short_entry_allowed_regimes: ['trend', 'uncertain'],
        short_entry_min_signal_edge_bps: 12,
        short_entry_alpha_min: 0.18,
        short_entry_confidence_min: 0.58,
        short_scale_in_min_signal_edge_bps: 10,
        short_scale_in_alpha_min: 0.20,
        short_scale_in_confidence_min: 0.60,
        short_reversal_min_signal_edge_bps: 20,
        short_reversal_alpha_min: 0.28,
        short_reversal_confidence_min: 0.72,
        entry_min_signal_edge_bps: 14,
        entry_alpha_min: 0.18,
        entry_confidence_min: 0.66,
        hedge_overlay_enabled: true,
        hedge_protective_enabled: true,
        hedge_overlay_mode: 'protective',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_effective_enabled: true,
        hedge_open_threshold: 0.58,
        hedge_close_threshold: 0.42,
        hedge_max_ratio: 0.50,
        hedge_min_hold_seconds: 300,
        hedge_rebalance_cooldown_seconds: 120,
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    baseline_assessment: {
      direction_bias: 'short',
      composite_alpha_score: -0.30,
      confidence: 0.84,
    },
    ai_assessment: {
      directional_edge: -0.12,
    },
    position_target: {
      position_intent: 'reduce_long',
      target_exposure_side: 'long',
      current_position_qty: 0.03,
      target_position_qty: 0.02,
      delta_position_qty: -0.01,
      guardrail_flags: ['protective_hedge_overlay_active'],
      hedge_overlay_decision: {
        enabled: true,
        runtime_supported: true,
        configured_mode: 'protective',
        active: true,
        state: 'opening',
        main_leg_signal: 'long',
        hedge_leg_signal: 'short',
        main_leg_current_qty: 0.05,
        hedge_leg_current_qty: 0.0,
        main_leg_target_qty: 0.04,
        hedge_leg_target_qty: 0.02,
        hedge_ratio: 0.5,
        max_ratio: 0.5,
        pressure_score: 0.76,
        open_threshold: 0.58,
        close_threshold: 0.42,
        reason_codes: ['protective_overlay_pressure_above_open_threshold'],
        blocked_reasons: [],
        min_hold_remaining_seconds: 0,
        rebalance_cooldown_remaining_seconds: 0,
      },
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesProtectiveReference: !html.includes('strategy_hedge_protective_enabled')
    && !html.includes('strategy_hedge_open_threshold / strategy_hedge_close_threshold')
    && !html.includes('strategy_hedge_max_ratio'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesProtectiveReference":true', result.stdout)

    def test_strategy_view_surfaces_opportunistic_overlay_config_and_state(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        shorting_runtime_supported: true,
        short_bias_enabled: true,
        runtime_shorting_blockers: [],
        short_entry_allowed_regimes: ['trend', 'uncertain'],
        short_entry_min_signal_edge_bps: 12,
        short_entry_alpha_min: 0.18,
        short_entry_confidence_min: 0.58,
        short_scale_in_min_signal_edge_bps: 10,
        short_scale_in_alpha_min: 0.20,
        short_scale_in_confidence_min: 0.60,
        short_reversal_min_signal_edge_bps: 20,
        short_reversal_alpha_min: 0.28,
        short_reversal_confidence_min: 0.72,
        entry_min_signal_edge_bps: 14,
        entry_alpha_min: 0.18,
        entry_confidence_min: 0.66,
        hedge_overlay_enabled: true,
        hedge_overlay_mode: 'opportunistic',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_enabled_in_mode: true,
        hedge_overlay_mode_ready: true,
        hedge_overlay_effective_enabled: true,
        hedge_open_threshold: 0.58,
        hedge_close_threshold: 0.42,
        hedge_max_ratio: 0.50,
        hedge_min_hold_seconds: 300,
        hedge_rebalance_cooldown_seconds: 120,
        hedge_opportunistic_enabled: true,
        hedge_opportunistic_open_threshold: 0.62,
        hedge_opportunistic_close_threshold: 0.46,
        hedge_opportunistic_max_ratio: 0.35,
        hedge_opportunistic_min_hold_seconds: 180,
        hedge_opportunistic_rebalance_cooldown_seconds: 90,
        hedge_opportunistic_max_fee_drag_ratio: 0.18,
        hedge_opportunistic_max_churn_ratio: 0.22,
        hedge_opportunistic_min_safe_net_edge_bps: 3.0,
        hedge_opportunistic_expected_slippage_buffer_bps: 1.0,
        hedge_opportunistic_expected_execution_buffer_bps: 2.0,
        hedge_opportunistic_weak_edge_execution_mode: 'report_only',
        hedge_opportunistic_max_acceptable_cost_bps: 7.5,
        hedge_opportunistic_passive_first_enabled: true,
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    baseline_assessment: {
      direction_bias: 'long',
      composite_alpha_score: 0.28,
      confidence: 0.84,
    },
    ai_assessment: {
      directional_edge: -0.25,
    },
    position_target: {
      position_intent: 'hold',
      target_exposure_side: 'long',
      current_position_qty: 0.05,
      target_position_qty: 0.03,
      delta_position_qty: -0.02,
      book_expectancy_summary: {
        source: 'opportunistic_overlay',
        books: [
          {
            leg: 'short',
            expected_gross_edge_bps: 8.0,
            expected_signal_edge_bps: 8.0,
            expected_slippage_bps: 1.0,
            expected_cost_bps: 4.0,
            expected_net_edge_bps: 4.0,
            required_safe_net_edge_bps: 6.0,
            max_acceptable_cost_bps: 7.5,
            weak_edge_execution_mode: 'report_only',
            weak_edge_report_only: true,
            passive_first_required: true,
          },
        ],
      },
      guardrail_flags: ['opportunistic_hedge_overlay_active'],
      hedge_overlay_decision: {
        enabled: true,
        runtime_supported: true,
        configured_mode: 'opportunistic',
        effective_mode: 'opportunistic',
        active: true,
        state: 'opening',
        main_leg_signal: 'long',
        hedge_leg_signal: 'short',
        main_leg_current_qty: 0.05,
        hedge_leg_current_qty: 0.0,
        main_leg_target_qty: 0.04,
        hedge_leg_target_qty: 0.014,
        hedge_ratio: 0.35,
        max_ratio: 0.35,
        pressure_score: 0.71,
        open_threshold: 0.62,
        close_threshold: 0.46,
        reason_codes: ['opportunistic_overlay_signal_above_open_threshold'],
        blocked_reasons: [],
        min_hold_remaining_seconds: 0,
        rebalance_cooldown_remaining_seconds: 0,
      },
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesOpportunisticReference: !html.includes('strategy_hedge_opportunistic_open_threshold / strategy_hedge_opportunistic_close_threshold')
    && !html.includes('strategy_hedge_opportunistic_min_safe_net_edge_bps / strategy_hedge_opportunistic_expected_slippage_buffer_bps / strategy_hedge_opportunistic_expected_execution_buffer_bps')
    && !html.includes('strategy_hedge_opportunistic_weak_edge_execution_mode / strategy_hedge_opportunistic_max_acceptable_cost_bps / strategy_hedge_opportunistic_passive_first_enabled'),
}));
"""
        result = _run_node_module(script)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesOpportunisticReference":true', result.stdout)

    def _obsolete_test_strategy_view_surfaces_overlay_residual_close_summary_copy(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const sharedPayload = {
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        hedge_overlay_enabled: true,
        hedge_overlay_mode: 'protective',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_mode_ready: true,
        hedge_overlay_effective_enabled: true,
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {
      configured_active_family: 'directional',
      latest_selected_family: 'protective',
      latest_selected_state: 'closing',
      latest_selected_route_action: 'override_target',
      latest_selected_family_action: 'close_protection_leg',
      latest_bundle_status: 'ready',
      latest_portfolio_requested_notional: 0,
      latest_portfolio_approved_notional: 0,
      latest_portfolio_budget_cut_notional: 0,
      latest_hedge_protected_notional: 0,
      latest_directional_reduced_notional: 0,
      latest_selection_reason_codes: [],
      protective_fallback_active: false,
      operator_summary: '',
    },
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
    smart_arbitrage_cost_summary: {},
  },
  latestDecision: {
    baseline_assessment: {},
    ai_assessment: {},
    position_target: {},
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
};

const protectiveHtml = renderStrategyView({
  ...sharedPayload,
  strategyRuntime: {
    ...sharedPayload.strategyRuntime,
    latest_snapshot: {
      candidates: [
        {
          family: 'protective',
          state: 'closing',
          route_action: 'override_target',
          family_action: 'close_protection_leg',
          urgency: 'low',
          symbol: 'BTC-USDT-SWAP',
          reason_codes: ['protective_overlay_main_signal_inferred_from_inventory'],
        },
      ],
      automation_decisions: [],
    },
    summary: {
      ...sharedPayload.strategyRuntime.summary,
      operator_summary: '当前选中的策略家族正在收回保护腿。',
    },
    latest_allocation_decision: {
      operator_summary: '当前 allocator v2 已批准收回保护腿的账户级执行目标。',
      reason_codes: [],
    },
    recent_sleeve_intents: [
      {
        strategy_sleeve_id: 'protective_close',
        family: 'protective',
        symbol: 'BTC-USDT-SWAP',
        state: 'closing',
        route_action: 'override_target',
        family_action: 'close_protection_leg',
        automatic_enabled: true,
        budget_multiplier: 1,
        allocator_weight: 1,
        reason_codes: ['protective_overlay_main_signal_inferred_from_inventory'],
        control_reason_codes: [],
      },
    ],
  },
});

const opportunityHtml = renderStrategyView({
  ...sharedPayload,
  strategyRuntime: {
    ...sharedPayload.strategyRuntime,
    latest_snapshot: {
      candidates: [
        {
          family: 'opportunistic',
          state: 'closing',
          route_action: 'override_target',
          family_action: 'close_opportunity_leg',
          urgency: 'low',
          symbol: 'BTC-USDT-SWAP',
          reason_codes: ['opportunistic_overlay_main_signal_inferred_from_inventory'],
        },
      ],
      automation_decisions: [],
    },
    summary: {
      ...sharedPayload.strategyRuntime.summary,
      latest_selected_family: 'opportunistic',
      latest_selected_family_action: 'close_opportunity_leg',
      operator_summary: '当前选中的策略家族正在收回机会腿。',
    },
    latest_allocation_decision: {
      operator_summary: '当前 allocator v2 已批准收回机会腿的账户级执行目标。',
      reason_codes: [],
    },
    recent_sleeve_intents: [
      {
        strategy_sleeve_id: 'opportunistic_close',
        family: 'opportunistic',
        symbol: 'BTC-USDT-SWAP',
        state: 'closing',
        route_action: 'override_target',
        family_action: 'close_opportunity_leg',
        automatic_enabled: true,
        budget_multiplier: 1,
        allocator_weight: 1,
        reason_codes: ['opportunistic_overlay_main_signal_inferred_from_inventory'],
        control_reason_codes: [],
      },
    ],
  },
});

console.log(JSON.stringify({
  hasProtectiveCloseCopy: protectiveHtml.includes('当前选中的策略家族正在收回保护腿。'),
  hasProtectiveRouteLabel: (protectiveHtml.match(/收回保护腿/g) || []).length >= 3,
  hasProtectiveAllocatorCopy: protectiveHtml.includes('当前 allocator v2 已批准收回保护腿的账户级执行目标。'),
  hasOpportunityCloseCopy: opportunityHtml.includes('当前选中的策略家族正在收回机会腿。'),
  hasOpportunityRouteLabel: (opportunityHtml.match(/收回机会腿/g) || []).length >= 3,
  hasOpportunityAllocatorCopy: opportunityHtml.includes('当前 allocator v2 已批准收回机会腿的账户级执行目标。'),
}));
"""
        result = _run_node_module(script)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hasProtectiveCloseCopy":true', result.stdout)
        self.assertIn('"hasProtectiveRouteLabel":true', result.stdout)
        self.assertIn('"hasProtectiveAllocatorCopy":true', result.stdout)
        self.assertIn('"hasOpportunityCloseCopy":true', result.stdout)
        self.assertIn('"hasOpportunityRouteLabel":true', result.stdout)
        self.assertIn('"hasOpportunityAllocatorCopy":true', result.stdout)

    def _obsolete_test_strategy_view_surfaces_opportunistic_execution_discipline_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        hedge_overlay_enabled: true,
        hedge_overlay_mode: 'opportunistic',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_enabled_in_mode: true,
        hedge_overlay_mode_ready: true,
        hedge_overlay_effective_enabled: true,
        hedge_opportunistic_enabled: true,
        hedge_opportunistic_open_threshold: 0.62,
        hedge_opportunistic_close_threshold: 0.46,
        hedge_opportunistic_min_safe_net_edge_bps: 3.0,
        hedge_opportunistic_expected_slippage_buffer_bps: 1.0,
        hedge_opportunistic_expected_execution_buffer_bps: 2.0,
        hedge_opportunistic_weak_edge_execution_mode: 'report_only',
        hedge_opportunistic_max_acceptable_cost_bps: 7.5,
        hedge_opportunistic_passive_first_enabled: true,
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    baseline_assessment: { direction_bias: 'long', composite_alpha_score: 0.28, confidence: 0.84 },
    ai_assessment: { directional_edge: -0.25 },
    position_target: {
      position_intent: 'hold',
      target_exposure_side: 'long',
      current_position_qty: 0.05,
      target_position_qty: 0.03,
      delta_position_qty: -0.02,
      book_expectancy_summary: {
        source: 'opportunistic_overlay',
        books: [
          {
            leg: 'short',
            expected_gross_edge_bps: 8.0,
            expected_signal_edge_bps: 8.0,
            expected_slippage_bps: 1.0,
            expected_cost_bps: 4.0,
            expected_net_edge_bps: 4.0,
            required_safe_net_edge_bps: 6.0,
            max_acceptable_cost_bps: 7.5,
            weak_edge_execution_mode: 'report_only',
            weak_edge_report_only: true,
            passive_first_required: true,
          },
        ],
      },
      guardrail_flags: ['opportunistic_hedge_overlay_active'],
      hedge_overlay_decision: {
        enabled: true,
        runtime_supported: true,
        configured_mode: 'opportunistic',
        effective_mode: 'opportunistic',
        active: true,
        state: 'opening',
        main_leg_signal: 'long',
        hedge_leg_signal: 'short',
        main_leg_current_qty: 0.05,
        hedge_leg_current_qty: 0.0,
        main_leg_target_qty: 0.04,
        hedge_leg_target_qty: 0.014,
        hedge_ratio: 0.35,
        max_ratio: 0.35,
        pressure_score: 0.71,
        open_threshold: 0.62,
        close_threshold: 0.46,
        reason_codes: ['opportunistic_overlay_signal_above_open_threshold'],
        blocked_reasons: [],
      },
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hasExpectancySummary:
    html.includes('空腿 毛/成本/净 8.00/4.00/4.00 基点')
    && html.includes('安全净边际 6.00 基点')
    && html.includes('弱边际 仅报告')
    && html.includes('本轮只保留报告')
    && html.includes('要求被动优先'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hasExpectancySummary":true', result.stdout)

    def test_strategy_view_surfaces_independent_overlay_config_and_state(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        shorting_runtime_supported: true,
        short_bias_enabled: true,
        runtime_shorting_blockers: [],
        short_entry_allowed_regimes: ['trend', 'uncertain'],
        short_entry_min_signal_edge_bps: 12,
        short_entry_alpha_min: 0.18,
        short_entry_confidence_min: 0.58,
        short_scale_in_min_signal_edge_bps: 10,
        short_scale_in_alpha_min: 0.20,
        short_scale_in_confidence_min: 0.60,
        short_reversal_min_signal_edge_bps: 20,
        short_reversal_alpha_min: 0.28,
        short_reversal_confidence_min: 0.72,
        entry_min_signal_edge_bps: 14,
        entry_alpha_min: 0.18,
        entry_confidence_min: 0.66,
        hedge_overlay_enabled: true,
        hedge_overlay_mode: 'independent',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_mode_ready: true,
        hedge_overlay_effective_enabled: true,
        hedge_independent_enabled: true,
        hedge_independent_long_entry_threshold: 0.66,
        hedge_independent_short_entry_threshold: 0.64,
        hedge_independent_long_close_threshold: 0.52,
        hedge_independent_short_close_threshold: 0.50,
        hedge_independent_long_scale_in_threshold: 0.70,
        hedge_independent_short_scale_in_threshold: 0.68,
        hedge_independent_long_min_hold_seconds: 300,
        hedge_independent_short_min_hold_seconds: 420,
        hedge_independent_rebalance_cooldown_seconds: 120,
        hedge_independent_trial_guard_enabled: true,
        hedge_independent_min_confirm_ticks: 2,
        hedge_independent_min_score_stability_bps: 2.0,
        hedge_independent_min_score_drawdown_bps: 6.0,
        hedge_independent_effective_score_drawdown_bps: 6.0,
        hedge_independent_min_liquidity_quality: 0.55,
        hedge_independent_require_execution_health_ok: true,
        hedge_independent_max_thesis_age_seconds: 1800,
        hedge_independent_de_risk_net_edge_bps: 2.0,
        hedge_independent_failed_thesis_net_edge_bps: -1.0,
        hedge_independent_execution_health_de_risk_enabled: true,
        hedge_independent_liquidity_de_risk_enabled: true,
        hedge_independent_min_safe_net_edge_bps: 3.0,
        hedge_independent_expected_slippage_buffer_bps: 1.0,
        hedge_independent_expected_execution_buffer_bps: 2.0,
        hedge_independent_weak_edge_execution_mode: 'report_only',
        hedge_independent_max_acceptable_cost_bps: 7.5,
        hedge_independent_passive_first_enabled: true,
        hedge_independent_entry_execution_mode: 'passive_first',
        hedge_independent_scale_in_execution_mode: 'bounded_limit',
        hedge_independent_de_risk_execution_mode: 'bounded_taker',
        hedge_independent_close_failed_thesis_execution_mode: 'aggressive_bounded_taker',
        hedge_independent_close_stale_execution_mode: 'bounded_limit',
        hedge_independent_limit_offset_bps_entry: 1.5,
        hedge_independent_limit_offset_bps_scale_in: 1.0,
        hedge_independent_limit_offset_bps_stale_close: 0.8,
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    baseline_assessment: {
      direction_bias: 'long',
      composite_alpha_score: 0.31,
      confidence: 0.82,
    },
    ai_assessment: {
      directional_edge: 0.18,
    },
    position_target: {
      position_intent: 'hold',
      target_exposure_side: 'long',
      current_position_qty: 0.01,
      target_position_qty: 0.01,
      delta_position_qty: 0,
      guardrail_flags: ['independent_books_active'],
      strategy_execution_legs: [
        {
          pos_side: 'long',
          execution_mode: 'independent_long_book',
          overlay_mode: 'independent',
          current_position_qty: 0.01,
          target_position_qty: 0.03,
        },
        {
          pos_side: 'short',
          execution_mode: 'independent_short_book',
          overlay_mode: 'independent',
          current_position_qty: -0.02,
          target_position_qty: -0.01,
        },
      ],
      hedge_overlay_decision: {
        enabled: true,
        runtime_supported: true,
        configured_mode: 'independent',
        effective_mode: 'independent',
        active: true,
        state: 'holding',
        long_leg_score: 0.74,
        short_leg_score: 0.68,
        long_leg_reason_codes: ['independent_long_book_signal_above_entry_threshold'],
        short_leg_reason_codes: ['independent_short_book_hold_above_entry_threshold'],
        long_leg_blocked_reasons: ['independent_long_book_trial_guard_active'],
        short_leg_blocked_reasons: [],
        reason_codes: ['independent_long_book_signal_above_entry_threshold'],
        blocked_reasons: [],
      },
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesIndependentReference: !html.includes('strategy_hedge_independent_long_entry_threshold / strategy_hedge_independent_short_entry_threshold')
    && !html.includes('strategy_hedge_independent_min_confirm_ticks / strategy_hedge_independent_effective_score_drawdown_bps / strategy_hedge_independent_min_liquidity_quality')
    && !html.includes('strategy_hedge_independent_entry_execution_mode / strategy_hedge_independent_scale_in_execution_mode / strategy_hedge_independent_de_risk_execution_mode / strategy_hedge_independent_close_failed_thesis_execution_mode / strategy_hedge_independent_close_stale_execution_mode'),
}));
"""
        result = _run_node_module(script)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesIndependentReference":true', result.stdout)

    def test_strategy_view_module_uses_updated_score_stability_copy(self) -> None:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(ui_router)
        app.state.runtime = SimpleNamespace(settings=AATSSettings.model_validate({}))

        with TestClient(app) as client:
            response = client.get("/ui/modules/views/strategy-view.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("上行抬升幅度", response.text)
        self.assertIn("向下回撤幅度", response.text)
        self.assertNotIn("确认次数 / 回撤阈值 / 流动性门槛", response.text)
        self.assertNotIn("分数回撤仍受控", response.text)

    def test_strategy_view_surfaces_overlay_rollout_stage_and_rollback_order(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        shorting_runtime_supported: true,
        short_bias_enabled: true,
        runtime_shorting_blockers: [],
        entry_min_signal_edge_bps: 14,
        entry_alpha_min: 0.18,
        entry_confidence_min: 0.66,
        hedge_overlay_enabled: true,
        hedge_overlay_mode: 'independent',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_enabled_in_mode: true,
        hedge_overlay_mode_ready: false,
        hedge_overlay_rollout_allowed: false,
        hedge_overlay_effective_enabled: false,
        hedge_opportunistic_enabled: true,
        hedge_opportunistic_rollout_stage: 'live',
        hedge_independent_enabled: true,
        hedge_independent_rollout_stage: 'dry_run',
        hedge_rollout: {
          runtime_stage: 'live',
          current_mode: 'independent',
          current_mode_allowed: false,
          current_mode_blocking_reasons: ['independent_overlay_rollout_stage_blocks_live_runtime'],
          current_mode_summary: '独立双书当前只放开到 dry-run，这条实盘运行线不会启用。',
          rollback_sequence: [
            '先关闭 strategy_hedge_opportunistic_enabled',
            '再关闭 strategy_hedge_independent_enabled',
            '保留 protective 作为最后兜底',
            '如需彻底回退，再把 strategy_hedge_overlay_mode 切回 protective',
          ],
          opportunistic: {
            configured_rollout_stage: 'live',
            runtime_allowed: true,
            blocking_reasons: [],
            summary: 'opportunistic 已放开到实盘；仍建议先看回放和 dry-run 样本再开启。',
          },
          independent: {
            configured_rollout_stage: 'dry_run',
            runtime_allowed: false,
            blocking_reasons: ['independent_overlay_rollout_stage_blocks_live_runtime'],
            summary: '独立双书当前只放开到 dry-run，这条实盘运行线不会启用。',
          },
        },
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    baseline_assessment: {
      direction_bias: 'long',
      composite_alpha_score: 0.31,
      confidence: 0.82,
    },
    ai_assessment: {
      directional_edge: 0.18,
    },
    position_target: {
      position_intent: 'hold',
      target_exposure_side: 'long',
      current_position_qty: 0.01,
      target_position_qty: 0.01,
      delta_position_qty: 0,
      hedge_overlay_decision: {
        enabled: true,
        runtime_supported: true,
        configured_mode: 'independent',
        effective_mode: 'independent',
        state: 'blocked',
        blocked_reasons: ['independent_overlay_rollout_stage_blocks_live_runtime'],
        rollout_stage: 'dry_run',
        runtime_rollout_stage: 'live',
      },
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesRolloutReference: !html.includes('strategy_hedge_opportunistic_rollout_stage / strategy_hedge_independent_rollout_stage')
    && !html.includes('先关闭 strategy_hedge_opportunistic_enabled')
    && !html.includes('独立双书当前只放开到 dry-run，这条实盘运行线不会启用'),
}));
"""
        result = _run_node_module(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesRolloutReference":true', result.stdout)

    def test_dashboard_redirects_to_login_when_auth_is_enabled(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "operator_auth_enabled": True,
                "operator_session_secret": "session-secret",
            }
        )
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(ui_router)
        app.state.runtime = SimpleNamespace(settings=settings)

        with TestClient(app) as client:
            root = client.get("/", follow_redirects=False)
            ai = client.get("/ui/ai", follow_redirects=False)
            ai_analysis = client.get("/ui/ai-analysis", follow_redirects=False)
            ai_config = client.get("/ui/ai-config", follow_redirects=False)
            login = client.get("/login")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login?reason=operator_auth_required")
        self.assertEqual(ai.status_code, 303)
        self.assertEqual(ai.headers["location"], "/login?reason=operator_auth_required")
        self.assertEqual(ai_analysis.status_code, 303)
        self.assertEqual(ai_analysis.headers["location"], "/login?reason=operator_auth_required")
        self.assertEqual(ai_config.status_code, 303)
        self.assertEqual(ai_config.headers["location"], "/login?reason=operator_auth_required")
        self.assertEqual(login.status_code, 200)
        self.assertIn("loginLead", login.text)
        self.assertIn("login", login.text.lower())

    def test_build_dashboard_bundle_path_uses_frontend_panel_registry(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDashboardBundlePath } from './aats/api/static/modules/store.js';

const path = buildDashboardBundlePath('strategy', {
  pageLimits: {
    recentDecisions: 5,
    recentOrders: 7,
    recentFills: 9,
  },
});
const url = new URL(path, 'http://localhost');
console.log(JSON.stringify({
  view: url.searchParams.get('view'),
  recentDecisions: url.searchParams.get('recentDecisions'),
  recentOrders: url.searchParams.get('recentOrders'),
  recentFills: url.searchParams.get('recentFills'),
  recentAIAssessments: url.searchParams.get('recentAIAssessments'),
  recentAIShadowDecisions: url.searchParams.get('recentAIShadowDecisions'),
  recentAIShadowEvaluations: url.searchParams.get('recentAIShadowEvaluations'),
  panels: url.searchParams.getAll('panel'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["view"], "strategy")
        self.assertEqual(payload["recentDecisions"], "5")
        self.assertEqual(payload["recentOrders"], "7")
        self.assertEqual(payload["recentFills"], "9")
        self.assertEqual(payload["recentAIAssessments"], "8")
        self.assertEqual(payload["recentAIShadowDecisions"], "8")
        self.assertEqual(payload["recentAIShadowEvaluations"], "8")
        self.assertEqual(
            payload["panels"],
            [
                "session",
                "authProviders",
                "health",
                "mode",
                "runtime",
                "systemRecovery",
                "blockerControl",
                "aiRuntime",
                "strategyRuntime",
                "strategyAttribution",
                "positionLifecycleAttribution",
                "latestDecision",
                "recentDecisions",
                "executionLatest",
                "trialReviewSummary",
                "trialReviewHistory",
            ],
        )

    def test_risk_view_dashboard_bundle_request_plan_defers_replay_and_exit_history(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDashboardBundleRequestPlan, buildDashboardBundlePath } from './aats/api/static/modules/store.js';

const plan = buildDashboardBundleRequestPlan('risk', {
  ui: {
    risk: {
      exitExecutionHistory: {
        action: 'all',
        parent: '',
        actor: '',
        windowHours: 'all',
        offset: 0,
        limit: 20,
      },
    },
  },
});
const full = new URL(buildDashboardBundlePath('risk'), 'http://localhost');
const primary = new URL(plan.primaryPath, 'http://localhost');
const deferred = new URL(plan.deferredPath, 'http://localhost');
console.log(JSON.stringify({
  fullPanels: full.searchParams.getAll('panel'),
  primaryPanels: primary.searchParams.getAll('panel'),
  deferredPanels: deferred.searchParams.getAll('panel'),
  deferredPlanPanels: plan.deferredPanels,
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn("replayStatus", payload["fullPanels"])
        self.assertIn("exitExecutionActionHistoryPage", payload["fullPanels"])
        self.assertIn("trialGuard", payload["fullPanels"])
        self.assertIn("guardedLivePreflight", payload["fullPanels"])
        self.assertIn("guardedLiveRunPacket", payload["fullPanels"])
        self.assertNotIn("mode", payload["fullPanels"])
        self.assertNotIn("runtime", payload["fullPanels"])
        self.assertNotIn("replayStatus", payload["primaryPanels"])
        self.assertNotIn("exitExecutionActionHistoryPage", payload["primaryPanels"])
        self.assertNotIn("trialGuard", payload["primaryPanels"])
        self.assertNotIn("guardedLivePreflight", payload["primaryPanels"])
        self.assertNotIn("guardedLiveRunPacket", payload["primaryPanels"])
        self.assertNotIn("phase1Shadow", payload["fullPanels"])
        self.assertEqual(
            payload["deferredPanels"],
            [
                "trialGuard",
                "guardedLivePreflight",
                "guardedLiveRunPacket",
                "replayStatus",
                "exitExecutionActionHistoryPage",
            ],
        )
        self.assertEqual(
            payload["deferredPlanPanels"],
            [
                "trialGuard",
                "guardedLivePreflight",
                "guardedLiveRunPacket",
                "replayStatus",
                "exitExecutionActionHistoryPage",
            ],
        )

    def test_second_round_dashboard_request_plan_defers_slow_page_details(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDashboardBundleRequestPlan } from './aats/api/static/modules/store.js';

const views = ['execution', 'exitExecution', 'replay', 'aiAnalysis', 'rdp'];
const payload = Object.fromEntries(views.map((view) => {
  const plan = buildDashboardBundleRequestPlan(view, {
    ui: {
      exitExecution: {
        exitExecutionHistory: {
          action: 'all',
          parent: '',
          actor: '',
          windowHours: 'all',
          offset: 0,
          limit: 50,
        },
      },
    },
  });
  return [view, {
    primaryPanels: plan.primaryPanels,
    deferredPanels: plan.deferredPanels,
  }];
}));
console.log(JSON.stringify(payload));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertIn("positionLifecycleAttribution", payload["execution"]["deferredPanels"])
        self.assertNotIn("positionLifecycleAttribution", payload["execution"]["primaryPanels"])
        self.assertIn("exitExecutionActionHistoryPage", payload["exitExecution"]["deferredPanels"])
        self.assertNotIn("exitExecutionActionHistoryPage", payload["exitExecution"]["primaryPanels"])
        self.assertEqual(payload["replay"]["deferredPanels"], ["replayStatus", "replayRecentValidations"])
        self.assertNotIn("replayStatus", payload["replay"]["primaryPanels"])
        self.assertNotIn("replayRecentValidations", payload["replay"]["primaryPanels"])
        for panel_key in ["aiRecent", "aiShadowRecent", "aiShadowEvaluations", "profileControlSummary"]:
            self.assertIn(panel_key, payload["aiAnalysis"]["deferredPanels"])
            self.assertNotIn(panel_key, payload["aiAnalysis"]["primaryPanels"])
        for panel_key in ["rdpWorkbenchItems", "rdpWorkbenchAlerts", "rdpTuningProposals"]:
            self.assertIn(panel_key, payload["rdp"]["deferredPanels"])
            self.assertNotIn(panel_key, payload["rdp"]["primaryPanels"])
        self.assertIn("rdpWorkbenchOverview", payload["rdp"]["primaryPanels"])
        self.assertIn("rdpTuningOverview", payload["rdp"]["primaryPanels"])

    def test_rdp_view_shows_loading_state_for_deferred_detail_panels(self) -> None:
        script = """
import { renderRdpView } from './aats/api/static/modules/views/rdp-view.js';

const html = renderRdpView({
  session: { role: 'admin', authenticated: true },
  authProviders: { auth_enabled: true },
  rdpControl: { environment: { name: 'test' }, observation_queue: [] },
  rdpWorkbenchOverview: {
    overall_status: 'ready',
    headline: 'RDP ready',
    health: { daemon: 'healthy', latest_gate: 'pass' },
    current_execution: {},
    next_queue: {},
  },
  rdpTuningOverview: { pending_review_count: 1, active_override_count: 0 },
  uiHints: {
    pendingPanels: {
      rdpWorkbenchItems: true,
      rdpWorkbenchAlerts: true,
      rdpTuningProposals: true,
    },
  },
});

console.log(JSON.stringify({
  hasItemsLoading: html.includes('待处理组合正在加载'),
  hasAlertsLoading: html.includes('阻断明细正在加载'),
  hasTuningLoading: html.includes('调优提案正在加载'),
  avoidsFalseEmptyItems: !html.includes('当前没有新的待处理组合。'),
  avoidsFalseEmptyAlerts: !html.includes('当前没有新的阻断。'),
  avoidsFalseEmptyTuning: !html.includes('当前没有待审核的自动调优提案。'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["hasItemsLoading"])
        self.assertTrue(payload["hasAlertsLoading"])
        self.assertTrue(payload["hasTuningLoading"])
        self.assertTrue(payload["avoidsFalseEmptyItems"])
        self.assertTrue(payload["avoidsFalseEmptyAlerts"])
        self.assertTrue(payload["avoidsFalseEmptyTuning"])

    def test_ai_analysis_view_shows_loading_state_for_deferred_history_panels(self) -> None:
        script = """
import { renderAIAnalysisSectionCards } from './aats/api/static/modules/views/ai-view.js';

const sections = renderAIAnalysisSectionCards({
  aiOverview: { shadow_summary: {}, performance_view: {} },
  aiLatest: {},
  uiHints: {
    pendingPanels: {
      aiRecent: true,
      aiShadowRecent: true,
      aiShadowEvaluations: true,
    },
  },
});
const html = sections.aiHistory;
console.log(JSON.stringify({
  hasLoading: html.includes('AI 历史正在加载'),
  avoidsFalseEmpty: !html.includes('当前暂无可复盘的 AI 历史记录'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["hasLoading"])
        self.assertTrue(payload["avoidsFalseEmpty"])

    def test_risk_view_shows_deferred_loading_notice_before_replay_and_exit_history_arrive(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  systemRecovery: {
    recovery: {
      safe_to_trade: true,
      review_required: false,
      resume_eligible: true,
      halted: false,
      rebaseline_available: false,
      resume_blocked_reasons: [],
      exit_execution_action_history: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-clean',
      severity: 'CLEAN',
      halt_required: false,
      review_required: false,
    },
    mismatch_summary: {},
    exchange_bills_summary: {},
  },
  runtime: {
    trial_guard: { status: 'monitoring', summary: 'ok', fill_count: 6, min_closed_fills: 5, breaches: [] },
    guarded_live_preflight: { status: 'ready', summary: 'ok', counts: { pass: 1, warn: 0, fail: 0 }, operator_actions: [], checks: [] },
    guarded_live_run_packet_summary: { status: 'ready', summary: 'ok', summary_metrics: {}, operator_actions: [] },
  },
  metrics: { phase1_shadow: { status: 'healthy', summary: 'ok', lag: {}, execution_shadow: {}, ledger_shadow: {} } },
  health: { runtime_state: 'healthy' },
  accountState: { fresh: true, ready: true, blockers: [], margin_buffer_overview: {}, position_mode_contract: {}, derivatives_live_guard: { current_derivatives_exposure: {} } },
  positions: { local_instrument_positions: [] },
  portfolio: { portfolio: { total_equity: 100, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  uiHints: {
    recoveryReasonsText: '',
    controlPermissionMessage: '',
    pendingPanels: {
      replayStatus: true,
      exitExecutionActionHistoryPage: true,
    },
  },
});

console.log(JSON.stringify({
  showsReplayPending: html.includes('回放状态正在补充'),
  showsExitHistoryPending: html.includes('退出任务长历史正在补充'),
  showsDeferredReason: html.includes('完整 parent-exit 历史会在首屏后自动补载'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"showsReplayPending":true', stdout)
        self.assertIn('"showsExitHistoryPending":true', stdout)
        self.assertIn('"showsDeferredReason":true', stdout)

    def test_strategy_view_dashboard_bundle_trims_removed_runtime_details(self) -> None:
        payload = {
            "generated_at": "2026-04-02T12:00:00Z",
            "summary": {
                "automatic_selection_enabled": True,
                "latest_selected_family": "independent",
                "latest_bundle_status": "submitted",
            },
            "family_enablement": {
                "independent": {
                    "enabled": True,
                    "runtime_supported": True,
                    "execution_compatible": True,
                }
            },
            "configured_parameters": {
                "strategy_family_active": "directional",
                "strategy_family_auto_selection_enabled": True,
                "strategy_sleeve_auto_execution_enabled": True,
                "strategy_sleeve_auto_execution_config_source": "strategy_sleeve_auto_execution_enabled",
                "strategy_sleeve_auto_execution_uses_deprecated_key": False,
                "compatibility": {
                    "deprecated_auto_execution_key": "strategy_sleeve_auto_parallel_enabled",
                    "deprecated_auto_execution_value": None,
                },
                "strategy_sleeve_auto_min_budget_multiplier": 0.4,
                "strategy_sleeve_auto_reconciliation_contraction_multiplier": 0.7,
                "strategy_sleeve_auto_soft_loss_usdt": 25.0,
                "strategy_sleeve_auto_hard_loss_usdt": 50.0,
                "strategy_sleeve_auto_volatility_cap_enabled": True,
                "env_template_profile": "derivatives.live",
                "trade_costs": {"spot_taker_fee_bps": 10},
                "directional": {"short_bias_enabled": True},
                "smart_arbitrage": {"enabled": True},
            },
            "latest_snapshot": {
                "automation_decisions": [{"strategy_sleeve_id": "independent_long_book"}],
                "candidates": [{"family": "independent"}],
                "selected_family": "independent",
            },
            "latest_bundle": {"bundle_id": "bundle_abc123", "status": "submitted"},
            "latest_applied_target": {"target_position_qty": 0.03},
            "recent_budget_snapshots": [{"strategy_sleeve_id": "independent_long_book"}],
            "recent_conflict_resolutions": [{"conflict_type": "symbol"}],
            "recent_netting_decisions": [{"symbol": "BTC-USDT-SWAP"}],
            "recent_sleeve_intents": [{"strategy_sleeve_id": "independent_long_book"}],
            "truth_source": "strategy_runtime_repo_plus_event_store",
        }

        trimmed = _strategy_view_strategy_runtime_payload(payload)

        self.assertEqual(trimmed["generated_at"], payload["generated_at"])
        self.assertEqual(trimmed["summary"], payload["summary"])
        self.assertEqual(trimmed["family_enablement"], payload["family_enablement"])
        self.assertEqual(trimmed["latest_bundle"], payload["latest_bundle"])
        self.assertEqual(trimmed["latest_applied_target"], payload["latest_applied_target"])
        self.assertEqual(trimmed["truth_source"], payload["truth_source"])
        self.assertEqual(
            trimmed["latest_snapshot"],
            {"automation_decisions": payload["latest_snapshot"]["automation_decisions"]},
        )
        self.assertNotIn("candidates", trimmed["latest_snapshot"])
        self.assertNotIn("selected_family", trimmed["latest_snapshot"])
        self.assertNotIn("trade_costs", trimmed["configured_parameters"])
        self.assertNotIn("directional", trimmed["configured_parameters"])
        self.assertNotIn("smart_arbitrage", trimmed["configured_parameters"])
        self.assertEqual(
            trimmed["configured_parameters"]["strategy_sleeve_auto_execution_config_source"],
            "strategy_sleeve_auto_execution_enabled",
        )
        self.assertFalse(trimmed["configured_parameters"]["strategy_sleeve_auto_execution_uses_deprecated_key"])
        self.assertNotIn("strategy_sleeve_auto_parallel_enabled", trimmed["configured_parameters"])
        self.assertEqual(
            trimmed["configured_parameters"]["compatibility"]["deprecated_auto_execution_key"],
            "strategy_sleeve_auto_parallel_enabled",
        )
        self.assertIsNone(trimmed["configured_parameters"]["compatibility"]["deprecated_auto_execution_value"])
        self.assertNotIn("recent_budget_snapshots", trimmed)
        self.assertNotIn("recent_conflict_resolutions", trimmed)
        self.assertNotIn("recent_netting_decisions", trimmed)
        self.assertNotIn("recent_sleeve_intents", trimmed)

    def test_strategy_view_explains_automatic_enabled_as_execution_chain_eligibility(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    summary: {
      entry_auto_execution_enabled: true,
      budget_zero_suppression_count: 0,
      execution_control_mode_counts: {
        approved: 1,
        permission_denied: 0,
        budget_zero_suppressed: 0,
        protective_override: 0,
      },
      execution_behavior_counts: {
        execute_target: 1,
        hold_current: 0,
        advisory_only: 0,
        suppressed_after_approval: 0,
        protective_execute: 0,
      },
      execution_control_summary: {
        active: true,
        primary_mode: 'approved',
        headline: '最近自动执行主路径正常放行',
        summary: '最近样本以允许自动进入执行链为主。',
        total_recent_intents: 1,
      },
      execution_behavior_summary: {
        active: true,
        primary_behavior: 'execute_target',
        headline: '最近执行行为以继续沿目标执行为主',
        summary: '最近样本会继续沿目标仓位进入执行链。',
        total_recent_intents: 1,
      },
      latest_approved_sleeve_weights: {},
      entry_execution_guard: {
        active: false,
      },
    },
    latest_snapshot: {
      candidates: [],
      automation_decisions: [],
    },
    configured_parameters: {
      strategy_sleeve_auto_execution_enabled: true,
      strategy_sleeve_auto_min_budget_multiplier: 0.25,
      strategy_sleeve_auto_soft_loss_usdt: 20,
      strategy_sleeve_auto_hard_loss_usdt: 50,
    },
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {},
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  showsEligibilityCopy: html.includes('当前自动入链资格')
    && html.includes('允许自动进入执行链')
    && html.includes('当前这类 sleeve 满足自动进入执行链的前置条件，后续仍会继续经过预算控制和执行约束。'),
  hidesLegacySwitchCopy: !html.includes('自动执行主开关')
    && !html.includes('当前按系统规则自动启停和分配预算。'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"showsEligibilityCopy":true', result.stdout)
        self.assertIn('"hidesLegacySwitchCopy":true', result.stdout)

    def test_ai_analysis_bundle_path_includes_recent_panels_and_ai_limits(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDashboardBundlePath } from './aats/api/static/modules/store.js';

const path = buildDashboardBundlePath('aiAnalysis', {
  pageLimits: {
    recentAIAssessments: 11,
    recentAIShadowDecisions: 13,
    recentAIShadowEvaluations: 15,
  },
});
const url = new URL(path, 'http://localhost');
console.log(JSON.stringify({
  recentAIAssessments: url.searchParams.get('recentAIAssessments'),
  recentAIShadowDecisions: url.searchParams.get('recentAIShadowDecisions'),
  recentAIShadowEvaluations: url.searchParams.get('recentAIShadowEvaluations'),
  panels: url.searchParams.getAll('panel'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["recentAIAssessments"], "11")
        self.assertEqual(payload["recentAIShadowDecisions"], "13")
        self.assertEqual(payload["recentAIShadowEvaluations"], "15")
        self.assertEqual(
            payload["panels"],
            [
                "session",
                "authProviders",
                "health",
                "mode",
                "runtime",
                "systemRecovery",
                "blockerControl",
                "aiRuntime",
                "aiOverview",
                "aiLatest",
                "aiShadowLatest",
                "profileControlSummary",
                "aiRecent",
                "aiShadowRecent",
                "aiShadowEvaluations",
            ],
        )

    def test_ai_views_render_in_node_smoke_test(self) -> None:
        script = """
import { renderAIAnalysisView } from './aats/api/static/modules/views/ai-analysis-view.js';
import { renderAIConfigView } from './aats/api/static/modules/views/ai-config-view.js';
import { renderRdpView } from './aats/api/static/modules/views/rdp-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const analysisHtml = renderAIAnalysisView({
  session: { role: 'admin' },
  blockerControl: {},
  aiOverview: {
    runtime: {
      configured_operating_mode: 'ai_decision_maker',
      effective_operating_mode: 'ai_decision_maker',
      provider_ready: true,
      shadow_mode_enabled: true,
      manual_override_active: false,
      manual_override_default_freeze_seconds: 3600,
      execution_suggestion_mode: 'shadow_translation',
    },
    latest_decision_outcome: {
      decision_source: 'baseline_fallback',
      final_action: 'hold',
      final_target_qty: 0,
      decision_authority: 'final_decision',
    },
    shadow_summary: { window_count: 3, outperformed_rate: 0.66, status: 'healthy' },
    downgrade_state: { provider_state: 'healthy', outcome_state: 'healthy' },
    latest_execution_suggestion: {
      configured_mode: 'shadow_translation',
      status: 'healthy',
      translation_present: true,
      latest_translation: {
        translation_preview: { execution_style: 'maker_bias', order_type: 'limit', time_in_force: 'IOC', limit_offset_bps: 3 },
        rejection_reasons: [],
        clipped_fields: [],
        notes: [],
      },
    },
    performance_view: {
      report_count: 1,
      status_counts: { healthy: 1 },
      replay_context: { healthy_rate: 1, validation_count: 2, latest_validation: { validated_at: '2026-03-21T12:00:00Z', divergence_count: 0 } },
      recent_reports: [],
    },
  },
  aiLatest: {},
  aiRecent: { assessments: [] },
  aiShadowRecent: { shadow_decisions: [] },
  aiShadowEvaluations: { evaluations: [] },
  profileControlSummary: {
    control_summary: {
      safety_profile_required: false,
      evidence: { cold_start_active: true, closed_trades: 1, min_closed_trades: 6, replay_validations: 0, min_replay_validations: 5 },
      adaptive_controls: {
        risk_budget: { multiplier: 0.7, status: 'contracted', reasons: ['execution_errors_elevated'] },
        execution_aggressiveness: { multiplier: 0.55, status: 'safe_mode', reasons: ['execution_errors_elevated', 'trial_guard_breached'] },
      },
    },
    activation: { active_profile_id: 'trend_normal' },
    active_revision: { profile_id: 'trend_normal', profile_label: '趋势标准' },
    latest_selection_decision: {
      blocked_reasons: ['strategy_profile_cold_start_lock_active'],
      candidate_profile_id: 'trend_strict',
      transition_class: 'conservative_rebalance',
      operator_summary: '当前候选档位 trend_strict 属于更保守切换，系统准备在门槛满足后自动收缩。',
      fast_track_eligible: true,
      fast_track_applied: false,
      gating_state: {
        confidence_floor: 0.75,
        remaining_closed_trades: 5,
        remaining_replay_validations: 5,
        remaining_consecutive_wins: 2,
        fast_track_reasons: ['execution_errors_elevated'],
        fast_track_bypass_gates: ['strategy_profile_cold_start_lock_active'],
        reconciliation_clean: true,
      },
      selection_reason_summary: 'raw backend summary should not appear',
    },
    latest_optimization_report: { recommended_profile_id: 'trend_strict', score_delta_vs_active: 1.2, notes: ['replay_history_neutralized'] },
  },
});

const baseWorkbenchOverview = {
  round_id: '20260321_121500_demo',
  overall_status: 'needs_approval',
  headline: '当前有 1 个组合待处理。',
  subheadline: '先看结论，再决定是否批准。',
  primary_action: {
    key: 'trigger_data_maintenance',
    label: '刷新数据',
    ui_action: 'rdp-trigger-workflow',
    value: 'data_maintenance',
    enabled: true,
  },
  secondary_actions: [
    {
      key: 'trigger_research_cycle',
      label: '运行完整 RDP',
      ui_action: 'rdp-trigger-workflow',
      value: 'research_cycle',
      enabled: true,
    },
  ],
  blockers: [
    {
      code: 'phase3_incomplete',
      severity: 'danger',
      title: '最新归因结果不完整',
      message: 'Phase3 当前不可用于正式结论。',
      blocks_approval: true,
    },
  ],
  summary_counts: {
    pending_items: 1,
    ready_release_items: 1,
    integrity_blocked_items: 1,
    observing_releases: 1,
    tuning_pending: 1,
  },
  current_execution: {
    workflow: 'research_cycle',
    status: 'running',
    started_at: '2026-03-21T12:15:00Z',
  },
  next_queue: {
    workflow: 'governance_cycle',
    status: 'pending',
    requested_at: '2026-03-21T12:16:00Z',
  },
  health: {
    daemon: 'healthy',
    governance_db: 'blocked',
    latest_gate: 'warn',
  },
};

const baseWorkbenchItems = {
  total: 1,
  items: [
    {
      combo_key: 'independent_15m',
      family: 'Independent',
      timeframe: '15M',
      recommendation_id: 'rec-1',
      recommendation_type: 'parameter_upgrade',
      confidence: 'high',
      headline: '参数候选待审批',
      decision_summary: '这轮产出了一组新参数，确认后就会进入待发布。',
      approval_effect_summary: '批准后进入待发布，下一步是运行 Gate 或创建发布。',
      reason_summary: [
        '归因失败率过高',
        '执行边际没有改善',
        '当前组合仍处于治理暂停状态',
      ],
      missing_evidence: ['最新归因结果不完整'],
      blocking_flags: ['governance degraded'],
      integrity_status: 'blocked',
      approval_enabled: false,
      approval_blocked_reason: '证据不完整，当前不能审批。',
      created_at: '2026-03-21T12:20:00Z',
      source_rounds: {
        phase2_round_id: 'round_step2_1',
        phase3_round_id: 'round_phase3_1',
        phase4_round_id: 'round_phase4_1',
      },
      detail_summary: {
        risk_summary: ['归因失败率过高', 'governance degraded'],
      },
      evidence_digest: [
        {
          phase: 'phase2',
          status: 'blocked',
          headline: 'Step2 研究证据当前不可直接审批',
          metrics: {
            openings_with_signal: 39,
            mean_positive_edge_ratio: 0.528,
          },
          round_id: 'round_step2_1',
          incomplete_reason: 'phase3_incomplete',
        },
        {
          phase: 'phase3',
          status: 'incomplete',
          headline: '最新归因结果不完整',
          metrics: {
            status: 'unknown',
          },
          round_id: 'round_phase3_1',
          incomplete_reason: 'manifest_missing_on_disk',
        },
      ],
      actions: [
        {
          key: 'approve',
          label: '批准参数候选',
          ui_action: 'rdp-approve-only',
          value: 'rec-1',
          enabled: false,
          disabled_reason: '证据不完整，当前不能审批',
        },
        {
          key: 'reject',
          label: '驳回候选',
          ui_action: 'rdp-reject-recommendation',
          value: 'rec-1',
          enabled: true,
        },
      ],
    },
  ],
  release_candidates: {
    total: 1,
    items: [
      {
        combo_key: 'independent_15m',
        family: 'Independent',
        timeframe: '15M',
        recommendation_id: 'rec-2',
        confidence: 'medium',
        headline: '已批准，待发布',
        decision_summary: '这组参数已经批准，下一步可以运行 Gate 或直接创建发布。',
        gate_status: 'warn',
        gate_note: '最近一次 Gate 有警告，请先复核。',
        reason_summary: ['这组参数已经批准，下一步可以运行 Gate 或直接创建发布。'],
        created_at: '2026-03-21T12:10:00Z',
        actions: [
          {
            key: 'run_gate',
            label: '运行 Gate',
            ui_action: 'rdp-run-gate',
            value: 'rec-2',
            enabled: true,
          },
          {
            key: 'create_release',
            label: '创建发布',
            ui_action: 'rdp-create-release',
            value: 'rec-2',
            enabled: true,
          },
        ],
      },
    ],
  },
};

const baseWorkbenchAlerts = {
  integrity_alerts: [
    {
      code: 'phase3_incomplete',
      severity: 'danger',
      phase: 'phase3',
      title: '最新归因结果不完整',
      message: 'Phase3 当前不可用于正式结论。',
      blocks_approval: true,
    },
  ],
  operational_alerts: [
    {
      code: 'governance_db',
      severity: 'danger',
      title: '治理数据库还没有接通，发布链路现在不可用。',
      message: 'governance_db_connection_failed: connection refused',
    },
  ],
};

const baseTuningOverview = {
  pending_review_count: 1,
  approved_not_applied_count: 0,
  active_override_count: 2,
  blocked_review_count: 0,
  headline: '自动调优已生成 1 条待审核提案。',
};

const baseTuningProposals = {
  total: 1,
  items: [
    {
      proposal_id: 'tp-1',
      family: 'Independent',
      timeframe: '15M',
      headline: '建议上调 min_safe_net_edge_bps',
      proposed_changes: [
        { key: 'min_safe_net_edge_bps', from: 2, to: 4 },
      ],
      reason_summary: ['最近四轮 execution 正向但安全边界偏松'],
      created_at: '2026-03-21T12:19:00Z',
      actions: [
        {
          key: 'approve',
          label: '批准调优',
          ui_action: 'rdp-approve-tuning-proposal',
          value: 'tp-1',
          enabled: true,
        },
        {
          key: 'reject',
          label: '拒绝调优',
          ui_action: 'rdp-reject-tuning-proposal',
          value: 'tp-1',
          enabled: true,
        },
      ],
    },
  ],
};

const configHtml = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'ai_decision_maker',
    effective_operating_mode: 'ai_decision_maker',
    manual_override_active: false,
    manual_override_default_freeze_seconds: 3600,
    shadow_mode_enabled: true,
    execution_suggestion_mode: 'shadow_translation',
    strategy_profile_auto_control_effective: false,
  },
  summary: {
    ai: {
      effective_operating_mode: 'ai_decision_maker',
      configured_operating_mode: 'ai_decision_maker',
      shadow_mode_enabled: true,
      execution_suggestion_mode: 'shadow_translation',
      shadow_summary: { window_count: 3, outperformed_rate: 0.66 },
      latest_profile_control_decision: {
        applied: false,
        blocked_reasons: ['strategy_profile_auto_switch_frozen'],
        frozen_by_admin_override: true,
        freeze_until: '2026-03-21T12:30:00Z',
      },
    },
    runtime_profile: {
      profile_source: 'env_only',
      current_runtime_payload: {
        default_symbol: 'BTC-USDT-SWAP',
        allowed_symbols: ['BTC-USDT-SWAP'],
        trading_product_type: 'derivatives',
        margin_mode: 'cross',
        default_order_qty: 0.01,
        max_notional_per_symbol: 8000,
      },
    },
    strategy_profile: {
      activation: { active_profile_id: 'trend_normal' },
      active_revision: { profile_id: 'trend_normal', profile_label: '趋势标准' },
      latest_selection_decision: {
        blocked_reasons: ['strategy_profile_auto_switch_frozen'],
        transition_class: 'same_risk_optimization',
        operator_summary: '当前候选档位 trend_strict 已产生，但仍有阻断条件未解除。',
        fast_track_eligible: false,
        fast_track_applied: false,
        gating_state: {
          confidence_floor: 0.8,
          remaining_closed_trades: 2,
          remaining_replay_validations: 1,
          remaining_consecutive_wins: 1,
          reconciliation_clean: true,
        },
      },
      latest_optimization_report: {
        recommended_profile_id: 'trend_strict',
        score_delta_vs_active: 0.6,
        notes: ['replay_history_neutralized'],
        control_summary: {
          adaptive_controls: {
            risk_budget: { multiplier: 0.85, status: 'contracted', reasons: ['current_margin_usage_elevated'] },
            execution_aggressiveness: { multiplier: 0.7, status: 'contracted', reasons: ['execution_errors_elevated'] },
          },
        },
      },
      activation_history: [],
    },
  },
  rdpControl: {
    environment: {
      name: 'staging',
      strict_environment: true,
      description: '预发布环境',
      require_gate_pass: true,
      require_approval: false,
      allow_parameter_rollback: true,
      direct_apply_allowed: true,
      required_observation_window_hours: 24,
    },
    health: {
      overall_health: 'blocked',
      blocking_reasons: ['critical_reliability_alerts'],
      warnings: ['workflow_runs_incomplete'],
      checks: [
        {
          category: 'governance_db',
          name: 'connection',
          status: 'blocked',
          detail: 'governance_db_connection_failed: connection refused',
        },
        { category: 'runtime', name: 'rdp-daemon', status: 'ok', detail: 'state=running, age_seconds=12' },
        { category: 'alerts', name: 'current_alerts', status: 'blocked', detail: 'overall=critical, critical=1, warning=0' },
        { category: 'workflow_runs', name: 'freshness', status: 'warn', detail: 'decision_cycle=stale:200.0h' },
        {
          category: 'live_db',
          name: 'readonly_access',
          status: 'warn',
          detail: 'RDP_LIVE_DATABASE_URL 未配置',
        },
      ],
    },
    operations_summary: {
      approved_release_candidate_count: 1,
      draft_recommendation_count: 2,
      draft_parameter_recommendation_count: 1,
      observing_release_count: 1,
      rollback_recommended_count: 0,
      latest_gate_status: 'warn',
      latest_release_apply_result: 'success',
      health_status: 'blocked',
      health_blocked: true,
    },
    tasks: {
      data_maintenance: {
        latest_task: { status: 'pending', requested_at: '2026-03-21T12:16:00Z' },
        pending_task: { status: 'pending', requested_at: '2026-03-21T12:16:00Z' },
        running_task: null,
        display_task: { status: 'pending', requested_at: '2026-03-21T12:16:00Z' },
      },
      research_cycle: {
        latest_task: { status: 'running', started_at: '2026-03-21T12:15:00Z' },
        pending_task: null,
        running_task: { status: 'running', started_at: '2026-03-21T12:15:00Z' },
        display_task: { status: 'running', started_at: '2026-03-21T12:15:00Z' },
      },
    },
    pending_recommendations: [
      {
        recommendation_id: 'rec-1',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'high',
        reason: '净边际不足，需要保守降门槛',
        status: 'draft',
        target_parameter_set_id: 'ps_candidate_1',
        created_at: '2026-03-21T12:20:00Z',
      },
      {
        recommendation_id: 'rec-1-pause',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'pause',
        confidence: 'high',
        reason: '存在严重负面信号；归因 failure 比例 100% 超过 85%',
        status: 'draft',
        created_at: '2026-03-21T12:21:00Z',
      },
      {
        recommendation_id: 'rec-2',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '已完成审批，准备创建 release',
        status: 'approved',
        target_parameter_set_id: 'ps_candidate_2',
      },
    ],
    recommendation_history: [
      {
        recommendation_id: 'rec-1-pause',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'pause',
        confidence: 'high',
        reason: '存在严重负面信号；归因 failure 比例 100% 超过 85%',
        status: 'draft',
        created_at: '2026-03-21T12:21:00Z',
      },
      {
        recommendation_id: 'rec-1',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'high',
        reason: '候选参数已经生成，可继续人工审批。',
        status: 'draft',
        target_parameter_set_id: 'ps_candidate_1',
        created_at: '2026-03-21T12:20:00Z',
      },
      {
        recommendation_id: 'rec-hist-1',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '历史候选 1',
        status: 'superseded',
        target_parameter_set_id: 'ps_hist_1',
        created_at: '2026-03-21T11:50:00Z',
      },
      {
        recommendation_id: 'rec-hist-2',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '历史候选 2',
        status: 'superseded',
        target_parameter_set_id: 'ps_hist_2',
        created_at: '2026-03-21T11:40:00Z',
      },
      {
        recommendation_id: 'rec-hist-3',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '历史候选 3',
        status: 'superseded',
        target_parameter_set_id: 'ps_hist_3',
        created_at: '2026-03-21T11:30:00Z',
      },
      {
        recommendation_id: 'rec-hist-4',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '历史候选 4',
        status: 'superseded',
        target_parameter_set_id: 'ps_hist_4',
        created_at: '2026-03-21T11:20:00Z',
      },
      {
        recommendation_id: 'rec-hist-5',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '历史候选 5',
        status: 'superseded',
        target_parameter_set_id: 'ps_hist_5',
        created_at: '2026-03-21T11:10:00Z',
      },
    ],
    active_parameters: {},
    runtime_parameter_source: {
      mode: 'governance_pause',
      active_count: 0,
      governance_managed: true,
      paused_combos: ['independent_15m'],
      combo_count: 4,
    },
    latest_round_summary: {
      available: true,
      round_id: '20260321_121500_demo',
      candidate_count: 4,
      conclusion_count: 4,
      has_conclusion_report: true,
      readiness_status: 'blocked',
    },
    latest_research_conclusions: [
      {
        family: 'independent',
        timeframe: '15m',
        decision: 'pause',
        confidence: 'high',
        reasons: ['净边际低于安全下限', 'failure ratio 过高'],
      },
    ],
    governance_state: {
      available: true,
      governance_managed: true,
      paused_combos: ['independent_15m'],
      parameter_source_mode: 'governance_pause',
      status_distribution: { pause: 4 },
      combo_states: [
        {
          combo_key: 'independent_15m',
          family: 'independent',
          timeframe: '15m',
          runtime_source: 'governance_pause',
          decision_status: 'pause',
          latest_round_decision: 'pause',
          candidate_parameter_set_id: 'ps_demo_1234567890',
          candidate_parameter_status: 'candidate',
          runtime_active_parameter_set_id: null,
          pending_operator_action: true,
          latest_recommendation_type: 'parameter_upgrade',
          latest_recommendation_status: 'draft',
          inconsistencies: [],
        },
      ],
    },
    recent_gate_results: [
      {
        gate_run_id: 'gate-1',
        recommendation_id: 'rec-2',
        created_at: '2026-03-21T12:18:00Z',
        gate_status: 'warn',
        allow_apply: true,
        blocking_reasons: [],
        warnings: ['workflow stale'],
        checks: [],
      },
    ],
    recent_releases: [
      {
        release_id: 'rel-1',
        created_at: '2026-03-21T12:00:00Z',
        family: 'independent',
        timeframe: '15m',
        combo_key: 'independent_15m',
        recommendation_id: 'rec-older',
        parameter_set_id: 'ps_live_1',
        previous_parameter_set_id: 'ps_base_1',
        actor: 'operator',
        gate_status: 'pass',
        apply_result: 'success',
        observation_status: 'observing',
        observation_window_hours: 24,
      },
    ],
    observation_queue: [
      {
        release_id: 'rel-1',
        family: 'independent',
        timeframe: '15m',
        combo_key: 'independent_15m',
        recommendation_id: 'rec-older',
        parameter_set_id: 'ps_live_1',
        previous_parameter_set_id: 'ps_base_1',
        created_at: '2026-03-21T12:00:00Z',
        gate_status: 'pass',
        apply_result: 'success',
        observation_status: 'observing',
        observation_window_hours: 24,
        observation: {
          status: 'observing',
          recommendation: 'keep',
          evaluated_at: '2026-03-21T12:20:00Z',
        },
        effectiveness: {
          conclusion: 'mixed',
          detail: 'still observing',
        },
        current_active_parameter_set_id: 'ps_live_1',
        is_current_active_release: true,
      },
    ],
  },
  rdpWorkbenchOverview: baseWorkbenchOverview,
  rdpWorkbenchItems: baseWorkbenchItems,
  rdpWorkbenchAlerts: baseWorkbenchAlerts,
  rdpTuningOverview: baseTuningOverview,
  rdpTuningProposals: baseTuningProposals,
});

const configAndRdpHtml = configHtml + renderRdpView({
  session: { role: 'admin' },
  rdpControl: {
    environment: { name: 'staging' },
    observation_queue: [
      {
        release_id: 'rel-1',
        family: 'independent',
        timeframe: '15m',
        combo_key: 'independent_15m',
        recommendation_id: 'rec-older',
        parameter_set_id: 'ps_live_1',
        previous_parameter_set_id: 'ps_base_1',
        created_at: '2026-03-21T12:00:00Z',
        gate_status: 'pass',
        apply_result: 'success',
        observation_status: 'observing',
        observation_window_hours: 24,
        observation: { status: 'observing', recommendation: 'keep' },
        effectiveness: { conclusion: 'mixed' },
        current_active_parameter_set_id: 'ps_live_1',
        is_current_active_release: true,
      },
    ],
  },
  rdpWorkbenchOverview: baseWorkbenchOverview,
  rdpWorkbenchItems: baseWorkbenchItems,
  rdpWorkbenchAlerts: baseWorkbenchAlerts,
  rdpTuningOverview: baseTuningOverview,
  rdpTuningProposals: baseTuningProposals,
  uiState: { rdp: {} },
});

const manualOnlyConfigHtml = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'baseline_only',
    effective_operating_mode: 'baseline_only',
    manual_override_active: false,
    ui_operating_mode_override: {
      enabled: false,
      disabled_reason: 'ui_operating_mode_override_disabled_by_governance_policy',
    },
    strategy_profile_auto_control_configured: false,
    strategy_profile_auto_control_effective: false,
    strategy_profile_auto_control_reason: 'explicit_setting_disabled',
  },
  summary: {
    ai: {
      configured_operating_mode: 'baseline_only',
      effective_operating_mode: 'baseline_only',
      strategy_profile_auto_control_configured: false,
      strategy_profile_auto_control_effective: false,
      strategy_profile_auto_control_reason: 'explicit_setting_disabled',
    },
    runtime_profile: {
      current_runtime_payload: {},
    },
    strategy_profile: {
      activation: { active_profile_id: 'trend_normal' },
      active_revision: { profile_id: 'trend_normal' },
      latest_selection_decision: {},
      latest_optimization_report: {},
    },
  },
  uiState: {
    modeManualEditing: false,
    profileManualEditing: false,
    aiConfig: {},
  },
});

const releaseEmptyConfigHtml = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'baseline_only',
    effective_operating_mode: 'baseline_only',
    manual_override_active: false,
    strategy_profile_auto_control_configured: false,
    strategy_profile_auto_control_effective: false,
    strategy_profile_auto_control_reason: 'explicit_setting_disabled',
  },
  summary: {
    ai: {
      configured_operating_mode: 'baseline_only',
      effective_operating_mode: 'baseline_only',
      strategy_profile_auto_control_configured: false,
      strategy_profile_auto_control_effective: false,
      strategy_profile_auto_control_reason: 'explicit_setting_disabled',
    },
    runtime_profile: { current_runtime_payload: {} },
    strategy_profile: {
      activation: { active_profile_id: 'trend_normal' },
      active_revision: { profile_id: 'trend_normal' },
      latest_selection_decision: {},
      latest_optimization_report: {},
    },
  },
  rdpControl: {
    environment: {
      name: 'dev',
      strict_environment: false,
      require_gate_pass: false,
      required_observation_window_hours: 24,
    },
    health: {
      overall_health: 'healthy',
      blocking_reasons: [],
      warnings: [],
      checks: [],
    },
    operations_summary: {
      approved_release_candidate_count: 0,
      draft_recommendation_count: 1,
      draft_parameter_recommendation_count: 1,
      observing_release_count: 0,
      rollback_recommended_count: 0,
      latest_gate_status: null,
      latest_release_apply_result: null,
      health_status: 'healthy',
      health_blocked: false,
    },
    tasks: {},
    pending_recommendations: [
      {
        recommendation_id: 'rec-empty-1',
        symbol: 'BTC-USDT-SWAP',
        family: 'directional',
        timeframe: '1h',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '等待审批',
        status: 'draft',
        target_parameter_set_id: 'ps_empty_1',
        created_at: '2026-03-21T12:20:00Z',
      },
    ],
    recommendation_history: [
      {
        recommendation_id: 'rec-empty-1',
        symbol: 'BTC-USDT-SWAP',
        family: 'directional',
        timeframe: '1h',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        reason: '等待审批',
        status: 'draft',
        target_parameter_set_id: 'ps_empty_1',
        created_at: '2026-03-21T12:20:00Z',
      },
    ],
    active_parameters: {},
    governance_state: {
      available: true,
      governance_managed: true,
      paused_combos: [],
      parameter_source_mode: 'governance_managed',
      status_distribution: {},
      combo_states: [
        {
          combo_key: 'directional_1h',
          family: 'directional',
          timeframe: '1h',
          candidate_parameter_set_id: 'ps_empty_1',
          candidate_parameter_status: 'candidate',
          latest_recommendation_type: 'parameter_upgrade',
          latest_recommendation_status: 'draft',
          inconsistencies: [],
        },
      ],
    },
    recent_gate_results: [],
    recent_releases: [],
    observation_queue: [],
  },
  rdpWorkbenchOverview: {
    round_id: '20260321_122000_empty',
    overall_status: 'needs_approval',
    headline: '当前有 1 个组合待处理。',
    subheadline: '先看结论，再决定是否批准。',
    primary_action: {
      key: 'trigger_data_maintenance',
      label: '刷新数据',
      ui_action: 'rdp-trigger-workflow',
      value: 'data_maintenance',
      enabled: true,
    },
    secondary_actions: [
      {
        key: 'trigger_research_cycle',
        label: '运行完整 RDP',
        ui_action: 'rdp-trigger-workflow',
        value: 'research_cycle',
        enabled: true,
      },
    ],
    blockers: [],
    summary_counts: {
      pending_items: 1,
      ready_release_items: 0,
      integrity_blocked_items: 0,
      observing_releases: 0,
      tuning_pending: 0,
    },
    current_execution: { workflow: null, status: 'idle' },
    next_queue: { workflow: null, status: 'none' },
    health: {
      daemon: 'healthy',
      governance_db: 'healthy',
      latest_gate: 'not_run',
    },
  },
  rdpWorkbenchItems: {
    total: 1,
    items: [
      {
        combo_key: 'directional_1h',
        family: 'Directional',
        timeframe: '1H',
        recommendation_id: 'rec-empty-1',
        recommendation_type: 'parameter_upgrade',
        confidence: 'medium',
        headline: '参数候选待审批',
        decision_summary: '这轮产出了一组新参数，确认后就会进入待发布。',
        approval_effect_summary: '批准后进入待发布，下一步是运行 Gate 或创建发布。',
        reason_summary: ['当前轮次只有 1 个 directional 组合需要审批'],
        missing_evidence: [],
        blocking_flags: [],
        integrity_status: 'complete',
        created_at: '2026-03-21T12:20:00Z',
        actions: [
          {
            key: 'approve',
            label: '批准参数候选',
            ui_action: 'rdp-approve-only',
            value: 'rec-empty-1',
            enabled: true,
          },
          {
            key: 'reject',
            label: '驳回候选',
            ui_action: 'rdp-reject-recommendation',
            value: 'rec-empty-1',
            enabled: true,
          },
        ],
      },
    ],
    release_candidates: {
      total: 0,
      items: [],
    },
  },
  rdpWorkbenchAlerts: {
    integrity_alerts: [],
    operational_alerts: [],
  },
  rdpTuningOverview: {
    pending_review_count: 0,
    approved_not_applied_count: 0,
    active_override_count: 0,
    blocked_review_count: 0,
    headline: '当前没有新的自动调优提案。',
  },
  rdpTuningProposals: {
    total: 0,
    items: [],
  },
  uiState: {
    modeManualEditing: false,
    profileManualEditing: false,
    aiConfig: {},
  },
});

const releaseEmptyCombinedHtml = releaseEmptyConfigHtml + renderRdpView({
  session: { role: 'admin' },
  rdpControl: {
    environment: { name: 'dev' },
    observation_queue: [],
  },
  rdpWorkbenchOverview: {
    overall_status: 'needs_approval',
    headline: '当前有 1 个组合待处理。',
    subheadline: '先看结论，再决定是否批准。',
    primary_action: {
      key: 'trigger_data_maintenance',
      label: '刷新数据',
      ui_action: 'rdp-trigger-workflow',
      value: 'data_maintenance',
      enabled: true,
    },
    secondary_actions: [],
    blockers: [],
    summary_counts: {
      pending_items: 1,
      ready_release_items: 0,
      integrity_blocked_items: 0,
      observing_releases: 0,
      tuning_pending: 0,
    },
    current_execution: { workflow: null, status: 'idle' },
    next_queue: { workflow: null, status: 'none' },
    health: {
      daemon: 'healthy',
      governance_db: 'healthy',
      latest_gate: 'not_run',
    },
  },
  rdpWorkbenchItems: {
    total: 1,
    items: [
      {
        combo_key: 'directional_1h',
        family: 'Directional',
        timeframe: '1H',
        headline: '参数候选待审批',
        decision_summary: '这轮产出了一组新参数，确认后就会进入待发布。',
        reason_summary: ['当前轮次只有 1 个 directional 组合需要审批'],
        integrity_status: 'complete',
        actions: [],
      },
    ],
    release_candidates: { total: 0, items: [] },
  },
  rdpWorkbenchAlerts: {
    integrity_alerts: [],
    operational_alerts: [],
  },
  rdpTuningOverview: {
    pending_review_count: 0,
    active_override_count: 0,
    headline: '当前没有新的自动调优提案。',
  },
  rdpTuningProposals: { total: 0, items: [] },
  uiState: { rdp: {} },
});

const rollbackConfigHtml = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'baseline_only',
    effective_operating_mode: 'baseline_only',
    manual_override_active: false,
    strategy_profile_auto_control_configured: false,
    strategy_profile_auto_control_effective: false,
    strategy_profile_auto_control_reason: 'explicit_setting_disabled',
  },
  summary: {
    ai: {
      configured_operating_mode: 'baseline_only',
      effective_operating_mode: 'baseline_only',
      strategy_profile_auto_control_configured: false,
      strategy_profile_auto_control_effective: false,
      strategy_profile_auto_control_reason: 'explicit_setting_disabled',
    },
    runtime_profile: { current_runtime_payload: {} },
    strategy_profile: {
      activation: { active_profile_id: 'trend_normal' },
      active_revision: { profile_id: 'trend_normal' },
      latest_selection_decision: {},
      latest_optimization_report: {},
    },
  },
  rdpControl: {
    environment: {
      name: 'prod',
      strict_environment: true,
      require_gate_pass: true,
      required_observation_window_hours: 72,
    },
    health: {
      overall_health: 'blocked',
      blocking_reasons: ['rollback_pending'],
      warnings: [],
      checks: [],
    },
    operations_summary: {
      approved_release_candidate_count: 1,
      draft_recommendation_count: 0,
      draft_parameter_recommendation_count: 0,
      observing_release_count: 1,
      rollback_recommended_count: 1,
      latest_gate_status: 'pass',
      latest_release_apply_result: 'success',
      health_status: 'blocked',
      health_blocked: true,
    },
    tasks: {},
    pending_recommendations: [
      {
        recommendation_id: 'rec-release-1',
        symbol: 'BTC-USDT-SWAP',
        family: 'independent',
        timeframe: '15m',
        recommendation_type: 'parameter_upgrade',
        confidence: 'high',
        reason: '这条建议已经批准，但当前不应先发布新版本。',
        status: 'approved',
        target_parameter_set_id: 'ps_release_1',
        created_at: '2026-03-21T12:22:00Z',
      },
    ],
    active_parameters: {},
    governance_state: {
      available: true,
      governance_managed: true,
      paused_combos: [],
      parameter_source_mode: 'governance_managed',
      status_distribution: {},
      combo_states: [],
    },
    recent_gate_results: [],
    recent_releases: [],
    observation_queue: [
      {
        release_id: 'rel-rollback-1',
        family: 'independent',
        timeframe: '15m',
        combo_key: 'independent_15m',
        recommendation_id: 'rec-old',
        parameter_set_id: 'ps_live_rollback',
        previous_parameter_set_id: 'ps_prev_rollback',
        created_at: '2026-03-21T10:00:00Z',
        gate_status: 'pass',
        apply_result: 'success',
        observation_status: 'rollback_recommended',
        observation_window_hours: 72,
        observation: {
          status: 'rollback_recommended',
          recommendation: 'rollback_recommended',
          evaluated_at: '2026-03-21T12:30:00Z',
        },
        effectiveness: {
          conclusion: 'negative',
          detail: '实盘表现显著回撤',
        },
        current_active_parameter_set_id: 'ps_live_rollback',
        is_current_active_release: true,
      },
    ],
  },
  rdpWorkbenchOverview: {
    round_id: '20260321_123000_rollback',
    overall_status: 'rollback_required',
    headline: '有发布进入回滚建议状态，先处理回滚。',
    subheadline: '回滚优先级高于当前轮次里的新建议与新发布。',
    primary_action: {
      key: 'rollback',
      label: '执行回滚',
      ui_action: 'rdp-rollback-parameters',
      value: 'independent/15m',
      enabled: true,
    },
    secondary_actions: [],
    blockers: [
      {
        code: 'rollback_pending',
        severity: 'danger',
        title: '当前有发布进入回滚建议状态',
        message: '应先回滚，再处理新的发布与审批。',
        blocks_approval: true,
      },
    ],
    summary_counts: {
      pending_items: 0,
      integrity_blocked_items: 0,
      observing_releases: 1,
      tuning_pending: 0,
    },
    current_execution: { workflow: null, status: 'idle' },
    next_queue: { workflow: null, status: 'none' },
    health: {
      daemon: 'healthy',
      governance_db: 'healthy',
      latest_gate: 'pass',
    },
  },
  rdpWorkbenchItems: {
    total: 0,
    items: [],
  },
  rdpWorkbenchAlerts: {
    integrity_alerts: [],
    operational_alerts: [],
  },
  rdpTuningOverview: {
    pending_review_count: 0,
    approved_not_applied_count: 0,
    active_override_count: 0,
    blocked_review_count: 0,
    headline: '当前没有新的自动调优提案。',
  },
  rdpTuningProposals: {
    total: 0,
    items: [],
  },
});

const rollbackCombinedHtml = rollbackConfigHtml + renderRdpView({
  session: { role: 'admin' },
  rdpControl: {
    environment: { name: 'prod' },
    observation_queue: [
      {
        release_id: 'rel-rollback-1',
        family: 'independent',
        timeframe: '15m',
        combo_key: 'independent_15m',
        recommendation_id: 'rec-old',
        parameter_set_id: 'ps_live_rollback',
        previous_parameter_set_id: 'ps_prev_rollback',
        created_at: '2026-03-21T10:00:00Z',
        gate_status: 'pass',
        apply_result: 'success',
        observation_status: 'rollback_recommended',
        observation_window_hours: 72,
        observation: {
          status: 'rollback_recommended',
          recommendation: 'rollback_recommended',
        },
        effectiveness: {
          conclusion: 'negative',
          detail: '实盘表现显著回撤',
        },
        current_active_parameter_set_id: 'ps_live_rollback',
        is_current_active_release: true,
      },
    ],
  },
  rdpWorkbenchOverview: {
    overall_status: 'rollback_required',
    headline: '有发布进入回滚建议状态，先处理回滚。',
    subheadline: '回滚优先级高于当前轮次里的新建议与新发布。',
    primary_action: {
      key: 'rollback',
      label: '执行回滚',
      ui_action: 'rdp-rollback-parameters',
      value: 'independent/15m',
      enabled: true,
    },
    secondary_actions: [],
    blockers: [
      {
        code: 'rollback_pending',
        severity: 'danger',
        title: '当前有发布进入回滚建议状态',
        message: '应先回滚，再处理新的发布与审批。',
        blocks_approval: true,
      },
    ],
    summary_counts: {
      pending_items: 0,
      integrity_blocked_items: 0,
      observing_releases: 1,
      tuning_pending: 0,
    },
    current_execution: { workflow: null, status: 'idle' },
    next_queue: { workflow: null, status: 'none' },
    health: {
      daemon: 'healthy',
      governance_db: 'healthy',
      latest_gate: 'pass',
    },
  },
  rdpWorkbenchItems: { total: 0, items: [], release_candidates: { total: 0, items: [] } },
  rdpWorkbenchAlerts: { integrity_alerts: [], operational_alerts: [] },
  rdpTuningOverview: {
    pending_review_count: 0,
    active_override_count: 0,
    headline: '当前没有新的自动调优提案。',
  },
  rdpTuningProposals: { total: 0, items: [] },
  uiState: { rdp: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-1',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: { position_intent: 'hold', current_position_qty: 0, target_position_qty: 0 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  decision_outcome: {
    decision_source: 'baseline_fallback',
    decision_authority: 'final_decision',
    decision_blocked_reasons: ['ai_confidence_below_threshold'],
  },
  ai_decision_audit: {
    configured_mode: 'ai_decision_maker',
    assessment_operating_mode: 'ai_decision_maker',
    decision_source: 'baseline_fallback',
    decision_authority: 'final_decision',
  },
});

console.log(JSON.stringify({
  analysisHasRuntimeSummary: analysisHtml.includes('AI 状态概览'),
  analysisHasDecisionChain: analysisHtml.includes('决策链概览'),
  analysisUsesStrategyShadowName: analysisHtml.includes('策略层 shadow'),
  analysisUsesExecutionShadowName: analysisHtml.includes('执行层 shadow'),
  analysisNoTopNavButtons: !analysisHtml.includes('前往 AI 工作台') && !analysisHtml.includes('前往 AI 配置'),
  configHasRuntimeModeCard: configHtml.includes('运行模式切换'),
  configHasAutoProfileControlCard: configHtml.includes('自动换档控制'),
  configUsesWorkbenchIdentity: configAndRdpHtml.includes('RDP 工作台') && !configAndRdpHtml.includes('RDP 核心面板'),
  configHasWorkbenchHero: configAndRdpHtml.includes('当前轮次'),
  configHasCoreQueue: configAndRdpHtml.includes('当前待处理'),
  configHasIntegrityPanel: configAndRdpHtml.includes('当前阻断'),
  configHasRuntimeRail: configAndRdpHtml.includes('后台状态'),
  configHasTuningCard: configAndRdpHtml.includes('自动调优'),
  configShowsTaskOrientedMetrics: configAndRdpHtml.includes('待处理') && configAndRdpHtml.includes('当前运行') && configAndRdpHtml.includes('下一项'),
  configShowsManualWorkflowActions: configAndRdpHtml.includes('刷新数据') && configAndRdpHtml.includes('运行完整 RDP') && !configAndRdpHtml.includes('只跑治理检查') && !configAndRdpHtml.includes('只跑决策链'),
  configRemovesWorkflowGridNoise: !configAndRdpHtml.includes('数据刷新执行') && !configAndRdpHtml.includes('研究流程排队'),
  configNoDirectApplyLanguage: !configAndRdpHtml.includes('data-action="rdp-approve-and-apply"') && !configAndRdpHtml.includes('data-action="rdp-apply-only"'),
  configShowsDecisionSummaryNotRawEvidence: configAndRdpHtml.includes('这轮产出了一组新参数，确认后就会进入待发布。') && !configAndRdpHtml.includes('[phase2_research]') && !configAndRdpHtml.includes('[phase3_attribution]'),
  configExplainsApprovalEffect: configAndRdpHtml.includes('如果批准：批准后进入待发布，下一步是运行 Gate 或创建发布。'),
  configShowsReleaseCandidates: configAndRdpHtml.includes('待发布候选') && configAndRdpHtml.includes('运行 Gate') && configAndRdpHtml.includes('创建发布'),
  configShowsApprovalBlockedCallout: configAndRdpHtml.includes('审批已阻断') && configAndRdpHtml.includes('证据不完整，当前不能审批'),
  configShowsEvidenceDrilldown: configAndRdpHtml.includes('查看证据详情') && configAndRdpHtml.includes('来源轮次') && configAndRdpHtml.includes('Step2 研究证据当前不可直接审批') && configAndRdpHtml.includes('Step2 轮次'),
  configShowsIntegrityAlerts: configAndRdpHtml.includes('最新归因结果不完整') && configAndRdpHtml.includes('治理数据库还没有接通，发布链路现在不可用。'),
  configShowsTuningProposalActions: configAndRdpHtml.includes('批准调优') && configAndRdpHtml.includes('拒绝调优'),
  configKeepsObservationSeparate: configAndRdpHtml.includes('观察与回滚') && configAndRdpHtml.includes('这些发布还在观察窗口内'),
  releaseEmptyUsesWorkbenchLayout: releaseEmptyCombinedHtml.includes('Directional / 1H') && releaseEmptyCombinedHtml.includes('参数候选待审批') && releaseEmptyCombinedHtml.includes('当前没有待审核的自动调优提案') && !releaseEmptyCombinedHtml.includes('待发布候选'),
  rollbackHeroPrioritizesRollback:
    rollbackCombinedHtml.split('当前待处理')[0].includes('data-action="rdp-rollback-parameters"')
    && !rollbackCombinedHtml.split('当前待处理')[0].includes('data-action="rdp-create-release"'),
  rollbackHeroExplainsPriority: rollbackCombinedHtml.includes('有发布进入回滚建议状态，先处理回滚。'),
  configHasRuntimeParams: configHtml.includes('运行参数概览'),
  configOmitsAdaptiveControls: !configHtml.includes('风险预算乘数') && !configHtml.includes('执行侵略性乘数'),
  configHasTimingControls: configHtml.includes('持有与冷却') && configHtml.includes('低边际保护'),
  configHasStrategyShadow: configHtml.includes('策略层 shadow'),
  configHasExecutionShadow: configHtml.includes('执行层 shadow'),
  configHasProfileControlModeButtons: configHtml.includes('手动切档') && configHtml.includes('自动切档'),
  configNoJumpButtons: !configHtml.includes('前往 AI 工作台') && !configHtml.includes('查看 AI 分析'),
  configAndRdpKeepsRdpWorkbench: configAndRdpHtml.includes('RDP 工作台') && configAndRdpHtml.includes('观察与回滚'),
  releaseEmptyCombinedShowsApprovalQueue: releaseEmptyCombinedHtml.includes('Directional / 1H') && releaseEmptyCombinedHtml.includes('参数候选待审批'),
  rollbackCombinedPrioritizesRollback: rollbackCombinedHtml.includes('有发布进入回滚建议状态，先处理回滚。') && rollbackCombinedHtml.includes('data-action="rdp-rollback-parameters"'),
  analysisHasAdaptiveControls: analysisHtml.includes('风险预算乘数') && analysisHtml.includes('自动切档闸门'),
  drawerExplainsFallback: drawer.body.includes('AI 已参与本轮评估'),
  drawerUsesHumanDecisionSource: drawer.body.includes('AI 未被采纳，沿用基础策略'),
  manualOnlyProfileDefaultsToManual: /<button class="primary-button" data-action="set-profile-control-mode" data-value="manual"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyProfileAutoEnabled: /<button class="secondary-button" data-action="set-profile-control-mode" data-value="auto"/.test(manualOnlyConfigHtml),
  manualOnlyProfileButtonsUnlocked: /data-action="manual-activate-strategy-profile" data-value="trend_strict"/.test(manualOnlyConfigHtml) && !/data-action="manual-activate-strategy-profile" data-value="trend_strict"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyRuntimeCurrentModeLocked: /<button class="primary-button" data-action="select-ai-operating-mode" data-value="baseline_only"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyRuntimePolicyLocksAlternativeModes:
    /data-action="select-ai-operating-mode" data-value="ai_assisted"[^>]*disabled/.test(manualOnlyConfigHtml)
    && /data-action="select-ai-operating-mode" data-value="ai_decision_maker"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyRuntimePolicyExplained: manualOnlyConfigHtml.includes('后端治理策略当前禁止从页面临时切换 AI 运行模式') && manualOnlyConfigHtml.includes('页面切换') && manualOnlyConfigHtml.includes('已禁用'),
  manualOnlyRuntimePolicyDoesNotExposeEnvVar: !manualOnlyConfigHtml.includes('AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE'),
  manualOnlyRuntimeAvoidsLegacyButtons: !manualOnlyConfigHtml.includes('跟随配置') && !manualOnlyConfigHtml.includes('手动接管'),
}));
"""
        result = _run_node_module(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"analysisHasRuntimeSummary":true', result.stdout)
        self.assertIn('"analysisHasDecisionChain":true', result.stdout)
        self.assertIn('"analysisUsesStrategyShadowName":true', result.stdout)
        self.assertIn('"analysisUsesExecutionShadowName":true', result.stdout)
        self.assertIn('"analysisNoTopNavButtons":true', result.stdout)
        self.assertIn('"configHasRuntimeModeCard":true', result.stdout)
        self.assertIn('"configHasAutoProfileControlCard":true', result.stdout)
        self.assertIn('"configUsesWorkbenchIdentity":true', result.stdout)
        self.assertIn('"configHasWorkbenchHero":true', result.stdout)
        self.assertIn('"configHasCoreQueue":true', result.stdout)
        self.assertIn('"configHasIntegrityPanel":true', result.stdout)
        self.assertIn('"configHasRuntimeRail":true', result.stdout)
        self.assertIn('"configHasTuningCard":true', result.stdout)
        self.assertIn('"configShowsTaskOrientedMetrics":true', result.stdout)
        self.assertIn('"configShowsManualWorkflowActions":true', result.stdout)
        self.assertIn('"configRemovesWorkflowGridNoise":true', result.stdout)
        self.assertIn('"configNoDirectApplyLanguage":true', result.stdout)
        self.assertIn('"configShowsDecisionSummaryNotRawEvidence":true', result.stdout)
        self.assertIn('"configExplainsApprovalEffect":true', result.stdout)
        self.assertIn('"configShowsApprovalBlockedCallout":true', result.stdout)
        self.assertIn('"configShowsEvidenceDrilldown":true', result.stdout)
        self.assertIn('"configShowsIntegrityAlerts":true', result.stdout)
        self.assertIn('"configShowsTuningProposalActions":true', result.stdout)
        self.assertIn('"configKeepsObservationSeparate":true', result.stdout)
        self.assertIn('"releaseEmptyUsesWorkbenchLayout":true', result.stdout)
        self.assertIn('"rollbackHeroPrioritizesRollback":true', result.stdout)
        self.assertIn('"rollbackHeroExplainsPriority":true', result.stdout)
        self.assertIn('"configHasRuntimeParams":true', result.stdout)
        self.assertIn('"configOmitsAdaptiveControls":true', result.stdout)
        self.assertIn('"configHasTimingControls":true', result.stdout)
        self.assertIn('"configHasStrategyShadow":true', result.stdout)
        self.assertIn('"configHasExecutionShadow":true', result.stdout)
        self.assertIn('"configHasProfileControlModeButtons":true', result.stdout)
        self.assertIn('"configNoJumpButtons":true', result.stdout)
        self.assertIn('"configAndRdpKeepsRdpWorkbench":true', result.stdout)
        self.assertIn('"releaseEmptyCombinedShowsApprovalQueue":true', result.stdout)
        self.assertIn('"rollbackCombinedPrioritizesRollback":true', result.stdout)
        self.assertIn('"analysisHasAdaptiveControls":true', result.stdout)
        self.assertIn('"drawerExplainsFallback":true', result.stdout)
        self.assertIn('"drawerUsesHumanDecisionSource":true', result.stdout)
        self.assertIn('"manualOnlyProfileDefaultsToManual":true', result.stdout)
        self.assertIn('"manualOnlyProfileAutoEnabled":true', result.stdout)
        self.assertIn('"manualOnlyProfileButtonsUnlocked":true', result.stdout)
        self.assertIn('"manualOnlyRuntimeCurrentModeLocked":true', result.stdout)
        self.assertIn('"manualOnlyRuntimePolicyLocksAlternativeModes":true', result.stdout)
        self.assertIn('"manualOnlyRuntimePolicyExplained":true', result.stdout)
        self.assertIn('"manualOnlyRuntimePolicyDoesNotExposeEnvVar":true', result.stdout)
        self.assertIn('"manualOnlyRuntimeAvoidsLegacyButtons":true', result.stdout)

    def test_ai_analysis_view_uses_truthful_ai_enabled_semantics(self) -> None:
        script = """
import { renderAIAnalysisView } from './aats/api/static/modules/views/ai-analysis-view.js';

const html = renderAIAnalysisView({
  session: { role: 'admin' },
  blockerControl: {},
  aiRuntime: {
    configured_operating_mode: 'ai_decision_maker',
    effective_operating_mode: 'ai_decision_maker',
    operating_mode_source: 'manual_selection',
    provider_ready: true,
    provider_state: 'healthy',
    outcome_state: 'auto_downgraded',
    outcome_review_required: false,
    shadow_mode_enabled: true,
    recent_fallback_ratio: 0.04,
    recent_timeout_count: 0,
    recent_invalid_output_count: 1,
    consecutive_failures: 0,
    consecutive_successes: 13,
    recent_assessment_count: 25,
    failure_budget: {
      remaining_failures_until_degrade: 3,
      remaining_successes_until_recover: 0,
    },
    outcome_policy: {
      bad_window_threshold: 3,
      remaining_bad_windows_until_review: 0,
    },
  },
  aiOverview: {
    runtime: {
      configured_operating_mode: 'ai_decision_maker',
      effective_operating_mode: 'ai_decision_maker',
    },
    downgrade_state: {
      provider_state: 'not_loaded',
      outcome_state: 'not_loaded',
      failure_budget: {},
      outcome_policy: {},
    },
    latest_assessment: {
      created_at: '2026-04-25T01:10:33Z',
      provider_name: 'deepseek',
      model_name: 'deepseek-v4-flash',
      model_version: '1.0.0',
      prompt_version: '0.3.0',
      provider_latency_ms: 12826.4,
      output_valid: true,
      fallback_used: false,
      economically_actionable: false,
      estimated_edge_bps: 5,
      estimated_cost_bps: 11,
      estimated_net_edge_bps: -6,
      directional_edge: 0.12,
      validation_flags: ['low_edge'],
      rejection_flags: [],
      regime: 'trend',
    },
    latest_baseline_reference: {
      direction_bias: 'flat',
      confidence: 0.53,
      reason_codes: ['baseline_direction_threshold_trend_0_100'],
    },
    latest_ai_decision_intent: null,
    latest_decision_outcome: {
      decision_source: 'baseline',
      decision_authority: 'final_decision',
      final_action: 'hold',
      final_target_qty: 0,
      decision_blocked_reasons: [],
      profile_control_source: 'system',
    },
    latest_profile_control_decision: null,
    latest_shadow_decision: {
      created_at: '2026-04-25T01:10:35Z',
      baseline_action: 'hold',
      baseline_target_qty: 0,
      ai_shadow_action: 'long',
      ai_shadow_target_qty: 1,
      would_override_baseline: true,
      shadow_action_type: 'override_target',
      reason_codes: ['low_edge'],
    },
    shadow_summary: {
      window_count: 10,
      outperformed_rate: 0,
      status: 'underperforming',
      review_required: false,
      latest_net_pnl_delta: -6.9,
      latest_fee_ratio_delta: 7.0,
      latest_churn_ratio_delta: 1,
    },
    performance_windows: {
      short: { net_pnl_delta_total: -20.968, outperformed_rate: 0, sample_size: 3 },
      medium: { net_pnl_delta_total: -34.9467, outperformed_rate: 0, sample_size: 5 },
      long: { avg_fee_ratio_delta: 8.1261, avg_churn_ratio_delta: 1, review_required_count: 2, sample_size: 10 },
    },
    performance_view: {
      report_count: 20,
      status_counts: { pending_review: 8, underperforming: 12 },
      trend: { avg_short_net_pnl_delta: -11.5775, avg_medium_net_pnl_delta: -17.4549 },
      replay_context: { validation_count: 0, healthy_rate: 0, latest_validation: null },
      recent_reports: [],
    },
  },
  aiLatest: {},
  aiRecent: {
    assessments: [{
      created_at: '2026-04-25T01:09:30Z',
      regime: 'trend',
      directional_edge: 0.12,
      baseline_override_recommended: false,
      economically_actionable: false,
      estimated_net_edge_bps: -6,
      output_valid: true,
      fallback_used: true,
      fallback_reason: 'ai_timeout',
      validation_flags: ['low_edge'],
      rejection_flags: [],
    }],
    total_available: 1,
    limit: 8,
    has_more: false,
  },
  aiShadowRecent: {
    shadow_decisions: [{
      created_at: '2026-04-25T01:10:35Z',
      baseline_action: 'hold',
      baseline_target_qty: 0,
      ai_shadow_action: 'long',
      ai_shadow_target_qty: 1,
      would_override_baseline: true,
      shadow_action_type: 'override_target',
      reason_codes: ['low_edge'],
    }],
    total_available: 1,
    limit: 8,
    has_more: false,
  },
  aiShadowEvaluations: {
    evaluations: [{
      window_start: '2026-04-25T00:00:00Z',
      window_end: '2026-04-25T01:00:00Z',
      baseline_net_pnl: 1,
      baseline_gross_pnl: 2,
      baseline_trade_count: 1,
      shadow_net_pnl: -1,
      shadow_gross_pnl: 0,
      shadow_trade_count: 1,
      baseline_fee_ratio: 0.01,
      shadow_fee_ratio: 0.08,
      baseline_churn_ratio: 0,
      shadow_churn_ratio: 1,
      shadow_outperformed: false,
    }],
    total_available: 1,
    limit: 8,
    has_more: false,
  },
  profileControlSummary: {
    control_summary: {
      safety_profile_required: true,
      evidence: { cold_start_active: true, closed_trades: 0, min_closed_trades: 0, replay_validations: 0, min_replay_validations: 5 },
      adaptive_controls: {
        risk_budget: { multiplier: 0.4, status: 'contracted', reasons: ['execution_errors_elevated'] },
        execution_aggressiveness: { multiplier: 0.3, status: 'safe_mode', reasons: ['execution_errors_elevated'] },
      },
    },
    activation: { active_profile_id: 'trend_normal', last_activation_at: '2026-04-25T00:59:43Z' },
    active_revision: { profile_id: 'trend_normal', profile_label: '趋势标准' },
    latest_selection_decision: {
      candidate_profile_id: 'trend_normal',
      transition_class: 'same_risk_optimization',
      blocked_reasons: [
        'strategy_profile_already_active',
        'strategy_profile_switch_cooldown_active',
        'strategy_profile_reconciliation_not_clean',
        'strategy_profile_requires_more_replay_validations',
        'strategy_profile_cold_start_lock_active',
      ],
      gating_state: {
        confidence_floor: 0.8,
        remaining_closed_trades: 0,
        remaining_replay_validations: 5,
        reconciliation_clean: false,
        fast_track_reasons: ['safety_profile_required', 'execution_errors_elevated'],
        fast_track_bypass_gates: [],
      },
    },
    latest_optimization_report: {
      recommended_profile_id: 'execution_degraded_safe',
      score_delta_vs_active: 0,
      notes: [],
    },
  },
});

console.log(JSON.stringify({
  providerUsesRuntime: html.includes('模型服务状态') && html.includes('运行正常') && !html.includes('not_loaded'),
  modeSourceNotOverstated: html.includes('当前运行模式与配置一致：AI 决策者') && !html.includes('当前运行模式已手动切到 AI 决策者'),
  decisionSourceLocalized: html.includes('基础策略路径') && !html.includes('>baseline<'),
  noIntentIsFactual: html.includes('本轮未生成 AI 交易意图') && !html.includes('或 AI 已回退') && !html.includes('当前暂无新的 AI 决策意图'),
  lowEdgeShownAsAdoptionReason: html.includes('AI 未进入最终路径的原因') && html.includes('净优势偏低'),
  modelAndLatencyShown: html.includes('deepseek-v4-flash v1.0.0') && html.includes('12.83 秒') && html.includes('prompt 0.3.0'),
  profileCandidateUsesGateSelection: html.includes('候选策略档位') && html.includes('趋势标准') && html.includes('优化器建议 执行降级安全'),
  replayZeroIsNotFalseUnhealthy: html.includes('暂无回放验证') && !html.includes('回放健康率</span>\\n              <strong class=\"summary-tile__value\">0</strong>'),
  windowsUseOverviewSource: html.includes('-20.968') && html.includes('-34.9467') && !html.includes('近 3 窗口净收益差</span>\\n              <strong class=\"summary-tile__value\">暂未形成结论</strong>'),
  assessmentFallbackIsNotFinalFallback: html.includes('使用回退评估') && !html.includes('最终回退到基础策略') && !html.includes('最终采用模型结果'),
  shadowCopyIsObservationOnly: html.includes('提出改写建议（未直接执行）') && !html.includes('会不会真的改动') && !html.includes('AI 影子结果'),
}));
"""
        result = _run_node_module(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"providerUsesRuntime":true', result.stdout)
        self.assertIn('"modeSourceNotOverstated":true', result.stdout)
        self.assertIn('"decisionSourceLocalized":true', result.stdout)
        self.assertIn('"noIntentIsFactual":true', result.stdout)
        self.assertIn('"lowEdgeShownAsAdoptionReason":true', result.stdout)
        self.assertIn('"modelAndLatencyShown":true', result.stdout)
        self.assertIn('"profileCandidateUsesGateSelection":true', result.stdout)
        self.assertIn('"replayZeroIsNotFalseUnhealthy":true', result.stdout)
        self.assertIn('"windowsUseOverviewSource":true', result.stdout)
        self.assertIn('"assessmentFallbackIsNotFinalFallback":true', result.stdout)
        self.assertIn('"shadowCopyIsObservationOnly":true', result.stdout)

    def test_ai_config_safety_profile_is_explained_as_guard_not_default(self) -> None:
        script = """
import { renderAIConfigView } from './aats/api/static/modules/views/ai-config-view.js';

const html = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'ai_decision_maker',
    effective_operating_mode: 'ai_decision_maker',
    strategy_profile_auto_control_configured: true,
    strategy_profile_auto_control_effective: true,
    strategy_profile_auto_control_reason: 'configured_auto',
  },
  summary: {
    ai: {
      configured_operating_mode: 'ai_decision_maker',
      effective_operating_mode: 'ai_decision_maker',
      strategy_profile_auto_control_configured: true,
      strategy_profile_auto_control_effective: true,
      strategy_profile_auto_control_reason: 'configured_auto',
    },
    runtime_profile: { current_runtime_payload: {} },
    strategy_profile: {
      activation: {
        active_profile_id: 'execution_degraded_safe',
        auto_switch_enabled: true,
        last_activation_result: 'activation_succeeded',
      },
      active_revision: {
        profile_id: 'execution_degraded_safe',
        profile_label: 'Execution Safe',
      },
      latest_selection_decision: {
        candidate_profile_id: 'execution_degraded_safe',
        execution_state: 'executed',
        fast_track_applied: false,
        fast_track_eligible: false,
        blocked_reasons: ['strategy_profile_already_active'],
        gating_state: {
          fast_track_reasons: ['safety_profile_required', 'execution_errors_elevated'],
        },
      },
      latest_optimization_report: {
        recommended_profile_id: 'execution_degraded_safe',
      },
      activation_history: [
        {
          to_profile_id: 'execution_degraded_safe',
          trigger_type: 'system_guard',
          executed_at: '2026-04-24T22:30:36Z',
        },
      ],
    },
  },
});

console.log(JSON.stringify({
  safetyTitle: html.includes('安全保护档已生效'),
  explainsNotDefault: html.includes('这不是默认档位'),
  showsGuardTrigger: html.includes('最近触发来源：系统保护'),
  calloutWarning: html.includes('class="callout tone-warning"'),
  currentSafetyButtonWarning: /<button class="warning-button" data-action="manual-activate-strategy-profile" data-value="execution_degraded_safe"[^>]*>执行降级安全（当前）/.test(html),
  avoidsMisleadingObserveCopy: !html.includes('正在观察执行降级安全'),
}));
"""
        result = _run_node_module(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"safetyTitle":true', result.stdout)
        self.assertIn('"explainsNotDefault":true', result.stdout)
        self.assertIn('"showsGuardTrigger":true', result.stdout)
        self.assertIn('"calloutWarning":true', result.stdout)
        self.assertIn('"currentSafetyButtonWarning":true', result.stdout)
        self.assertIn('"avoidsMisleadingObserveCopy":true', result.stdout)

    def test_rdp_view_surfaces_auth_failures_instead_of_fake_loading(self) -> None:
        script = """
import { renderRdpView } from './aats/api/static/modules/views/rdp-view.js';

const html = renderRdpView({
  session: {
    authenticated: false,
    role: 'anonymous',
    auth_blocked_reason: 'operator_https_required_for_secure_session',
  },
  authProviders: {
    auth_enabled: true,
    session_enabled: true,
    secure_cookie_required: true,
    transport_compatible: false,
    required_transport: 'https',
    auth_blocked_reason: 'operator_https_required_for_secure_session',
  },
  summary: {
    ai: {},
    runtime_profile: {},
    strategy_profile: {},
  },
  rdpWorkbenchOverview: {},
  rdpWorkbenchItems: {},
  rdpWorkbenchAlerts: {},
  rdpTuningOverview: {},
  rdpTuningProposals: {},
  errors: {
    rdpWorkbenchOverview: 'operator_https_required_for_secure_session',
    rdpWorkbenchItems: 'operator_https_required_for_secure_session',
    rdpWorkbenchAlerts: 'operator_https_required_for_secure_session',
  },
  uiState: { aiConfig: {} },
});

console.log(JSON.stringify({
  showsAuthCallout: html.includes('RDP 访问需要先建立会话'),
  showsHttpsHint: html.includes('当前入口使用 HTTP，受保护会话要求 HTTPS，请通过 HTTPS 访问控制台。'),
  hidesFakeLoading: !html.includes('RDP 数据暂未就绪'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"showsAuthCallout":true', stdout)
        self.assertIn('"showsHttpsHint":true', stdout)
        self.assertIn('"hidesFakeLoading":true', stdout)

    def test_protected_auth_blocked_view_distinguishes_transport_and_permission_errors(self) -> None:
        script = """
import { renderProtectedAuthBlockedView } from './aats/api/static/modules/views/protected-auth-view.js';

const transportBlocked = renderProtectedAuthBlockedView({
  viewLabel: 'AI 配置',
  authSummary: {
    access_state: 'transport_blocked',
    primary_error: 'operator_https_required_for_secure_session',
  },
  authProviders: {
    auth_blocked_reason: 'operator_https_required_for_secure_session',
  },
});

const adminBlocked = renderProtectedAuthBlockedView({
  viewLabel: '系统设置',
  authSummary: {
    access_state: 'auth_required',
    primary_error: 'operator_admin_access_required',
  },
});

console.log(JSON.stringify({
  transportShowsHttps: transportBlocked.includes('当前入口无法建立安全会话')
    && transportBlocked.includes('当前入口使用 HTTP，受保护会话要求 HTTPS，请通过 HTTPS 访问控制台。')
    && transportBlocked.includes('前往登录页'),
  adminShowsPermissionCopy: adminBlocked.includes('当前账号没有管理员权限')
    && adminBlocked.includes('请切换到管理员账号后重试。')
    && !adminBlocked.includes('前往登录页'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"transportShowsHttps":true', stdout)
        self.assertIn('"adminShowsPermissionCopy":true', stdout)

    def test_rdp_action_handlers_use_default_observation_windows_without_prompt(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { createRdpActionHandlers } from './aats/api/static/modules/actions/rdp-actions.js';

const requests = [];
let promptCount = 0;
let refreshCount = 0;
const state = {
  actionInFlight: false,
  flash: null,
  data: {
    rdpControl: {
      environment: {
        required_observation_window_hours: 72,
      },
    },
  },
};

const handlers = createRdpActionHandlers({
  beginAction: () => {
    state.actionInFlight = true;
    return () => {
      state.actionInFlight = false;
    };
  },
  renderBanners: () => {},
  refreshDashboard: async () => {
    refreshCount += 1;
  },
  requestJson: async (path, options = {}) => {
    requests.push({ path, body: options.body || null });
    if (path === '/rdp/releases/create') {
      return { ok: true, release: { release_id: 'rel-created-1' } };
    }
    if (path === '/rdp/observations/run') {
      return { ok: true, status: 'observing' };
    }
    return { ok: true };
  },
  state,
  windowRef: {
    confirm: () => true,
    prompt: () => {
      promptCount += 1;
      return '999';
    },
  },
});

await handlers['rdp-create-release']('rec-1');
await handlers['rdp-run-observation']('rel-1', { dataset: { hours: '48' } });

console.log(JSON.stringify({
  promptCount,
  refreshCount,
  createReleaseUsesEnvWindow: requests.some((item) =>
    item.path === '/rdp/releases/create'
    && item.body
    && item.body.observation_window_hours === 72
  ),
  runObservationUsesReleaseWindow: requests.some((item) =>
    item.path === '/rdp/observations/run'
    && item.body
    && item.body.window_hours === 48
  ),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"promptCount":0', result.stdout)
        self.assertIn('"refreshCount":2', result.stdout)
        self.assertIn('"createReleaseUsesEnvWindow":true', result.stdout)
        self.assertIn('"runObservationUsesReleaseWindow":true', result.stdout)

    def test_rdp_rollback_handler_obtains_token_before_rollback(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { createRdpActionHandlers } from './aats/api/static/modules/actions/rdp-actions.js';

const requests = [];
let refreshCount = 0;
const state = {
  actionInFlight: false,
  flash: null,
  data: {},
};

const handlers = createRdpActionHandlers({
  beginAction: () => {
    state.actionInFlight = true;
    return () => {
      state.actionInFlight = false;
    };
  },
  renderBanners: () => {},
  refreshDashboard: async () => {
    refreshCount += 1;
  },
  requestJson: async (path, options = {}) => {
    requests.push({
      path,
      method: options.method || 'GET',
      body: options.body || null,
      headers: options.headers || {},
    });
    if (path === '/rdp/operator-tokens') {
      return { token: 'rollback-token-1', action: 'rollback', ttl_seconds: 300 };
    }
    if (path === '/rdp/parameters/rollback') {
      return { ok: true };
    }
    return { ok: true };
  },
  state,
  windowRef: {
    confirm: () => true,
  },
});

await handlers['rdp-rollback-parameters']('independent/15m');

const tokenRequest = requests.find((item) => item.path === '/rdp/operator-tokens');
const rollbackRequest = requests.find((item) => item.path === '/rdp/parameters/rollback');
console.log(JSON.stringify({
  refreshCount,
  tokenRequestedFirst: requests[0]?.path === '/rdp/operator-tokens',
  tokenAction: tokenRequest?.body?.action,
  rollbackHasTokenHeader: rollbackRequest?.headers?.['X-Rdp-Apply-Token'] === 'rollback-token-1',
  rollbackBodyAligned: rollbackRequest?.body?.family === 'independent'
    && rollbackRequest?.body?.timeframe === '15m'
    && rollbackRequest?.body?.actor === 'operator',
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn('"refreshCount":1', result.stdout)
        self.assertIn('"tokenRequestedFirst":true', result.stdout)
        self.assertIn('"tokenAction":"rollback"', result.stdout)
        self.assertIn('"rollbackHasTokenHeader":true', result.stdout)
        self.assertIn('"rollbackBodyAligned":true', result.stdout)

    def test_strategy_view_renders_smart_arbitrage_config_card_and_threshold_copy(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  latestDecision: {
    decision_id: 'dec-1',
    decision_time: '2026-03-26T12:00:00Z',
    baseline_assessment: { regime: 'range', confidence: 0.41 },
    position_target: {
      strategy_family: 'smart_arbitrage',
      position_intent: 'hold',
      current_position_qty: 0,
      target_position_qty: 0,
      delta_position_qty: 0,
      product_type: 'derivatives',
      margin_mode: 'cross',
    },
    policy_decision: {
      execution_allowed: false,
      blocker_reasons: ['smart_arbitrage_basis_below_entry_threshold'],
    },
    risk_decision: {
      approved: false,
      rejection_reasons: ['smart_arbitrage_basis_below_entry_threshold'],
    },
    decision_outcome: {
      selected_strategy_family: 'smart_arbitrage',
    },
    decision_context: {
      symbol: 'BTC-USDT-SWAP',
      current_position_qty: 0,
      as_of_ts: '2026-03-26T12:00:00Z',
      product_type: 'derivatives',
    },
  },
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: {
    summary: {
      automatic_selection_enabled: true,
      configured_active_family: 'smart_arbitrage',
      latest_selected_family: 'smart_arbitrage',
      latest_selected_state: 'inactive',
      latest_bundle_status: 'inactive',
      latest_portfolio_requested_notional: 0,
      latest_portfolio_approved_notional: 0,
      latest_portfolio_budget_cut_notional: 0,
      entry_execution_guard: {
        active: true,
        headline: '当前 non-protective entry execution 已被降级为 advisory-only。',
        summary: '当前 non-protective entry execution 已被降级为 advisory-only；新的非保护性开仓/加仓只做参考，不会自动下单，保护性收缩仍可继续执行。',
      },
      latest_approved_sleeve_weights: {},
      latest_selection_reason_codes: ['smart_arbitrage_basis_below_entry_threshold'],
    },
    latest_snapshot: {
      candidates: [
        {
          family: 'smart_arbitrage',
          state: 'inactive',
          route_action: 'hold_current',
          urgency: 'low',
          target_position_qty: 0,
          delta_position_qty: 0,
          reason_codes: ['smart_arbitrage_basis_below_entry_threshold'],
          pair_id: 'btc_usdt_swap',
          metrics: {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            derivatives_symbol: 'BTC-USDT-SWAP',
            basis_bps: 12,
          },
          legs: [],
        },
      ],
      automation_decisions: [],
    },
    configured_parameters: {
      trade_costs: {
        rate_unit: 'bps',
        rate_example: '8 = 0.08%',
        live_fee_resolution: 'account_schedule_fallback_to_configured',
        spot_maker_fee_bps: 8,
        spot_taker_fee_bps: 10,
        margin_maker_fee_bps: 8,
        margin_taker_fee_bps: 10,
        derivatives_maker_fee_bps: 2,
        derivatives_taker_fee_bps: 5,
        delivery_settlement_fee_bps: 1,
        spot_spread_bps: 1.2,
        spot_slippage_bps: 0.6,
        margin_spread_bps: 1.0,
        margin_slippage_bps: 0.5,
        derivatives_spread_bps: 0.8,
        derivatives_slippage_bps: 0.4,
      },
      smart_arbitrage: {
        enabled: true,
        pair_definitions: [
          {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            hedge_symbol: 'BTC-USDT-SWAP',
            metadata: { source: 'configured' },
          },
        ],
        basis_exit_bps: 6,
        estimated_cost_bps: 34,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        cost_model_enabled: true,
        funding_cost_enabled: false,
        borrow_cost_enabled: true,
        fee_source_mode: 'configured',
        funding_source_mode: 'configured',
        borrow_source_mode: 'apr_window_model',
        expected_hold_hours: 8,
        funding_interval_hours: 8,
        expected_funding_events: 1,
        hedge_target_leverage: 3,
        negative_basis_mode: 'margin_backed',
        inventory_reservation_enabled: false,
        margin_short_enabled: true,
        margin_short_execution_ready: true,
        margin_short_spot_margin_mode: 'cross',
        margin_short_auto_repay_enabled: false,
        max_concurrent_pairs: 1,
        pair_priority_mode: 'net_edge',
        min_inventory_backed_ratio: 1,
        uses_global_trade_costs: true,
        estimated_execution_mismatch_bps: 0.5,
        estimated_transfer_cost_bps: 0.2,
        time_decay_bps_per_hour: 0.1,
        estimated_borrow_apr: 18,
        borrow_interest_free_ratio: 0,
        estimated_funding_bps: 0,
        estimated_borrow_bps: 0,
      },
    },
    smart_arbitrage_cost_summary: {
      available: true,
      pair_label: 'BTC-USDT <-> BTC-USDT-SWAP',
      predicted: {
        ideal_edge_bps: 14,
        executable_edge_bps: 8,
        breakeven_basis_bps: 10,
        ideal_total_fee_bps: 2,
        executable_spread_bps: 2,
        executable_slippage_bps: 1,
        execution_mismatch_bps: 0.5,
        funding_cost_bps: 0,
        borrow_cost_bps: 0,
        transfer_cost_bps: 0.2,
        time_decay_cost_bps: 0.8,
        cost_source_flags: ['fee_configured_per_leg', 'spread_configured_per_leg', 'slippage_configured_per_leg', 'execution_mismatch_configured'],
      },
      realized: {
        realized_total_drag_bps: 3.2,
      },
      calibration: {
        predicted_vs_realized_total_drag_error_bps: 1.4,
      },
    },
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: {
    summary: {},
    profitability_by_strategy_sleeve: [],
    sleeve_inventory_summary: [],
  },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesReferenceCards: !html.includes('trade_cost_spot_taker_fee_bps')
    && !html.includes('smart_arbitrage_quote_budget_per_trade')
    && !html.includes('smart_arbitrage_cost_model_enabled')
    && !html.includes('BTC-USDT &lt;-&gt; BTC-USDT-SWAP'),
  showsEntryExecutionGuard: html.includes('当前非保护性开仓已降级为仅参考')
    && html.includes('当前 non-protective entry execution 已被降级为 advisory-only')
    && html.includes('保护性收缩仍可执行'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesReferenceCards":true', result.stdout)
        self.assertIn('"showsEntryExecutionGuard":true', result.stdout)

    def test_strategy_view_uses_combined_net_pnl_copy_in_trial_review_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  latestDecision: {},
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: {},
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: {
    summary: {
      readiness: 'watch',
      combined_net_realized_pnl: -1.23,
      net_realized_pnl: 9.99,
      closed_fill_count: 4,
      fee_to_notional_ratio: 0.0012,
      headline: '最近综合净收益已经转负，先不要继续放大试盘。',
    },
    recommendation: { action_items: [], reasons: [] },
    scaling_readiness: {
      readiness: 'watch',
      safe_to_trade: true,
      review_required: false,
      active_blocker_count: 0,
      trial_guard_status: 'normal',
    },
    sections: {
      forward_validation: {
        summary: { verdict: 'watch', reasons: [] },
        periods: [
          {
            period_label: '最近观察周期',
            verdict: 'watch',
            combined_net_realized_pnl: -0.45,
            net_realized_pnl: 8.88,
            fee_to_notional_ratio: 0.001,
            avg_adverse_slippage_bps: 1.2,
            closed_fill_count: 2,
            reasons: [],
          },
        ],
      },
    },
  },
});

console.log(JSON.stringify({
  usesCombinedSummaryLabel: html.includes('最近综合净收益') && !html.includes('最近净收益'),
  usesCombinedTableHeader: html.includes('综合净收益'),
  prefersCombinedSummaryValue: html.includes('<strong class=\"summary-tile__value\">-0.45</strong>') && !html.includes('8.88'),
  prefersCombinedPeriodValue: html.includes('-0.45'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"usesCombinedSummaryLabel":true', result.stdout)
        self.assertIn('"usesCombinedTableHeader":true', result.stdout)
        self.assertIn('"prefersCombinedSummaryValue":true', result.stdout)
        self.assertIn('"prefersCombinedPeriodValue":true', result.stdout)

    def test_strategy_view_surfaces_budget_zero_suppression_as_non_permission_block(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    summary: {
      budget_zero_suppression_count: 2,
      execution_behavior_counts: {
        execute_target: 0,
        hold_current: 0,
        advisory_only: 0,
        suppressed_after_approval: 2,
        protective_execute: 0,
      },
      execution_control_summary: {
        active: true,
        primary_mode: 'budget_zero_suppressed',
        headline: '最近自动执行主要受预算压零抑制',
        summary: '最近 2 条 sleeve intent 已允许自动执行，但预算层把可执行量压成了 0。',
        total_recent_intents: 2,
      },
      execution_behavior_summary: {
        active: true,
        primary_behavior: 'suppressed_after_approval',
        headline: '最近执行行为以批准后压零为主',
        summary: '最近 2 条 sleeve intent 已获批准，但最终执行行为仍是压零保留。',
        total_recent_intents: 2,
      },
      latest_approved_sleeve_weights: {},
      entry_execution_guard: {
        active: false,
      },
    },
    latest_snapshot: {
      candidates: [],
      automation_decisions: [],
    },
    configured_parameters: {
      strategy_sleeve_auto_execution_enabled: true,
      strategy_sleeve_auto_min_budget_multiplier: 0.25,
      strategy_sleeve_auto_soft_loss_usdt: 20,
      strategy_sleeve_auto_hard_loss_usdt: 50,
    },
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {},
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  showsExecutionControlSummaryCallout: html.includes('最近自动执行主要受预算压零抑制')
    && html.includes('预算压零')
    && html.includes('最近样本 2')
    && html.includes('最近 2 条 sleeve intent 已允许自动执行，但预算层把可执行量压成了 0。'),
  showsBudgetZeroSuppressionKv: html.includes('预算压零抑制')
    && html.includes('表示权限已通过，但预算层把最终可执行量压成了 0。'),
  showsExecutionControlSummaryKv: html.includes('最近自动控制摘要')
    && html.includes('最近自动执行主要受预算压零抑制'),
  showsExecutionBehaviorSummaryKv: html.includes('最近执行行为摘要')
    && html.includes('最近执行行为以批准后压零为主')
    && html.includes('最近 2 条 sleeve intent 已获批准，但最终执行行为仍是压零保留。'),
  showsExecutionBehaviorDistributionKv: html.includes('最近执行行为分布')
    && html.includes('批准后压零 2'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"showsExecutionControlSummaryCallout":true', result.stdout)
        self.assertIn('"showsBudgetZeroSuppressionKv":true', result.stdout)
        self.assertIn('"showsExecutionControlSummaryKv":true', result.stdout)
        self.assertIn('"showsExecutionBehaviorSummaryKv":true', result.stdout)
        self.assertIn('"showsExecutionBehaviorDistributionKv":true', result.stdout)

    def test_strategy_view_surfaces_control_mode_distribution_and_deprecated_config_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    summary: {
      budget_zero_suppression_count: 0,
      execution_control_mode_counts: {
        approved: 0,
        permission_denied: 1,
        budget_zero_suppressed: 0,
        protective_override: 1,
      },
      execution_behavior_counts: {
        execute_target: 0,
        hold_current: 0,
        advisory_only: 1,
        suppressed_after_approval: 0,
        protective_execute: 1,
      },
      execution_control_summary: {
        active: true,
        primary_mode: 'permission_denied',
        headline: '最近自动执行主要受权限拒绝影响',
        summary: '最近 1 条 sleeve intent 因执行权限未通过被降级为 advisory-only 或 hold-current。',
        total_recent_intents: 2,
      },
      execution_behavior_summary: {
        active: true,
        primary_behavior: 'advisory_only',
        headline: '最近执行行为以仅参考为主',
        summary: '最近 1 条 sleeve intent 的最终执行行为是 advisory-only。',
        total_recent_intents: 2,
      },
      entry_execution_guard: {
        active: true,
        summary: '当前 non-protective entry execution 已被降级为 advisory-only。',
      },
      entry_auto_execution_config_source: 'strategy_sleeve_auto_execution_enabled',
      entry_auto_execution_uses_deprecated_key: false,
      latest_approved_sleeve_weights: {},
    },
    latest_snapshot: {
      candidates: [],
      automation_decisions: [
        {
          strategy_sleeve_id: 'sleeve_protective',
          family: 'protective',
          automation_state: 'protective_only',
          compatibility: {
            legacy_automation_state: 'protective_only',
          },
          execution_control_mode: 'protective_override',
          execution_behavior: 'protective_execute',
          budget_multiplier: 1,
          allocator_weight: 0,
          recent_net_pnl: 0,
          operator_summary: '保护性例外仍可执行',
        },
      ],
    },
    configured_parameters: {
      strategy_sleeve_auto_execution_enabled: false,
      strategy_sleeve_auto_execution_config_source: 'strategy_sleeve_auto_execution_enabled',
      strategy_sleeve_auto_execution_uses_deprecated_key: false,
      compatibility: {
        deprecated_auto_execution_key: 'strategy_sleeve_auto_parallel_enabled',
        deprecated_auto_execution_value: null,
      },
      strategy_sleeve_auto_min_budget_multiplier: 0.25,
      strategy_sleeve_auto_soft_loss_usdt: 20,
      strategy_sleeve_auto_hard_loss_usdt: 50,
    },
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {},
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  showsDeprecatedConfigCallout: html.includes('自动执行仍在使用旧配置键')
    && html.includes('strategy_sleeve_auto_parallel_enabled')
    && html.includes('建议迁移到 strategy_sleeve_auto_execution_enabled'),
  showsControlModeDistribution: html.includes('最近控制模式分布')
    && html.includes('正常放行 0')
    && html.includes('权限拒绝 1')
    && html.includes('预算压零 0')
    && html.includes('保护性例外 1'),
  showsExecutionControlSummaryKv: html.includes('最近自动控制摘要')
    && html.includes('最近自动执行主要受权限拒绝影响')
    && html.includes('最近 1 条 sleeve intent 因执行权限未通过被降级为 advisory-only 或 hold-current。'),
  showsExecutionBehaviorSummaryKv: html.includes('最近执行行为摘要')
    && html.includes('最近执行行为以仅参考为主')
    && html.includes('最近 1 条 sleeve intent 的最终执行行为是 advisory-only。'),
  showsExecutionBehaviorDistributionKv: html.includes('最近执行行为分布')
    && html.includes('仅参考 1')
    && html.includes('保护性执行 1'),
  showsProtectiveOverrideRowMeta: html.includes('保护性例外')
    && html.includes('保护性执行')
    && html.includes('保护性例外仍可执行'),
  hidesLegacyAutomationStateLabel: !html.includes('protective_only'),
  hidesLegacyAutomationStateField: !html.includes('legacy_automation_state'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"showsDeprecatedConfigCallout":false', result.stdout)
        self.assertIn('"showsControlModeDistribution":true', result.stdout)
        self.assertIn('"showsExecutionControlSummaryKv":true', result.stdout)
        self.assertIn('"showsExecutionBehaviorSummaryKv":true', result.stdout)
        self.assertIn('"showsExecutionBehaviorDistributionKv":true', result.stdout)
        self.assertIn('"showsProtectiveOverrideRowMeta":true', result.stdout)
        self.assertIn('"hidesLegacyAutomationStateLabel":true', result.stdout)
        self.assertIn('"hidesLegacyAutomationStateField":true', result.stdout)

    def test_strategy_view_hides_derivatives_only_reference_blocks_for_spot_runtime(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  latestDecision: {
    decision_id: 'dec-spot-reference',
    decision_time: '2026-03-27T12:00:00Z',
    baseline_assessment: { regime: 'trend', direction_bias: 'flat', confidence: 0.55 },
    ai_assessment: { directional_edge: 0.03 },
    position_target: {
      strategy_family: 'spot_grid',
      position_intent: 'hold',
      current_position_qty: 0.2,
      target_position_qty: 0.2,
      delta_position_qty: 0,
      product_type: 'spot',
      margin_mode: 'cash',
      target_exposure_side: 'long',
      guardrail_flags: [],
    },
    policy_decision: { execution_allowed: true, allow_reasons: ['spot_grid_rebalance_ready'] },
    risk_decision: { approved: true, approval_reasons: ['risk_within_limits'] },
    decision_outcome: { selected_strategy_family: 'spot_grid' },
    decision_context: {
      symbol: 'BTC-USDT',
      current_position_qty: 0.2,
      as_of_ts: '2026-03-27T12:00:00Z',
      product_type: 'spot',
    },
  },
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: {
    summary: {
      automatic_selection_enabled: true,
      configured_active_family: 'spot_grid',
      latest_selected_family: 'spot_grid',
      latest_selected_state: 'ready',
      latest_bundle_status: 'single_sleeve',
      latest_portfolio_requested_notional: 100,
      latest_portfolio_approved_notional: 100,
      latest_portfolio_budget_cut_notional: 0,
      latest_approved_sleeve_weights: {},
      latest_selection_reason_codes: ['spot_grid_rebalance_ready'],
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: {
      trade_costs: {
        rate_unit: 'bps',
        rate_example: '8 = 0.08%',
        live_fee_resolution: 'account_schedule_fallback_to_configured',
        spot_maker_fee_bps: 8,
        spot_taker_fee_bps: 10,
        margin_maker_fee_bps: 8,
        margin_taker_fee_bps: 10,
        derivatives_maker_fee_bps: 2,
        derivatives_taker_fee_bps: 5,
        delivery_settlement_fee_bps: 1,
        spot_spread_bps: 1.2,
        spot_slippage_bps: 0.6,
        margin_spread_bps: 1.0,
        margin_slippage_bps: 0.5,
        derivatives_spread_bps: 0.8,
        derivatives_slippage_bps: 0.4,
      },
      directional: {
        product_type: 'spot',
        shorting_runtime_supported: false,
        short_bias_enabled: false,
        entry_allowed_regimes: ['trend', 'breakout'],
        entry_min_signal_edge_bps: 14,
        entry_alpha_min: 0.18,
        entry_confidence_min: 0.63,
        scale_in_min_signal_edge_bps: 16,
        scale_in_alpha_min: 0.22,
        scale_in_confidence_min: 0.68,
        reversal_min_signal_edge_bps: 20,
        reversal_alpha_min: 0.28,
        reversal_confidence_min: 0.72,
      },
      spot_grid: {
        enabled: true,
        anchor_lookback_snapshots: 24,
        band_bps: 150,
        inventory_floor_fraction: 0.15,
        inventory_ceiling_fraction: 1.0,
        rebalance_min_fraction_of_max_qty: 0.08,
        breakout_guard_enabled: true,
      },
      dca: {
        enabled: true,
        interval_seconds: 86400,
        quote_budget_per_cycle: 25,
        max_position_fraction_of_limit: 1.0,
        pullback_only_enabled: false,
        pullback_entry_bps: 40,
      },
    },
    smart_arbitrage_cost_summary: {},
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: false, runtime_supported: false, execution_compatible: false } },
  },
  strategyAttribution: {
    summary: {},
    profitability_by_strategy_sleeve: [],
    sleeve_inventory_summary: [],
  },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesRemovedReference: !html.includes('href="#strategy-reference"')
    && !html.includes('trade_cost_spot_taker_fee_bps')
    && !html.includes('strategy_short_entry_allowed_regimes')
    && !html.includes('smart_arbitrage_quote_budget_per_trade'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesRemovedReference":true', result.stdout)

    def test_strategy_view_organizes_workspace_into_outcome_opportunity_reference_and_history_sections(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  latestDecision: {
    decision_id: 'dec-structure',
    decision_time: '2026-03-27T12:00:00Z',
    baseline_assessment: { regime: 'trend', confidence: 0.67 },
    position_target: {
      strategy_family: 'directional',
      position_intent: 'hold',
      current_position_qty: 0,
      target_position_qty: 0,
      delta_position_qty: 0,
      product_type: 'derivatives',
      margin_mode: 'cross',
    },
    policy_decision: { execution_allowed: true, allow_reasons: ['signal_edge_clear'] },
    risk_decision: { approved: true, approval_reasons: ['risk_within_limits'] },
    decision_outcome: { selected_strategy_family: 'directional' },
    decision_context: {
      symbol: 'BTC-USDT-SWAP',
      current_position_qty: 0,
      as_of_ts: '2026-03-27T12:00:00Z',
      product_type: 'derivatives',
    },
  },
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: {
    summary: {
      automatic_selection_enabled: true,
      configured_active_family: 'directional',
      latest_selected_family: 'directional',
      latest_selected_state: 'ready',
      latest_bundle_status: 'inactive',
      latest_portfolio_requested_notional: 0,
      latest_portfolio_approved_notional: 0,
      latest_portfolio_budget_cut_notional: 0,
      latest_approved_sleeve_weights: {},
      latest_selection_reason_codes: ['signal_edge_clear'],
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: {
      trade_costs: {
        rate_unit: 'bps',
        rate_example: '8 = 0.08%',
        live_fee_resolution: 'account_schedule_fallback_to_configured',
        spot_maker_fee_bps: 8,
        spot_taker_fee_bps: 10,
        margin_maker_fee_bps: 8,
        margin_taker_fee_bps: 10,
        derivatives_maker_fee_bps: 2,
        derivatives_taker_fee_bps: 5,
      },
      directional: {
        short_bias_enabled: true,
      },
      smart_arbitrage: {
        enabled: true,
        pair_definitions: [],
      },
    },
    smart_arbitrage_cost_summary: {},
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: { summary: {} },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hasNav: html.includes('href="#strategy-overview"') && !html.includes('href="#strategy-reference"') && html.includes('href="#strategy-history"'),
  hasOutcomeSection: html.includes('本轮策略到底想做什么') && html.includes('当前候选与自动调度') && html.includes('试盘与自动运行状态'),
  hidesReferenceSection: !html.includes('展开配置与成本参考') && !html.includes('统一交易成本配置') && !html.includes('智能套利配置'),
  hidesCoordinatorDetails: !html.includes('预算快照') && !html.includes('冲突解算') && !html.includes('净额决策') && !html.includes('调度结论'),
  keepsCoordinatorSummary: html.includes('策略家族模式') && html.includes('最近一次选中') && html.includes('最近执行包') && html.includes('组合预算变化'),
  hasCollapsedHistory: html.includes('归因与历史记录') && html.includes('复盘最近为什么赚钱/亏钱、或者核对历史策略输出，直接在这里查看。'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hasNav":true', result.stdout)
        self.assertIn('"hasOutcomeSection":true', result.stdout)
        self.assertIn('"hidesReferenceSection":true', result.stdout)
        self.assertIn('"hidesCoordinatorDetails":true', result.stdout)
        self.assertIn('"keepsCoordinatorSummary":true', result.stdout)
        self.assertIn('"hasCollapsedHistory":true', result.stdout)

    def _obsolete_test_strategy_view_uses_reason_copy_for_blocked_smart_arbitrage_intents_and_multi_pair_targets(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: {
      selected_family: 'smart_arbitrage',
      selected_state: 'opening',
      selected_route_action: 'override_target',
      candidates: [
        {
          family: 'smart_arbitrage',
          state: 'opening',
          route_action: 'override_target',
          urgency: 'medium',
          pair_id: 'multi_pair',
          target_position_qty: null,
          delta_position_qty: null,
          reason_codes: ['smart_arbitrage_positive_basis'],
          metrics: {
            aggregate_candidate: true,
            pair_count_selected: 2,
            selected_pair_summaries: [
              { pair_id: 'btc_usdt_swap', spot_symbol: 'BTC-USDT', derivatives_symbol: 'BTC-USDT-SWAP' },
              { pair_id: 'eth_usdt_swap', spot_symbol: 'ETH-USDT', derivatives_symbol: 'ETH-USDT-SWAP' },
            ],
          },
          legs: [
            { symbol: 'BTC-USDT', product_type: 'spot', side: 'buy', execution_mode: 'spot_carry' },
            { symbol: 'BTC-USDT-SWAP', product_type: 'derivatives', side: 'sell', execution_mode: 'spot_carry' },
          ],
        },
      ],
    },
    configured_parameters: {
      smart_arbitrage: {
        enabled: true,
        pair_definitions: [
          {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            hedge_symbol: 'BTC-USDT-SWAP',
            execution_modes: ['spot_carry'],
            metadata: {
              source: 'pair_registry',
              configuration_error_codes: ['smart_arbitrage_pair_execution_modes_invalid'],
            },
          },
        ],
        pair_registry_error_codes: ['smart_arbitrage_pair_execution_modes_invalid'],
        pair_registry_source: 'coordinator_resolved',
        basis_entry_bps: 40,
        basis_exit_bps: 6,
        estimated_cost_bps: 34,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        cost_model_enabled: true,
        funding_cost_enabled: false,
        borrow_cost_enabled: false,
        negative_basis_mode: 'margin_backed',
        inventory_reservation_enabled: false,
        margin_short_enabled: true,
        margin_short_execution_ready: true,
        margin_short_spot_margin_mode: 'cross',
        margin_short_auto_repay_enabled: false,
        max_concurrent_pairs: 2,
        pair_priority_mode: 'net_edge',
        min_inventory_backed_ratio: 1,
        estimated_fee_bps: 0,
        estimated_slippage_bps: 0,
        estimated_funding_bps: 0,
        estimated_borrow_bps: 0,
      },
    },
    recent_sleeve_intents: [
      {
        strategy_sleeve_id: 'sintent_smart_arbitrage',
        family: 'smart_arbitrage',
        state: 'blocked',
        route_action: 'advisory_only',
        pair_id: 'btc_usdt_swap',
        symbol: 'BTC-USDT-SWAP',
        target_position_qty: 0,
        delta_position_qty: 0,
        automatic_enabled: true,
        budget_multiplier: 1,
        allocator_weight: 1,
        headline: 'Positive basis pair is ready.',
        reason_codes: ['smart_arbitrage_positive_basis', 'smart_arbitrage_spot_carry_not_allowed'],
        legs: [],
      },
    ],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  showsMultiPairTargetCopy: html.includes('按多组套利对分别执行'),
  hidesBlockedReadyHeadline: !html.includes('Positive basis pair is ready.'),
  showsBlockedReasonCopy: html.includes('当前是正基差，但这组配对没有开放正向现货套利模式，系统暂不执行。'),
  showsPairConfigRisk: html.includes('execution_modes 配置非法'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"showsMultiPairTargetCopy":true', result.stdout)
        self.assertIn('"hidesBlockedReadyHeadline":true', result.stdout)
        self.assertIn('"showsBlockedReasonCopy":true', result.stdout)
        self.assertIn('"showsPairConfigRisk":true', result.stdout)

    def test_strategy_view_surfaces_pair_registry_source_labels_for_smart_arbitrage(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const coordinatorHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: {
      smart_arbitrage: {
        enabled: true,
        pair_definitions: [{ pair_id: 'btc_usdt_swap', spot_symbol: 'BTC-USDT', hedge_symbol: 'BTC-USDT-SWAP' }],
        pair_registry_error_codes: ['smart_arbitrage_pair_execution_modes_invalid'],
        pair_registry_source: 'coordinator_resolved',
        basis_entry_bps: 40,
        basis_exit_bps: 6,
        estimated_cost_bps: 20,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        cost_model_enabled: true,
        funding_cost_enabled: false,
        borrow_cost_enabled: false,
        negative_basis_mode: 'advisory_only',
      },
      directional: {},
      trade_costs: {},
    },
    smart_arbitrage_cost_summary: {},
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: { summary: {} },
  trialReviewSummary: { summary: {}, sections: {} },
});

const fallbackHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: {
      smart_arbitrage: {
        enabled: true,
        pair_definitions: [],
        pair_registry_error_codes: ['smart_arbitrage_pair_execution_modes_invalid'],
        pair_registry_source: 'settings_fallback',
        basis_entry_bps: 40,
        basis_exit_bps: 6,
        estimated_cost_bps: 20,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        cost_model_enabled: true,
        funding_cost_enabled: false,
        borrow_cost_enabled: false,
        negative_basis_mode: 'advisory_only',
      },
      directional: {},
      trade_costs: {},
    },
    smart_arbitrage_cost_summary: {},
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: { summary: {} },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hidesCoordinatorSource: !coordinatorHtml.includes('协调器已解析结果'),
  hidesFallbackSource: !fallbackHtml.includes('环境文件默认策略'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesCoordinatorSource":true', result.stdout)
        self.assertIn('"hidesFallbackSource":true', result.stdout)

    def _obsolete_test_strategy_view_localizes_negative_basis_reason_copy_across_advisory_opening_and_blocked_states(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: {
      selected_family: 'smart_arbitrage',
      selected_state: 'opening',
      selected_route_action: 'override_target',
      candidates: [
        {
          family: 'smart_arbitrage',
          state: 'advisory_only',
          state_phase: 'advisory',
          route_action: 'advisory_only',
          urgency: 'low',
          pair_id: 'btc_usdt_swap',
          target_position_qty: 0,
          delta_position_qty: 0,
          headline: 'Negative basis is detected, but reverse-carry auto execution is not available.',
          reason_codes: ['smart_arbitrage_negative_basis', 'smart_arbitrage_spot_short_not_supported'],
          blocking_reasons: ['smart_arbitrage_negative_basis_advisory_only'],
          metrics: {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            derivatives_symbol: 'BTC-USDT-SWAP',
          },
          legs: [],
        },
        {
          family: 'smart_arbitrage',
          state: 'opening',
          state_phase: 'opening',
          route_action: 'override_target',
          urgency: 'high',
          pair_id: 'eth_usdt_swap',
          execution_mode: 'margin_reverse_carry',
          target_position_qty: -1,
          delta_position_qty: -1,
          headline: 'Negative basis reverse carry is ready with margin-backed spot execution.',
          reason_codes: ['smart_arbitrage_negative_basis', 'smart_arbitrage_margin_short_ready'],
          metrics: {
            pair_id: 'eth_usdt_swap',
            spot_symbol: 'ETH-USDT',
            derivatives_symbol: 'ETH-USDT-SWAP',
            execution_mode: 'margin_reverse_carry',
          },
          legs: [
            { symbol: 'ETH-USDT', product_type: 'spot', side: 'sell', execution_mode: 'margin_reverse_carry' },
            { symbol: 'ETH-USDT-SWAP', product_type: 'derivatives', side: 'buy', execution_mode: 'margin_reverse_carry' },
          ],
        },
        {
          family: 'smart_arbitrage',
          state: 'blocked',
          state_phase: 'blocked',
          route_action: 'advisory_only',
          urgency: 'medium',
          pair_id: 'sol_usdt_swap',
          execution_mode: 'inventory_reverse_carry',
          target_position_qty: 0,
          delta_position_qty: 0,
          headline: 'Negative basis is detected, but the configured reverse-carry execution path is blocked.',
          reason_codes: ['smart_arbitrage_negative_basis', 'smart_arbitrage_inventory_backed_spot_balance_unavailable'],
          blocking_reasons: ['smart_arbitrage_inventory_backed_spot_balance_unavailable'],
          metrics: {
            pair_id: 'sol_usdt_swap',
            spot_symbol: 'SOL-USDT',
            derivatives_symbol: 'SOL-USDT-SWAP',
            execution_mode: 'inventory_reverse_carry',
          },
          legs: [],
        },
        {
          family: 'smart_arbitrage',
          state: 'blocked',
          state_phase: 'blocked',
          route_action: 'advisory_only',
          urgency: 'medium',
          pair_id: 'ada_usdt_swap',
          execution_mode: 'margin_reverse_carry',
          target_position_qty: 0,
          delta_position_qty: 0,
          headline: 'Negative basis is detected, but the configured reverse-carry execution path is blocked.',
          reason_codes: ['smart_arbitrage_negative_basis', 'smart_arbitrage_margin_short_disabled'],
          blocking_reasons: ['smart_arbitrage_margin_short_disabled'],
          metrics: {
            pair_id: 'ada_usdt_swap',
            spot_symbol: 'ADA-USDT',
            derivatives_symbol: 'ADA-USDT-SWAP',
            execution_mode: 'margin_reverse_carry',
          },
          legs: [],
        },
      ],
      automation_decisions: [],
    },
    configured_parameters: {
      smart_arbitrage: {
        enabled: true,
        basis_entry_bps: 40,
        basis_exit_bps: 6,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        negative_basis_mode: 'margin_backed',
        margin_short_enabled: true,
        margin_short_execution_ready: true,
            pair_definitions: [
              { pair_id: 'btc_usdt_swap', spot_symbol: 'BTC-USDT', hedge_symbol: 'BTC-USDT-SWAP' },
              { pair_id: 'eth_usdt_swap', spot_symbol: 'ETH-USDT', hedge_symbol: 'ETH-USDT-SWAP' },
              { pair_id: 'sol_usdt_swap', spot_symbol: 'SOL-USDT', hedge_symbol: 'SOL-USDT-SWAP' },
              { pair_id: 'ada_usdt_swap', spot_symbol: 'ADA-USDT', hedge_symbol: 'ADA-USDT-SWAP' },
              { pair_id: 'xrp_usdt_swap', spot_symbol: 'XRP-USDT', hedge_symbol: 'XRP-USDT-SWAP' },
            ],
          },
        },
    recent_sleeve_intents: [
      {
        strategy_sleeve_id: 'sintent_smart_arbitrage',
        family: 'smart_arbitrage',
        state: 'blocked',
        state_phase: 'blocked',
        route_action: 'advisory_only',
        pair_id: 'xrp_usdt_swap',
        symbol: 'XRP-USDT-SWAP',
        target_position_qty: 0,
        delta_position_qty: 0,
        automatic_enabled: true,
        budget_multiplier: 1,
        allocator_weight: 1,
        headline: 'Negative basis is detected, but the configured reverse-carry execution path is blocked.',
        reason_codes: ['smart_arbitrage_negative_basis', 'smart_arbitrage_margin_short_disabled'],
        blocking_reasons: ['smart_arbitrage_margin_short_disabled'],
        legs: [],
      },
    ],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  showsAdvisoryCopy: html.includes('当前是负基差，但自动执行只支持正基差双腿；现货现金模式不能自动做空。'),
  showsOpeningCopy: html.includes('当前是负基差，且保证金融券反套链路已就绪，系统会按借币卖出现货并买入合约的模式生成双腿计划。'),
  showsBlockedInventoryCopy: html.includes('当前识别到负基差，但账户里没有可用于反套的现货余额，不能自动生成库存反套执行计划。'),
  showsBlockedMarginDisabledIntentCopy: html.includes('当前识别到负基差，配置要求走保证金融券反套，但这条执行模式当前未启用。'),
  showsBlockedInventoryLegCopy: html.includes('当前识别到负基差，但账户里没有可用于反套的现货余额。'),
  showsBlockedMarginDisabledLegCopy: html.includes('当前识别到负基差，但保证金融券反套模式当前未启用。'),
  hidesAdvisoryHeadline: !html.includes('Negative basis is detected, but reverse-carry auto execution is not available.'),
  hidesOpeningHeadline: !html.includes('Negative basis reverse carry is ready with margin-backed spot execution.'),
  hidesBlockedHeadline: !html.includes('Negative basis is detected, but the configured reverse-carry execution path is blocked.'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"showsAdvisoryCopy":true', result.stdout)
        self.assertIn('"showsOpeningCopy":true', result.stdout)
        self.assertIn('"showsBlockedInventoryCopy":true', result.stdout)
        self.assertIn('"showsBlockedMarginDisabledIntentCopy":true', result.stdout)
        self.assertIn('"showsBlockedInventoryLegCopy":true', result.stdout)
        self.assertIn('"showsBlockedMarginDisabledLegCopy":true', result.stdout)
        self.assertIn('"hidesAdvisoryHeadline":true', result.stdout)
        self.assertIn('"hidesOpeningHeadline":true', result.stdout)
        self.assertIn('"hidesBlockedHeadline":true', result.stdout)

    def _obsolete_test_strategy_view_compacts_observe_only_smart_arbitrage_copy(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: {
      candidates: [
        {
          family: 'smart_arbitrage',
          state: 'inactive',
          route_action: 'hold_current',
          urgency: 'low',
          target_position_qty: 0,
          delta_position_qty: 0,
          reason_codes: ['smart_arbitrage_basis_below_entry_threshold'],
          pair_id: 'btc_usdt_swap',
          metrics: {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            derivatives_symbol: 'BTC-USDT-SWAP',
            basis_bps: -4.7,
            entry_threshold_bps: 40,
          },
          legs: [],
        },
      ],
      automation_decisions: [],
    },
    configured_parameters: {
      smart_arbitrage: {
        enabled: true,
        pair_definitions: [
          {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            hedge_symbol: 'BTC-USDT-SWAP',
            metadata: { source: 'configured' },
          },
        ],
        basis_exit_bps: 6,
        estimated_cost_bps: 34,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        negative_basis_mode: 'margin_backed',
      },
    },
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const thresholdNeedle = '当前基差 -4.7 个基点，还没有达到入场阈值 40 个基点，系统继续观察。';
console.log(JSON.stringify({
  thresholdCopyCount: (html.split(thresholdNeedle).length - 1),
  hasObserveRoute: html.includes('本轮不入场'),
  hasObserveTarget: html.includes('暂不生成套利双腿'),
  hasNoLegPlanCopy: html.includes('当前还没有生成套利双腿。'),
  avoidsPendingThresholdCopy: !html.includes('BTC-USDT <-> BTC-USDT-SWAP | 基差 -4.7 个基点 | 入场阈值 待确认'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"thresholdCopyCount":2', result.stdout)
        self.assertIn('"hasObserveRoute":true', result.stdout)
        self.assertIn('"hasObserveTarget":true', result.stdout)
        self.assertIn('"hasNoLegPlanCopy":true', result.stdout)
        self.assertIn('"avoidsPendingThresholdCopy":true', result.stdout)

    def _obsolete_test_strategy_view_distinguishes_waiting_exit_vs_kill_switch_blocked_exit_and_short_card_states(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const waitingHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        shorting_runtime_supported: true,
        short_bias_enabled: true,
        runtime_shorting_blockers: [],
        short_entry_allowed_regimes: ['trend'],
        short_entry_min_signal_edge_bps: 12,
        short_entry_alpha_min: 0.18,
        short_entry_confidence_min: 0.58,
        short_scale_in_min_signal_edge_bps: 10,
        short_scale_in_alpha_min: 0.2,
        short_scale_in_confidence_min: 0.6,
        short_reversal_min_signal_edge_bps: 24,
        short_reversal_alpha_min: 0.34,
        short_reversal_confidence_min: 0.8,
      },
      smart_arbitrage: {
        enabled: true,
        basis_entry_bps: 40,
        basis_exit_bps: 6,
        hedge_target_leverage: 3,
        pair_definitions: [{ pair_id: 'btc_usdt_swap', spot_symbol: 'BTC-USDT', hedge_symbol: 'BTC-USDT-SWAP' }],
      },
    },
    latest_snapshot: {
      candidates: [
        {
          family: 'smart_arbitrage',
          state: 'active',
          state_phase: 'active',
          route_action: 'hold_current',
          urgency: 'low',
          pair_id: 'btc_usdt_swap',
          target_position_qty: 0,
          delta_position_qty: 0,
          reason_codes: ['smart_arbitrage_pair_active_waiting_exit'],
          metrics: {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            derivatives_symbol: 'BTC-USDT-SWAP',
          },
          legs: [
            { symbol: 'BTC-USDT', product_type: 'spot', side: 'buy', execution_mode: 'spot_carry' },
            { symbol: 'BTC-USDT-SWAP', product_type: 'derivatives', side: 'sell', execution_mode: 'spot_carry' },
          ],
        },
      ],
      automation_decisions: [],
    },
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  latestDecision: {
    baseline_assessment: {
      direction_bias: 'flat',
      composite_alpha_score: 0.03,
      confidence: 0.61,
    },
    ai_assessment: {
      directional_edge: 0.04,
    },
    position_target: {
      position_intent: 'hold',
      target_exposure_side: 'flat',
      current_position_qty: 0,
      target_position_qty: 0,
      delta_position_qty: 0,
      guardrail_flags: [],
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const blockedHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        shorting_runtime_supported: true,
        short_bias_enabled: true,
        runtime_shorting_blockers: ['kill_switch_active'],
        short_entry_allowed_regimes: ['trend'],
        short_entry_min_signal_edge_bps: 12,
        short_entry_alpha_min: 0.18,
        short_entry_confidence_min: 0.58,
        short_scale_in_min_signal_edge_bps: 10,
        short_scale_in_alpha_min: 0.2,
        short_scale_in_confidence_min: 0.6,
        short_reversal_min_signal_edge_bps: 24,
        short_reversal_alpha_min: 0.34,
        short_reversal_confidence_min: 0.8,
      },
      smart_arbitrage: {
        enabled: true,
        basis_entry_bps: 40,
        basis_exit_bps: 6,
        hedge_target_leverage: 3,
        pair_definitions: [{ pair_id: 'btc_usdt_swap', spot_symbol: 'BTC-USDT', hedge_symbol: 'BTC-USDT-SWAP' }],
      },
    },
    latest_snapshot: {
      candidates: [
        {
          family: 'smart_arbitrage',
          state: 'unwinding',
          state_phase: 'unwinding',
          route_action: 'hold_current',
          urgency: 'high',
          pair_id: 'btc_usdt_swap',
          target_position_qty: 0,
          delta_position_qty: 0,
          reason_codes: ['smart_arbitrage_exit_ready'],
          blocking_reasons: ['kill_switch_active'],
          metrics: {
            pair_id: 'btc_usdt_swap',
            spot_symbol: 'BTC-USDT',
            derivatives_symbol: 'BTC-USDT-SWAP',
          },
          legs: [
            { symbol: 'BTC-USDT', product_type: 'spot', side: 'buy', execution_mode: 'spot_carry' },
            { symbol: 'BTC-USDT-SWAP', product_type: 'derivatives', side: 'sell', execution_mode: 'spot_carry' },
          ],
        },
      ],
      automation_decisions: [],
    },
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: { smart_arbitrage: { enabled: true, runtime_supported: true, execution_compatible: true } },
  },
  latestDecision: {
    baseline_assessment: {
      direction_bias: 'short',
      composite_alpha_score: -0.26,
      confidence: 0.64,
    },
    ai_assessment: {
      directional_edge: -0.12,
    },
    position_target: {
      position_intent: 'reduce_long',
      target_exposure_side: 'flat',
      current_position_qty: 1,
      target_position_qty: 0,
      delta_position_qty: -1,
      guardrail_flags: [],
    },
    policy_decision: { execution_allowed: false, rejection_reasons: ['kill_switch_active'] },
    risk_decision: { approved: false, rejection_reasons: ['kill_switch_active'], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  waitingShowsHoldCopy: waitingHtml.includes('这不是挂单未成'),
  waitingHidesKillSwitchCopy: !waitingHtml.includes('平仓提交被 kill switch 阻断') && !waitingHtml.includes('配置允许，但当前运行线已暂停'),
  blockedShowsKillSwitchCopy: blockedHtml.includes('kill switch') && blockedHtml.includes('交易所里并没有新的退出挂单'),
  shortCardShowsConfigEnabled: waitingHtml.includes('配置允许自动做空'),
  shortCardPrefersNoBearishSignalReason: waitingHtml.includes('当前这轮基础信号并不偏空'),
  shortCardShowsRuntimePauseWhenBlocked: blockedHtml.includes('配置允许，但当前运行线已暂停'),
  shortCardShowsKillSwitchReasonWhenBearish: blockedHtml.includes('当前已经识别到偏空机会，但 kill switch 正在阻断任何新增暴露。'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"waitingShowsHoldCopy":true', result.stdout)
        self.assertIn('"waitingHidesKillSwitchCopy":true', result.stdout)
        self.assertIn('"blockedShowsKillSwitchCopy":true', result.stdout)
        self.assertIn('"shortCardShowsConfigEnabled":true', result.stdout)
        self.assertIn('"shortCardPrefersNoBearishSignalReason":true', result.stdout)
        self.assertIn('"shortCardShowsRuntimePauseWhenBlocked":true', result.stdout)
        self.assertIn('"shortCardShowsKillSwitchReasonWhenBearish":true', result.stdout)

    def test_risk_view_actions_follow_blocker_state(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const healthyHtml = renderRiskView({
  blockerControl: {
    primary_task: {
      kind: 'healthy',
      title: '当前无需人工处理',
      summary: '当前没有新的第一优先级任务。',
      reason: '最新对账和恢复状态都没有给出新的硬阻断或人工复核要求。',
      completion_outcome: '如果仍需再次确认状态，可以手动重新对账（刷新交易所状态）。',
      source_blocker: null,
      secondary_blocker_count: 0,
      actions: [],
    },
    blockers: [],
    secondary_blockers: [],
    next_step_summary: '当前没有待处理的阻断项。',
  },
  systemRecovery: {
    recovery: {
      safe_to_trade: true,
      review_required: false,
      resume_eligible: true,
      halted: false,
      rebaseline_available: false,
      resume_blocked_reasons: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-clean',
      severity: 'CLEAN',
      halt_required: false,
      review_required: false,
      observational_only: false,
      recommended_operator_action: null,
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-03-25T16:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'healthy' },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

const manualHaltHtml = renderRiskView({
  blockerControl: {
    primary_task: {
      kind: 'resume',
      title: '可以直接恢复自动运行',
      summary: '当前没有更高优先级阻断。确认无误后直接恢复自动运行。',
      reason: '系统目前只是处于暂停状态。',
      completion_outcome: '恢复后系统会立刻重新校验当前状态。',
      source_blocker: null,
      secondary_blocker_count: 0,
      actions: [
        {
          action_id: 'resume-system',
          label: '恢复自动运行',
          kind: 'client',
          method: 'CLIENT',
          client_action: 'trigger-resume',
          tone: 'warning',
          enabled: true,
        },
      ],
    },
    blockers: [{ blocker: 'kill_switch_active', title: '系统处于手动暂停状态', actions: [] }],
    secondary_blockers: [],
    next_step_summary: '确认当前状态无误后，直接恢复自动运行。',
  },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: false,
      resume_eligible: true,
      halted: true,
      rebaseline_available: false,
      resume_blocked_reasons: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-clean',
      severity: 'CLEAN',
      halt_required: false,
      review_required: false,
      observational_only: false,
      recommended_operator_action: null,
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-03-25T16:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'paused', halted: true },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

const reviewHtml = renderRiskView({
  blockerControl: {
    primary_task: {
      kind: 'review_reconciliation',
      title: '先确认当前账实状态',
      summary: '先查看最新对账和交易所账单；只有确认当前状态符合预期后，才接受为新基线。',
      reason: '当前仍处于人工确认流程。',
      completion_outcome: '确认完成后系统会重新评估是否能够自动恢复运行。',
      source_blocker: null,
      secondary_blocker_count: 0,
      actions: [
        { action_id: 'inspect-reconciliation:recon-review', label: '查看最新对账', kind: 'client', method: 'CLIENT', client_action: 'inspect-reconciliation', value: 'recon-review', tone: 'ghost', enabled: true },
        { action_id: 'reconcile-now', label: '重新对账（刷新交易所状态）', kind: 'client', method: 'CLIENT', client_action: 'trigger-reconciliation-validate', tone: 'secondary', enabled: true },
        { action_id: 'accept-rebaseline', label: '接受当前状态为新基线', kind: 'client', method: 'CLIENT', client_action: 'trigger-rebaseline', tone: 'warning', enabled: true },
      ],
    },
    blockers: [],
    secondary_blockers: [],
    next_step_summary: '先确认当前账实状态。',
  },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: true,
      rebaseline_available: true,
      resume_blocked_reasons: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-review',
      severity: 'HARD_MISMATCH',
      halt_required: true,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'rebaseline_if_expected',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-03-25T16:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  healthyHasNoPause: !healthyHtml.includes('继续保持暂停'),
  healthyHasNoInspectReconciliation: !healthyHtml.includes('查看最新对账'),
  manualHaltHasResume: manualHaltHtml.includes('恢复自动运行'),
  manualHaltHasNoPause: !manualHaltHtml.includes('继续保持暂停'),
  manualHaltHasNoInspectReconciliation: !manualHaltHtml.includes('查看最新对账'),
  reviewHasInspectReconciliation: reviewHtml.includes('查看最新对账'),
  reviewHasRebaseline: reviewHtml.includes('接受当前状态为新基线'),
  reviewHasNoPause: !reviewHtml.includes('继续保持暂停'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"healthyHasNoPause":true', result.stdout)
        self.assertIn('"healthyHasNoInspectReconciliation":true', result.stdout)
        self.assertIn('"manualHaltHasResume":true', result.stdout)
        self.assertIn('"manualHaltHasNoPause":true', result.stdout)
        self.assertIn('"manualHaltHasNoInspectReconciliation":true', result.stdout)
        self.assertIn('"reviewHasInspectReconciliation":true', result.stdout)
        self.assertIn('"reviewHasRebaseline":true', result.stdout)
        self.assertIn('"reviewHasNoPause":true', result.stdout)

    def test_risk_view_surfaces_exit_execution_review_actions_from_startup_snapshot(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  session: { role: 'admin', authenticated: true },
  authProviders: { auth_enabled: true },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: true,
      rebaseline_available: false,
      resume_blocked_reasons: ['exit_execution_parent_review_required'],
      exit_execution_review_items: [],
      latest_state_snapshot: {
        snapshot_id: 'snapshot_exit_review',
        reconciliation_id: 'recon_exit_review',
        details_json: {
          source: 'startup_exit_execution_review',
          review_items: [
            {
              kind: 'exit_execution_resume_limit_lookup_failed',
              symbol: 'BTC-USDT-SWAP',
              parent_intent_id: 'exit_parent:btc_close',
              aggregate_status: 'PARTIALLY_FILLED',
              reconciliation_state: 'review_required',
              target_exit_quantity: '3',
              aggregated_filled_quantity: '1',
              open_child_working_quantity: '0',
              open_child_unknown_quantity: '0',
              remaining_dispatchable_quantity: '2',
              remaining_unresolved_quantity: '2',
              operator_review_required: true,
              operator_review_reason: 'exit_execution_resume_limit_lookup_failed',
              cancel_requested: false,
              child_order_ids: ['child_exit_a'],
              resume_block_reason: 'resume_limit_lookup_failed',
              dispatch_template_available: true,
              resume_ready: false,
              resume_issue_kind: 'resume_limit_lookup_failed',
              latest_operator_action: {
                action: 'retry_limit_lookup',
                status: 'completed',
                created_at: '2026-04-02T10:05:00Z',
                actor_role: 'admin',
                actor_identity: 'risk-admin',
                summary: '已重试拆单上限查询，但上限仍不可用。',
                remaining_blocker: {
                  code: 'resume_limit_lookup_failed',
                  source: 'resume_block_reason',
                  summary: '交易所单笔上限查询仍未恢复，当前不能继续续派。',
                },
              },
              recent_operator_actions: [
                {
                  action: 'retry_limit_lookup',
                  status: 'completed',
                  created_at: '2026-04-02T10:05:00Z',
                  actor_role: 'admin',
                  actor_identity: 'risk-admin',
                  summary: '已重试拆单上限查询，但上限仍不可用。',
                  remaining_blocker: {
                    code: 'resume_limit_lookup_failed',
                    source: 'resume_block_reason',
                    summary: '交易所单笔上限查询仍未恢复，当前不能继续续派。',
                  },
                },
                {
                  action: 'refresh_exchange_state',
                  status: 'completed',
                  created_at: '2026-04-02T09:58:00Z',
                  actor_role: 'admin',
                  actor_identity: 'risk-admin',
                  summary: '已刷新交易所状态，但当前阻断仍然存在。',
                  remaining_blocker: {
                    code: 'resume_limit_lookup_failed',
                    source: 'resume_block_reason',
                    summary: '交易所单笔上限查询仍未恢复，当前不能继续续派。',
                  },
                },
              ],
              current_blocker: {
                code: 'resume_limit_lookup_failed',
                source: 'resume_block_reason',
                summary: '交易所单笔上限查询仍未恢复，当前不能继续续派。',
              },
              available_operator_actions: ['refresh_exchange_state', 'retry_limit_lookup', 'safe_cancel'],
            },
          ],
        },
      },
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-exit-review',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'review_exit_execution_parent_and_retry_limit_lookup',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  runtime: { operator_auth: { unsafe_write_without_auth: false } },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  hasCard: html.includes('退出任务人工处理'),
  hasStartupSnapshotPill: html.includes('启动快照'),
  hasParentId: html.includes('exit_parent:btc_close'),
  hasRefreshAction: html.includes('trigger-exit-execution-refresh'),
  hasRetryAction: html.includes('trigger-exit-execution-retry-limit-lookup'),
  hasSafeCancelAction: html.includes('trigger-exit-execution-safe-cancel'),
  hasLatestAction: html.includes('最近动作：重试拆单上限查询 / 已完成'),
  hasLatestActionSummary: html.includes('已重试拆单上限查询，但上限仍不可用。'),
  hasRecentActionHistory: html.includes('最近处理记录'),
  hasRecentActionEntry: html.includes('刷新交易所状态 / 已完成') && html.includes('已刷新交易所状态，但当前阻断仍然存在。'),
  hasRemainingBlocker: html.includes('这次动作后仍卡在：') && html.includes('交易所单笔上限查询仍未恢复，当前不能继续续派。'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasCard":true', stdout)
        self.assertIn('"hasStartupSnapshotPill":true', stdout)
        self.assertIn('"hasParentId":true', stdout)
        self.assertIn('"hasRefreshAction":true', stdout)
        self.assertIn('"hasRetryAction":true', stdout)
        self.assertIn('"hasSafeCancelAction":true', stdout)
        self.assertIn('"hasLatestAction":true', stdout)
        self.assertIn('"hasLatestActionSummary":true', stdout)
        self.assertIn('"hasRecentActionHistory":true', stdout)
        self.assertIn('"hasRecentActionEntry":true', stdout)
        self.assertIn('"hasRemainingBlocker":true', stdout)

    def test_risk_view_surfaces_exit_execution_action_timeline(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  session: { role: 'admin', authenticated: true },
  authProviders: { auth_enabled: true },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: true,
      rebaseline_available: false,
      resume_blocked_reasons: ['exit_execution_parent_review_required'],
      exit_execution_review_items: [],
      exit_execution_action_history: [
        {
          parent_intent_id: 'exit_parent:btc_close',
          symbol: 'BTC-USDT-SWAP',
          action: 'refresh_exchange_state',
          status: 'completed',
          aggregate_status: 'REVIEW_REQUIRED',
          created_at: '2026-04-02T10:10:00Z',
          actor_role: 'admin',
          actor_identity: 'risk-admin',
          summary: '已刷新交易所状态，但当前阻断仍未解除。',
          remaining_blocker: {
            code: 'child_unknown_truth_requires_review',
            source: 'operator_review_reason',
            summary: '仍有子订单真相未确认，需要先人工复核。',
          },
        },
      ],
      latest_state_snapshot: null,
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-exit-history',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'review_exit_execution_parent_and_refresh_exchange_state',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  runtime: { operator_auth: { unsafe_write_without_auth: false } },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  hasTimelineCard: html.includes('退出任务处理时间线'),
  hasTimelineEntry: html.includes('exit_parent:btc_close') && html.includes('刷新交易所状态 / 已完成 / 父任务'),
  hasTimelineSummary: html.includes('已刷新交易所状态，但当前阻断仍未解除。'),
  hasTimelineBlocker: html.includes('动作后仍卡在：') && html.includes('仍有子订单真相未确认，需要先人工复核。'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasTimelineCard":true', stdout)
        self.assertIn('"hasTimelineEntry":true', stdout)
        self.assertIn('"hasTimelineSummary":true', stdout)
        self.assertIn('"hasTimelineBlocker":true', stdout)

    def test_risk_view_filters_exit_execution_action_timeline_with_ui_state(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  session: { role: 'admin', authenticated: true },
  authProviders: { auth_enabled: true },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: true,
      rebaseline_available: false,
      resume_blocked_reasons: ['exit_execution_parent_review_required'],
      exit_execution_review_items: [],
      exit_execution_action_history: [
        {
          parent_intent_id: 'exit_parent:btc_close',
          symbol: 'BTC-USDT-SWAP',
          action: 'refresh_exchange_state',
          status: 'completed',
          aggregate_status: 'REVIEW_REQUIRED',
          created_at: '2026-04-02T10:10:00Z',
          actor_role: 'admin',
          actor_identity: 'risk-admin',
          summary: '刷新交易所状态后仍需人工确认。',
          remaining_blocker: {
            code: 'child_unknown_truth_requires_review',
            source: 'operator_review_reason',
            summary: '仍有子订单真相未确认，需要先人工复核。',
          },
        },
        {
          parent_intent_id: 'exit_parent:eth_close',
          symbol: 'ETH-USDT-SWAP',
          action: 'safe_cancel',
          status: 'completed',
          aggregate_status: 'CANCELED',
          created_at: '2026-04-02T10:20:00Z',
          actor_role: 'admin',
          actor_identity: 'ops-two',
          summary: '安全取消已完成。',
          remaining_blocker: null,
        },
      ],
      latest_state_snapshot: null,
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-exit-history-filter',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'review_exit_execution_parent_and_refresh_exchange_state',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  runtime: { operator_auth: { unsafe_write_without_auth: false } },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
}, {
  exitExecutionHistory: {
    action: 'safe_cancel',
    parent: 'eth_close',
    actor: 'ops-two',
  },
});

const btcMatch = html.match(/data-parent-intent-id="exit_parent:btc_close"[\\s\\S]*?data-action-kind="refresh_exchange_state"[^>]*>/);
const ethMatch = html.match(/data-parent-intent-id="exit_parent:eth_close"[\\s\\S]*?data-action-kind="safe_cancel"[^>]*>/);

console.log(JSON.stringify({
  hasActionFilter: html.includes('data-exit-history-filter="action"'),
  hasParentFilterValue: html.includes('value="eth_close"'),
  hasActorFilterValue: html.includes('value="ops-two"'),
  marksFilteredRowHidden: Boolean(btcMatch && btcMatch[0].includes('hidden')),
  keepsMatchingRowVisible: Boolean(ethMatch && !ethMatch[0].includes('hidden')),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasActionFilter":true', stdout)
        self.assertIn('"hasParentFilterValue":true', stdout)
        self.assertIn('"hasActorFilterValue":true', stdout)
        self.assertIn('"marksFilteredRowHidden":true', stdout)
        self.assertIn('"keepsMatchingRowVisible":true', stdout)

    def test_risk_view_surfaces_exit_execution_workspace_with_paging_and_synced_filters(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  session: { role: 'admin', authenticated: true },
  authProviders: { auth_enabled: true },
      systemRecovery: {
        recovery: {
          safe_to_trade: false,
          review_required: true,
          resume_eligible: false,
          halted: true,
          rebaseline_available: false,
          resume_blocked_reasons: ['exit_execution_parent_review_required'],
          exit_execution_review_items: [],
          exit_execution_action_history: [
            {
              parent_intent_id: 'exit_parent:btc_close',
              symbol: 'BTC-USDT-SWAP',
              action: 'safe_cancel',
              status: 'completed',
              aggregate_status: 'CANCELED',
              created_at: '2026-04-02T10:20:00Z',
              actor_role: 'admin',
              actor_identity: 'ops-two',
              summary: '安全取消已完成。',
              remaining_blocker: null,
            },
          ],
          latest_state_snapshot: null,
        },
      },
  exitExecutionActionHistoryPage: {
    actions: [
      {
        parent_intent_id: 'exit_parent:btc_close',
        symbol: 'BTC-USDT-SWAP',
        action: 'safe_cancel',
        status: 'completed',
        aggregate_status: 'CANCELED',
        created_at: '2026-04-02T10:20:00Z',
        actor_role: 'admin',
        actor_identity: 'ops-two',
        summary: '安全取消已完成。',
        remaining_blocker: null,
      },
    ],
    limit: 20,
    offset: 20,
    total_available: 41,
    has_more: true,
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-exit-workspace',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'review_exit_execution_parent_and_refresh_exchange_state',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  runtime: { operator_auth: { unsafe_write_without_auth: false } },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
}, {
  exitExecutionHistory: {
    action: 'safe_cancel',
    parent: 'btc_close',
    actor: 'ops-two',
    windowHours: '24',
    offset: 20,
    limit: 20,
  },
});

console.log(JSON.stringify({
  hasWorkspaceSection: html.includes('退出任务工作区'),
  hasWorkspaceAnchor: html.includes('id="risk-exit-workspace"'),
  hasApplyButton: html.includes('apply-exit-execution-history-workspace'),
  hasPagingButton: html.includes('paginate-exit-execution-history'),
  showsSyncedParentFilter: (html.match(/data-exit-history-filter="parent"/g) || []).length >= 2 && html.includes('value="btc_close"'),
  showsWindowFilter: html.includes('data-exit-history-filter="windowHours"') && html.includes('最近 24 小时'),
  showsPagingSummary: html.includes('当前显示 21 - 21 / 41 条'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasWorkspaceSection":true', stdout)
        self.assertIn('"hasWorkspaceAnchor":true', stdout)
        self.assertIn('"hasApplyButton":true', stdout)
        self.assertIn('"hasPagingButton":true', stdout)
        self.assertIn('"showsSyncedParentFilter":true', stdout)
        self.assertIn('"showsWindowFilter":true', stdout)
        self.assertIn('"showsPagingSummary":true', stdout)

    def test_exit_execution_view_surfaces_workspace_filters_and_paging(self) -> None:
        script = """
import { renderExitExecutionView } from './aats/api/static/modules/views/exit-execution-view.js';

const html = renderExitExecutionView({
  systemRecovery: {
    recovery: {
      review_required: true,
      exit_execution_review_items: [
        {
          symbol: 'BTC-USDT-SWAP',
          parent_intent_id: 'exit_parent:btc_close',
          review_summary: 'child truth 仍待确认',
          remaining_dispatchable_quantity: '0',
          open_child_unknown_quantity: '1',
          review_source: 'runtime',
        },
      ],
    },
  },
  exitExecutionActionHistoryPage: {
    actions: [
      {
        parent_intent_id: 'exit_parent:btc_close',
        symbol: 'BTC-USDT-SWAP',
        action: 'safe_cancel',
        status: 'completed',
        actor_identity: 'ops-two',
        created_at: '2026-04-02T10:00:00Z',
        remaining_blocker: {
          code: 'exit_execution_parent_review_required',
          summary: '仍需继续确认 child 的真实状态',
        },
      },
    ],
    limit: 50,
    offset: 100,
    total_available: 240,
    has_more: true,
  },
}, {
  exitExecutionHistory: {
    action: 'safe_cancel',
    parent: 'exit_parent:btc_close',
    actor: 'ops-two',
    windowHours: '24',
    offset: 100,
    limit: 50,
  },
});

console.log(JSON.stringify({
  hasStandaloneHeading: html.includes('退出任务独立工作台'),
  hasWorkspaceAnchor: html.includes('id="exit-execution-workspace"'),
  showsParentFilter: html.includes('value="exit_parent:btc_close"'),
  showsActorFilter: html.includes('value="ops-two"'),
  showsPagingSummary: html.includes('当前显示 101 - 101 / 240 条'),
  mentionsShareableUrl: html.includes('当前筛选会写入地址栏'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasStandaloneHeading":true', stdout)
        self.assertIn('"hasWorkspaceAnchor":true', stdout)
        self.assertIn('"showsParentFilter":true', stdout)
        self.assertIn('"showsActorFilter":true', stdout)
        self.assertIn('"showsPagingSummary":true', stdout)
        self.assertIn('"mentionsShareableUrl":true', stdout)

    def test_risk_view_prefers_runtime_parent_review_and_disables_admin_only_retry_for_operator(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  session: { role: 'operator', authenticated: true },
  authProviders: { auth_enabled: true },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: true,
      rebaseline_available: false,
      resume_blocked_reasons: ['exit_execution_parent_review_required'],
      exit_execution_review_items: [
        {
          kind: 'exit_execution_parent_review_required',
          symbol: 'BTC-USDT-SWAP',
          parent_intent_id: 'exit_parent:btc_close',
          aggregate_status: 'REVIEW_REQUIRED',
          reconciliation_state: 'review_required',
          target_exit_quantity: '3',
          aggregated_filled_quantity: '1',
          open_child_working_quantity: '0',
          open_child_unknown_quantity: '1',
          remaining_dispatchable_quantity: '0',
          remaining_unresolved_quantity: '2',
          operator_review_required: true,
          operator_review_reason: 'child_unknown_truth_requires_review',
          cancel_requested: false,
          child_order_ids: ['child_exit_a', 'child_exit_b'],
          resume_block_reason: 'review_required',
          dispatch_template_available: true,
          resume_ready: false,
          resume_issue_kind: null,
          available_operator_actions: ['refresh_exchange_state', 'safe_cancel'],
        },
      ],
      latest_state_snapshot: {
        snapshot_id: 'snapshot_exit_review',
        reconciliation_id: 'recon_exit_review',
        details_json: {
          source: 'startup_exit_execution_review',
          review_items: [
            {
              kind: 'exit_execution_resume_limit_lookup_failed',
              symbol: 'BTC-USDT-SWAP',
              parent_intent_id: 'exit_parent:btc_close',
              aggregate_status: 'PARTIALLY_FILLED',
              reconciliation_state: 'review_required',
              target_exit_quantity: '3',
              aggregated_filled_quantity: '1',
              open_child_working_quantity: '0',
              open_child_unknown_quantity: '0',
              remaining_dispatchable_quantity: '2',
              remaining_unresolved_quantity: '2',
              operator_review_required: true,
              operator_review_reason: 'exit_execution_resume_limit_lookup_failed',
              cancel_requested: false,
              child_order_ids: ['child_exit_a'],
              resume_block_reason: 'resume_limit_lookup_failed',
              dispatch_template_available: true,
              resume_ready: false,
              resume_issue_kind: 'resume_limit_lookup_failed',
              available_operator_actions: ['refresh_exchange_state', 'retry_limit_lookup', 'safe_cancel'],
            },
          ],
        },
      },
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-exit-review',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'review_exit_execution_parent_and_refresh_exchange_state',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  runtime: { operator_auth: { unsafe_write_without_auth: false } },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  keepsRuntimeReviewReason: html.includes('退出任务仍有未自动收敛的子订单状态，需要人工确认'),
  hidesSnapshotSpecificReason: !html.includes('退出任务续派被交易所单笔上限查询阻断'),
  showsRefreshAction: html.includes('trigger-exit-execution-refresh'),
  showsSafeCancelAction: html.includes('trigger-exit-execution-safe-cancel'),
  hidesRetryAction: !html.includes('trigger-exit-execution-retry-limit-lookup'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"keepsRuntimeReviewReason":true', stdout)
        self.assertIn('"hidesSnapshotSpecificReason":true', stdout)
        self.assertIn('"showsRefreshAction":true', stdout)
        self.assertIn('"showsSafeCancelAction":true', stdout)
        self.assertIn('"hidesRetryAction":true', stdout)

    def test_risk_view_disables_retry_limit_lookup_for_non_admin_sessions(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  session: { role: 'operator', authenticated: true },
  authProviders: { auth_enabled: true },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: true,
      rebaseline_available: false,
      resume_blocked_reasons: ['exit_execution_resume_limit_lookup_failed'],
      exit_execution_review_items: [
        {
          kind: 'exit_execution_resume_limit_lookup_failed',
          symbol: 'BTC-USDT-SWAP',
          parent_intent_id: 'exit_parent:btc_retry',
          aggregate_status: 'PARTIALLY_FILLED',
          reconciliation_state: 'review_required',
          target_exit_quantity: '3',
          aggregated_filled_quantity: '1',
          open_child_working_quantity: '0',
          open_child_unknown_quantity: '0',
          remaining_dispatchable_quantity: '2',
          remaining_unresolved_quantity: '2',
          operator_review_required: true,
          operator_review_reason: 'exit_execution_resume_limit_lookup_failed',
          cancel_requested: false,
          child_order_ids: ['child_exit_retry'],
          resume_block_reason: 'resume_limit_lookup_failed',
          dispatch_template_available: true,
          resume_ready: false,
          resume_issue_kind: 'resume_limit_lookup_failed',
          available_operator_actions: ['refresh_exchange_state', 'retry_limit_lookup', 'safe_cancel'],
        },
      ],
      latest_state_snapshot: null,
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-exit-review',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      recommended_operator_action: 'review_exit_execution_parent_and_retry_limit_lookup',
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'halted', halted: true },
  runtime: { operator_auth: { unsafe_write_without_auth: false } },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  hasRetryButton: html.includes('重试拆单上限查询'),
  hasRetryDisabled: html.includes('当前动作需要 admin 权限') && html.includes('disabled'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasRetryButton":true', stdout)
        self.assertIn('"hasRetryDisabled":true', stdout)

    def test_risk_view_hides_independent_recovery_snapshot_card(self) -> None:
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: false,
      rebaseline_available: false,
      resume_blocked_reasons: ['independent_transition_invalid'],
      independent_recovery_snapshots: [
        {
          symbol: 'BTC-USDT',
          leg: 'long',
          strategy_sleeve_id: 'sleeve_independent_long_short',
          recovery_posture: 'pending_execution_attempts',
          state_version: 2,
          score_stability_semantics_version: 2,
          active_execution_chain_ids: ['independent:decision_independent_1:long:open'],
          unresolved_attempt_ids: ['attempt_independent_1'],
          recovery_blockers: [],
        },
      ],
      exit_execution_review_items: [],
    },
  },
  reconciliationLatest: { reconciliation: null },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-02T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'warning', halted: false },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  hasIndependentRecoveryCard: html.includes('独立双书恢复快照'),
  hasStateVersionDetail: html.includes('状态机版本') && html.includes('>2<'),
  hasSemanticsVersionDetail: html.includes('稳定性语义版本') && html.includes('independent:decision_independent_1:long:open'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasIndependentRecoveryCard":false', stdout)
        self.assertIn('"hasStateVersionDetail":false', stdout)
        self.assertIn('"hasSemanticsVersionDetail":false', stdout)

    def test_risk_view_surfaces_leg_level_reconciliation_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  systemRecovery: {
    recovery: {
      safe_to_trade: false,
      review_required: true,
      resume_eligible: false,
      halted: false,
      rebaseline_available: true,
      resume_blocked_reasons: ['operator_rebaseline_required'],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-leg-summary',
      severity: 'REVIEW_REQUIRED',
      halt_required: false,
      review_required: true,
      observational_only: false,
      exchange_comparison_enabled: true,
      recommended_operator_action: 'review_and_rebaseline_if_expected',
    },
    mismatch_summary: {
      mismatch_reasons: ['derivatives_leg_position_differs_from_exchange'],
      safety_impacts: ['derivatives_leg_state_requires_review'],
      recommended_operator_action: 'review_and_rebaseline_if_expected',
      leg_mismatch_summary: {
        total_count: 1,
        missing_execution_chain_count: 0,
        items: [
          {
            symbol: 'BTC-USDT-SWAP',
            leg_side: 'short',
            stored_qty: '-0.01',
            exchange_qty: '-0.02',
          },
        ],
      },
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-03-25T16:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {},
  metrics: {},
  health: { runtime_state: 'warning' },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  hasLegMismatchCard: html.includes('持仓腿异常'),
  hasLegMismatchCount: html.includes('1 条'),
  hasShortLegCopy: html.includes('BTC-USDT-SWAP 空头腿：本地 -0.01，交易所 -0.02'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hasLegMismatchCard":true', result.stdout)
        self.assertIn('"hasLegMismatchCount":true', result.stdout)
        self.assertIn('"hasShortLegCopy":true', result.stdout)

    def test_overview_and_decision_drawer_surface_hedge_mode_details(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderOverviewView } from './aats/api/static/modules/views/overview-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const overviewHtml = renderOverviewView({
  mode: { default_symbol: 'BTC-USDT-SWAP' },
  runtime: { symbols: ['BTC-USDT-SWAP'] },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: {
    portfolio: {
      total_equity: 1200,
      realized_pnl: 15,
      unrealized_pnl: 9,
      gross_exposure: 2100,
      net_exposure: 700,
      snapshot_ts: '2026-03-27T14:00:00Z',
      positions: [],
    },
  },
  positions: {
    local_instrument_positions: [
      {
        symbol: 'BTC-USDT-SWAP',
        position_mode: 'long_short_mode',
        margin_mode: 'cross',
        dual_legged: true,
        exposure_side: 'long',
        leg_count: 2,
        long_position_qty: '0.02',
        short_position_qty: '0.01',
        net_position_qty: '0.01',
        gross_position_qty: '0.03',
        long_position_notional: '1400',
        short_position_notional: '700',
        net_position_notional: '700',
        gross_position_notional: '2100',
        unrealized_pnl: '9',
        target_leverage: 3,
      },
    ],
  },
  latestDecision: { decision_id: 'dec-hedge', position_target: { delta_position_qty: '0.01' }, policy_decision: { execution_allowed: true }, risk_decision: { approved: true } },
  executionLatest: {},
  reconciliationLatest: {},
  metrics: {},
  health: { runtime_state: 'healthy', overall_status: 'healthy' },
  uiHints: {},
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-hedge',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.01 },
  position_target: { position_intent: 'scale_in_long', current_position_qty: 0.01, target_position_qty: 0.03 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  hedge_mode_audit: {
    position_mode: {
      configured_derivatives_position_mode: 'hedge',
      required_exchange_position_mode: 'long_short_mode',
      exchange_position_mode: 'long_short_mode',
      exchange_position_mode_matches_configured: true,
      position_mode_match_required: true,
      observed_position_modes: ['long_short_mode'],
      observed_pos_sides: ['long', 'short'],
      mode_change_detected: false,
      contract_mismatch_detected: false,
    },
    leg_orders: {
      total_count: 1,
      open_count: 1,
      reduce_count: 0,
      close_count: 0,
      pos_sides: ['long'],
      symbols: ['BTC-USDT-SWAP'],
      items: [{ symbol: 'BTC-USDT-SWAP', pos_side: 'long', action: 'open', quantity: '0.02', status: 'FILLED', fill_count: 1 }],
    },
    leg_reconciliation: {
      total_count: 1,
      missing_execution_chain_count: 1,
      items: [{ symbol: 'BTC-USDT-SWAP', leg_side: 'short', kind: 'missing_execution_chain', stored_qty: '0', exchange_qty: '-0.01' }],
    },
  },
});

console.log(JSON.stringify({
  overviewHasDualLeg: overviewHtml.includes('双腿并存'),
  overviewHasGross: overviewHtml.includes('毛敞口'),
  overviewHasLongShortBreakdown: overviewHtml.includes('多头 0.02 / 空头 0.01'),
  drawerHasHedgeAudit: drawer.body.includes('对冲模式审计'),
  drawerHasLegOrderAudit: drawer.body.includes('腿级订单审计'),
  drawerHasLegReconciliationAudit: drawer.body.includes('腿级对账审计'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"overviewHasDualLeg":true', result.stdout)
        self.assertIn('"overviewHasGross":true', result.stdout)
        self.assertIn('"overviewHasLongShortBreakdown":true', result.stdout)
        self.assertIn('"drawerHasHedgeAudit":true', result.stdout)
        self.assertIn('"drawerHasLegOrderAudit":true', result.stdout)
        self.assertIn('"drawerHasLegReconciliationAudit":true', result.stdout)

    def _legacy_test_terms_localize_scale_in_position_intents(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { readableState } from './aats/api/static/modules/terms.js';

console.log(JSON.stringify({
  scaleInLong: readableState('scale_in_long'),
  scaleInShort: readableState('scale_in_short'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"scaleInLong":"加多"', result.stdout)
        self.assertIn('"scaleInShort":"加空"', result.stdout)

    def test_terms_localize_scale_in_position_intents(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { readableState } from './aats/api/static/modules/terms.js';

console.log(JSON.stringify({
  scaleInLong: readableState('scale_in_long'),
  scaleInShort: readableState('scale_in_short'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"scaleInLong":"加多"', stdout)
        self.assertIn('"scaleInShort":"加空"', stdout)

    def test_responsive_table_generates_mobile_cards_when_explicit_cards_are_missing(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { responsiveTable } from './aats/api/static/modules/components.js';

const html = responsiveTable(
  ['状态', '说明', '附加信息'],
  [[
    '<div><strong>已就绪</strong><div class="table-meta">最新一轮</div></div>',
    '<div>这里是说明</div>',
    '<span class="signal-pill tone-positive">正常</span>',
  ]],
  '当前没有记录'
);

console.log(JSON.stringify({
  hasTable: html.includes('data-table'),
  hasMobileList: html.includes('mobile-record-list'),
  hasFallbackCard: html.includes('mobile-record-card'),
  keepsFirstColumnAsDetail: html.includes('查看首列信息') && html.includes('状态') && html.includes('已就绪') && html.includes('最新一轮'),
  keepsOtherColumnsAsFields: html.includes('说明') && html.includes('这里是说明') && html.includes('附加信息') && html.includes('正常'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasTable":true', stdout)
        self.assertIn('"hasMobileList":true', stdout)
        self.assertIn('"hasFallbackCard":true', stdout)
        self.assertIn('"keepsFirstColumnAsDetail":true', stdout)
        self.assertIn('"keepsOtherColumnsAsFields":true', stdout)

    def test_trade_display_preserves_reverse_to_direction_specific_labels(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { fillDrawerRows, fillRowTitle, orderDrawerRows, orderRowTitle } from './aats/api/static/modules/trade-display.js';

const reverseLongOrder = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'reverse',
  position_intent: 'reverse_to_long',
  margin_mode: 'cross',
  exposure_side: 'long',
  target_leverage: 5,
  status: 'SUBMITTED',
  requested_qty: 0.01,
  remaining_qty: 0.01,
  filled_qty: 0,
};

const reverseShortFill = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'reverse',
  position_intent: 'reverse_to_short',
  margin_mode: 'cross',
  exposure_side: 'short',
  side: 'sell',
  liquidity_role: 'taker',
  fill_qty: 0.01,
  fill_price: 66500,
  starting_position_qty: 0.01,
  ending_position_qty: -0.01,
  fee_amount: 0,
};

const orderRows = orderDrawerRows(reverseLongOrder);
const fillRows = fillDrawerRows(reverseShortFill);

console.log(JSON.stringify({
  orderTitleIsDirectional: orderRowTitle(reverseLongOrder) === '反手做多',
  fillTitleIsDirectional: fillRowTitle(reverseShortFill) === '反手做空',
  orderDrawerIsDirectional: orderRows[1][1] === '反手做多',
  fillDrawerIsDirectional: fillRows[1][1] === '反手做空',
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"orderTitleIsDirectional":true', stdout)
        self.assertIn('"fillTitleIsDirectional":true', stdout)
        self.assertIn('"orderDrawerIsDirectional":true', stdout)
        self.assertIn('"fillDrawerIsDirectional":true', stdout)

    def test_trade_display_preserves_reduce_and_close_direction_specific_labels(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { fillDrawerRows, fillRowTitle, orderDrawerRows, orderRowTitle } from './aats/api/static/modules/trade-display.js';

const reduceLongOrder = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'reduce',
  position_intent: 'reduce_long',
  margin_mode: 'cross',
  exposure_side: 'long',
  target_leverage: 5,
  status: 'SUBMITTED',
  requested_qty: 0.01,
  remaining_qty: 0.01,
  filled_qty: 0,
};

const closeShortOrder = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'exit',
  position_intent: 'close_short',
  margin_mode: 'cross',
  exposure_side: 'short',
  target_leverage: 5,
  status: 'SUBMITTED',
  requested_qty: 0.01,
  remaining_qty: 0.01,
  filled_qty: 0,
};

const reduceShortFill = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'reduce',
  position_intent: 'reduce_short',
  margin_mode: 'cross',
  exposure_side: 'short',
  side: 'buy',
  liquidity_role: 'taker',
  fill_qty: 0.01,
  fill_price: 66500,
  starting_position_qty: -0.02,
  ending_position_qty: -0.01,
  fee_amount: 0,
};

const closeLongFill = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'exit',
  position_intent: 'close_long',
  margin_mode: 'cross',
  exposure_side: 'long',
  side: 'sell',
  liquidity_role: 'taker',
  fill_qty: 0.01,
  fill_price: 66500,
  starting_position_qty: 0.01,
  ending_position_qty: 0,
  fee_amount: 0,
};

const reduceLongRows = orderDrawerRows(reduceLongOrder);
const closeShortRows = orderDrawerRows(closeShortOrder);
const reduceShortFillRows = fillDrawerRows(reduceShortFill);
const closeLongFillRows = fillDrawerRows(closeLongFill);

console.log(JSON.stringify({
  reduceLongOrderTitle: orderRowTitle(reduceLongOrder),
  reduceLongOrderDrawer: reduceLongRows[1][1],
  closeShortOrderTitle: orderRowTitle(closeShortOrder),
  closeShortOrderDrawer: closeShortRows[1][1],
  reduceShortFillTitle: fillRowTitle(reduceShortFill),
  reduceShortFillDrawer: reduceShortFillRows[1][1],
  closeLongFillTitle: fillRowTitle(closeLongFill),
  closeLongFillDrawer: closeLongFillRows[1][1],
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"reduceLongOrderTitle":"减多"', stdout)
        self.assertIn('"reduceLongOrderDrawer":"减多"', stdout)
        self.assertIn('"closeShortOrderTitle":"平空"', stdout)
        self.assertIn('"closeShortOrderDrawer":"平空"', stdout)
        self.assertIn('"reduceShortFillTitle":"减空"', stdout)
        self.assertIn('"reduceShortFillDrawer":"减空"', stdout)
        self.assertIn('"closeLongFillTitle":"平多"', stdout)
        self.assertIn('"closeLongFillDrawer":"平多"', stdout)

    def test_trade_display_preserves_open_and_scale_in_direction_specific_labels(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { fillDrawerRows, fillRowTitle, orderDrawerRows, orderRowTitle } from './aats/api/static/modules/trade-display.js';

const openLongOrder = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'enter',
  position_intent: 'open_long',
  margin_mode: 'cross',
  exposure_side: 'long',
  target_leverage: 5,
  status: 'SUBMITTED',
  requested_qty: 0.01,
  remaining_qty: 0.01,
  filled_qty: 0,
};

const openShortOrder = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'enter',
  position_intent: 'open_short',
  margin_mode: 'cross',
  exposure_side: 'short',
  target_leverage: 5,
  status: 'SUBMITTED',
  requested_qty: 0.01,
  remaining_qty: 0.01,
  filled_qty: 0,
};

const scaleInLongFill = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'scale_in',
  position_intent: 'scale_in_long',
  margin_mode: 'cross',
  exposure_side: 'long',
  side: 'buy',
  liquidity_role: 'taker',
  fill_qty: 0.01,
  fill_price: 66500,
  starting_position_qty: 0.01,
  ending_position_qty: 0.02,
  fee_amount: 0,
};

const scaleInShortFill = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'scale_in',
  position_intent: 'scale_in_short',
  margin_mode: 'cross',
  exposure_side: 'short',
  side: 'sell',
  liquidity_role: 'taker',
  fill_qty: 0.01,
  fill_price: 66500,
  starting_position_qty: -0.01,
  ending_position_qty: -0.02,
  fee_amount: 0,
};

const openLongRows = orderDrawerRows(openLongOrder);
const openShortRows = orderDrawerRows(openShortOrder);
const scaleInLongFillRows = fillDrawerRows(scaleInLongFill);
const scaleInShortFillRows = fillDrawerRows(scaleInShortFill);

console.log(JSON.stringify({
  openLongOrderTitle: orderRowTitle(openLongOrder),
  openLongOrderDrawer: openLongRows[1][1],
  openShortOrderTitle: orderRowTitle(openShortOrder),
  openShortOrderDrawer: openShortRows[1][1],
  scaleInLongFillTitle: fillRowTitle(scaleInLongFill),
  scaleInLongFillDrawer: scaleInLongFillRows[1][1],
  scaleInShortFillTitle: fillRowTitle(scaleInShortFill),
  scaleInShortFillDrawer: scaleInShortFillRows[1][1],
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"openLongOrderTitle":"开多"', stdout)
        self.assertIn('"openLongOrderDrawer":"开多"', stdout)
        self.assertIn('"openShortOrderTitle":"开空"', stdout)
        self.assertIn('"openShortOrderDrawer":"开空"', stdout)
        self.assertIn('"scaleInLongFillTitle":"加多"', stdout)
        self.assertIn('"scaleInLongFillDrawer":"加多"', stdout)
        self.assertIn('"scaleInShortFillTitle":"加空"', stdout)
        self.assertIn('"scaleInShortFillDrawer":"加空"', stdout)

    def test_execution_view_shows_fee_as_negative_cost_and_positive_rebate(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderExecutionSections } from './aats/api/static/modules/views/execution-view.js';

const sections = renderExecutionSections({
  executionLatest: {},
  recentOrders: { orders: [] },
  recentFills: {
    fills: [
      {
        product_type: 'derivatives',
        symbol: 'BTC-USDT-SWAP',
        execution_action: 'enter',
        position_intent: 'open_long',
        margin_mode: 'cross',
        exposure_side: 'long',
        side: 'buy',
        liquidity_role: 'taker',
        fill_id: 'fill_cost',
        fill_qty: 0.01,
        fill_price: 68494,
        fee_amount: 0.3425,
        realized_pnl: -0.3425,
        ingestion_timestamp: '2026-04-01T10:49:08Z',
      },
      {
        product_type: 'derivatives',
        symbol: 'BTC-USDT-SWAP',
        execution_action: 'enter',
        position_intent: 'open_long',
        margin_mode: 'cross',
        exposure_side: 'long',
        side: 'buy',
        liquidity_role: 'maker',
        fill_id: 'fill_rebate',
        fill_qty: 0.01,
        fill_price: 68494,
        fee_amount: -0.125,
        realized_pnl: 0.125,
        ingestion_timestamp: '2026-04-01T10:50:08Z',
      },
    ],
    total_available: 2,
    has_more: false,
    limit: 8,
  },
  executionErrors: { errors: [] },
  metrics: { current_open_order_count: 0 },
});

const html = sections.executionFills;
console.log(JSON.stringify({
  showsNegativeFeeCost: html.includes('手续费 -0.3425'),
  showsPositiveFeeRebate: html.includes('手续费 +0.125'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"showsNegativeFeeCost":true', stdout)
        self.assertIn('"showsPositiveFeeRebate":true', stdout)

    def test_execution_view_surfaces_lifecycle_diagnostics_with_detail_action(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderExecutionSections } from './aats/api/static/modules/views/execution-view.js';

const sections = renderExecutionSections({
  executionLatest: {},
  recentOrders: { orders: [] },
  recentFills: { fills: [] },
  positionLifecycleAttribution: {
    summary: {
      lifecycle_count: 1,
      combined_net_realized_pnl: -0.33,
      total_fee_quote: 0.22,
      funding_fee_quote: -0.05,
      unassigned_funding_fee_count: 1,
    },
    lifecycles: [
      {
        lifecycle_id: 'lifecycle:btc:1',
        symbol: 'BTC-USDT-SWAP',
        direction: 'long',
        timeframe: '15m',
        family: 'independent',
        combined_net_realized_pnl: -0.33,
        gross_realized_pnl: 0.12,
        total_fee_quote: 0.22,
        entry_fee_quote: 0.10,
        exit_fee_quote: 0.12,
        funding_fee_quote: -0.05,
        hold_seconds: 960,
        entry_fill_count: 1,
        exit_fill_count: 2,
        child_order_count: 3,
        closed_at: '2026-04-15T11:56:00Z',
        exit_reason_breakdown: [
          { reason: 'execution_health_degraded', transition_category: 'execution_guard_exit' },
        ],
      },
    ],
  },
  executionErrors: { errors: [] },
  metrics: { current_open_order_count: 0 },
});

const html = sections.executionLifecycleDiagnostics;
console.log(JSON.stringify({
  showsLifecycleCard: html.includes('仓位生命周期诊断'),
  showsWholePositionCopy: html.includes('整笔仓位口径'),
  showsDetailAction: html.includes('data-action="inspect-lifecycle-attribution"') && html.includes('lifecycle:btc:1'),
  showsExitReasonSummary: html.includes('执行健康度退化') && html.includes('执行守护退出'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"showsLifecycleCard":true', stdout)
        self.assertIn('"showsWholePositionCopy":true', stdout)
        self.assertIn('"showsDetailAction":true', stdout)
        self.assertIn('"showsExitReasonSummary":true', stdout)

    def test_lifecycle_attribution_drawer_consumes_detail_endpoint_payload(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildLifecycleAttributionDrawer } from './aats/api/static/modules/lifecycle-drawer.js';

const drawer = buildLifecycleAttributionDrawer({
  lifecycle_id: 'lifecycle:btc:1',
  summary: {
    lifecycle_id: 'lifecycle:btc:1',
    symbol: 'BTC-USDT-SWAP',
    position_key: 'BTC-USDT-SWAP:long',
    direction: 'long',
    timeframe: '15m',
    family: 'independent',
    status: 'closed',
    opened_at: '2026-04-15T11:40:00Z',
    closed_at: '2026-04-15T11:56:00Z',
    hold_seconds: 960,
    combined_net_realized_pnl: -0.33,
    gross_realized_pnl: 0.12,
    net_realized_pnl: -0.28,
    total_fee_quote: 0.22,
    entry_fee_quote: 0.10,
    exit_fee_quote: 0.12,
    funding_fee_quote: -0.05,
    child_order_count: 3,
    decision_trace_count: 2,
    trace_completeness: 'partial',
    unmatched_actionable_decision_count: 1,
    missing_linked_reference_count: 1,
    child_execution_count: 2,
    gross_to_net_capture_ratio: -2.75,
  },
  trace_completeness: 'partial',
  unmatched_actionable_decision_count: 1,
  missing_linked_reference_count: 1,
  exit_reason_breakdown: [
    { reason: 'failed_thesis', transition_category: 'strategy_exit', decision_count: 1 },
  ],
  exit_intent_breakdown: [
    { intent: 'close_long', fill_count: 2, exit_notional_quote: 203 },
  ],
  decision_trace: [
    {
      timestamp: '2026-04-15T11:50:00Z',
      book_state: 'closing',
      book_action: 'close_failed_thesis',
      close_reason: 'failed_thesis',
      transition_category: 'strategy_exit',
      expected_lifecycle_cost_bps: 9.5,
      expected_lifecycle_net_edge_bps: -1.2,
      execution_health_state: 'degraded',
      fee_drag_ratio: 0.8,
      churn_ratio: 0.7,
      position_qty_before: 2,
      position_qty_after: 1,
      close_notional_quote: 102,
      residual_notional_quote: 101,
      expectancy_scope: 'decision_fallback',
    },
  ],
  candidate_decisions: [
    {
      decision_id: 'decision-shadow',
      timestamp: '2026-04-15T11:49:00Z',
      book_state: 'de_risking',
      book_action: 'de_risk',
      close_reason: 'weak_edge_de_risk',
      transition_category: 'protective_exit',
      expected_cost_bps: 6.0,
      expected_lifecycle_cost_bps: 6.0,
      expected_net_edge_bps: -0.5,
      expected_lifecycle_net_edge_bps: -0.5,
      execution_health_state: 'degraded',
      fee_drag_ratio: 0.25,
      position_qty_before: 1,
      position_qty_after: 0.5,
      timeframe: '15m',
      execution_chain_id: 'independent:decision-shadow:long:de_risk',
    },
  ],
  key_metrics_timeline: [
    {
      timestamp: '2026-04-15T11:50:00Z',
      event_type: 'decision',
      close_reason: 'failed_thesis',
      book_action: 'close_failed_thesis',
      transition_category: 'strategy_exit',
      position_qty_before: 2,
      position_qty_after: 1,
      expected_net_edge_bps: -1.2,
      execution_health_state: 'degraded',
    },
    {
      timestamp: '2026-04-15T11:51:00Z',
      event_type: 'fill',
      fill_bucket: 'exit',
      position_intent: 'close_long',
      fill_notional_quote: 102,
      fee_quote: 0.12,
      realized_pnl_delta: -0.2,
      gross_realized_pnl: -0.08,
    },
    {
      timestamp: '2026-04-15T11:52:00Z',
      event_type: 'funding_fee',
      bill_id: 'bill-funding-1',
      amount: -0.05,
      direction: 'expense',
    },
  ],
  child_fills: [
    {
      fill_id: 'fill-close-1',
      timestamp: '2026-04-15T11:51:00Z',
      fill_bucket: 'exit',
      position_intent: 'close_long',
      execution_action: 'close_long',
      fill_qty: 1,
      fill_price: 102,
      fill_notional_quote: 102,
      fee_quote: 0.12,
      realized_pnl_delta: -0.2,
      gross_realized_pnl: -0.08,
      starting_position_qty: 2,
      ending_position_qty: 1,
    },
  ],
});

console.log(JSON.stringify({
  usesWholePositionCopy: drawer.summary.includes('整笔仓位口径'),
  showsSummaryCard: drawer.body.includes('整笔仓位摘要') && drawer.body.includes('综合净收益'),
  showsDecisionFallbackNote: drawer.body.includes('生命周期成本口径暂回退到决策级估计'),
  showsExitTrace: drawer.body.includes('退出链诊断') && drawer.body.includes('thesis失效') && drawer.body.includes('策略性退出'),
  showsTraceCompletenessWarning: drawer.body.includes('退出链证据不完整') && drawer.body.includes('弱关联候选决策'),
  showsMissingLinkedWarning: drawer.body.includes('缺失强关联') && drawer.body.includes('未找到对应审计/运行态证据'),
  showsCandidateDiagnostics: drawer.body.includes('预期净边际') && drawer.body.includes('生命周期成本') && drawer.body.includes('手续费拖累'),
  usesChineseLifecycleLabels: drawer.body.includes('个子委托') && drawer.body.includes('子执行尝试') && drawer.body.includes('资金费'),
  showsChildFillSection: drawer.body.includes('子成交明细') && drawer.body.includes('退出成交'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"usesWholePositionCopy":true', stdout)
        self.assertIn('"showsSummaryCard":true', stdout)
        self.assertIn('"showsDecisionFallbackNote":true', stdout)
        self.assertIn('"showsExitTrace":true', stdout)
        self.assertIn('"showsTraceCompletenessWarning":true', stdout)
        self.assertIn('"showsMissingLinkedWarning":true', stdout)
        self.assertIn('"showsCandidateDiagnostics":true', stdout)
        self.assertIn('"usesChineseLifecycleLabels":true', stdout)
        self.assertIn('"showsChildFillSection":true', stdout)

    def test_trade_display_and_order_drawer_show_fee_cost_as_negative_and_rebate_as_positive(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildOrderDrawer } from './aats/api/static/modules/detail-drawers.js';
import { fillDrawerRows, fillImpactMeta } from './aats/api/static/modules/trade-display.js';

const feeCostFill = {
  product_type: 'derivatives',
  symbol: 'BTC-USDT-SWAP',
  execution_action: 'enter',
  position_intent: 'open_long',
  margin_mode: 'cross',
  exposure_side: 'long',
  side: 'buy',
  liquidity_role: 'taker',
  fill_id: 'fill_cost',
  fill_qty: 0.01,
  fill_price: 68494,
  fee_amount: 0.3425,
  fee_currency: 'USDT',
  realized_pnl: -0.3425,
};

const feeRebateFill = {
  ...feeCostFill,
  fill_id: 'fill_rebate',
  liquidity_role: 'maker',
  fee_amount: -0.125,
  realized_pnl: 0.125,
};

const fillDrawer = fillDrawerRows(feeCostFill);
const orderDrawerHtml = buildOrderDrawer({
  order: {
    client_order_id: 'ord-1',
    product_type: 'derivatives',
    symbol: 'BTC-USDT-SWAP',
    margin_mode: 'cross',
    exposure_side: 'long',
    status: 'FILLED',
  },
  fills: [feeCostFill, feeRebateFill],
}).body;

console.log(JSON.stringify({
  impactMetaShowsNegativeFeeCost: fillImpactMeta(feeCostFill).includes('手续费 -0.3425 USDT'),
  fillDrawerShowsNegativeFeeCost: fillDrawer[5][2] === '手续费 -0.3425 USDT',
  orderDrawerShowsNegativeFeeCost: orderDrawerHtml.includes('手续费 -0.3425 USDT'),
  orderDrawerShowsPositiveFeeRebate: orderDrawerHtml.includes('手续费 +0.125 USDT'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"impactMetaShowsNegativeFeeCost":true', stdout)
        self.assertIn('"fillDrawerShowsNegativeFeeCost":true', stdout)
        self.assertIn('"orderDrawerShowsNegativeFeeCost":true', stdout)
        self.assertIn('"orderDrawerShowsPositiveFeeRebate":true', stdout)

    def test_strategy_attribution_and_risk_view_preserve_fee_and_funding_sign_semantics(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const strategyHtml = renderStrategyView({
  latestDecision: {},
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    latest_bundle: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    latest_applied_target: {},
    latest_allocation_decision: {},
    configured_parameters: { directional: {}, smart_arbitrage: {} },
    family_enablement: {},
    smart_arbitrage_cost_summary: {},
  },
  strategyAttribution: {
    summary: {
      sleeve_pnl_record_count: 2,
      combined_net_realized_pnl: 1.25,
      funding_fee_net_pnl: -0.6,
      protected_fill_count: 0,
      unprotected_fill_count: 2,
    },
    profitability_by_strategy_sleeve: [
      {
        strategy_sleeve_id: 'sleeve_cost',
        families: ['directional'],
        combined_net_realized_pnl: 1.2,
        realized_pnl: 1.5,
        funding_fee_amount: -0.3,
        fee_amount: 0.3425,
        inventory_move_qty: 0.01,
        record_count: 1,
      },
      {
        strategy_sleeve_id: 'sleeve_rebate',
        families: ['directional'],
        combined_net_realized_pnl: 0.5,
        realized_pnl: 0.4,
        funding_fee_amount: 0.2,
        fee_amount: -0.125,
        inventory_move_qty: 0,
        record_count: 1,
      },
    ],
    sleeve_inventory_summary: [],
    profitability_by_attribution_type: [],
    profitability_by_strategy_bundle: [],
  },
  trialReviewSummary: { summary: {}, recommendation: {}, sections: {} },
  trialReviewHistory: {},
  forwardValidation: {},
  scalingReadiness: {},
});

const riskHtml = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  systemRecovery: {
    recovery: {
      safe_to_trade: true,
      review_required: false,
      resume_eligible: true,
      halted: false,
      rebaseline_available: false,
      resume_blocked_reasons: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-clean',
      severity: 'CLEAN',
      halt_required: false,
      review_required: false,
      observational_only: false,
      recommended_operator_action: null,
    },
    exchange_bills_summary: {},
  },
  guardedLiveRunPacket: {
    status: 'healthy',
    summary: 'ok',
    operator_actions: [],
    summary_metrics: {
      combined_net_realized_pnl: 12.5,
      funding_fee_net_pnl: -4.2,
      execution_blocker_count: 0,
      current_initial_margin_usage_fraction: 0.12,
      nearest_liquidation_gap_ratio: 0.35,
      open_position_count: 1,
    },
  },
  trialGuard: {
    status: 'healthy',
    summary: 'ok',
    fill_count: 10,
    min_closed_fills: 5,
    daily_combined_net_realized: 8.4,
    daily_trading_net_realized: 10.1,
    daily_funding_fee_net: -1.7,
    consecutive_losses: 0,
    breaches: [],
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-04-01T10:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: { healthy: true, recent_validations: [] },
  metrics: {},
  health: { runtime_state: 'healthy' },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  strategyShowsNegativeFeeCost: strategyHtml.includes('费用 -0.3425'),
  strategyShowsPositiveFeeRebate: strategyHtml.includes('费用 +0.125'),
  strategyKeepsFundingDirection: strategyHtml.includes('-0.3') && strategyHtml.includes('+0.2'),
  riskShowsSignedGuardedLiveNet: riskHtml.includes('+12.5') && riskHtml.includes('资金费 -4.2'),
  riskShowsSignedTrialGuardNet: riskHtml.includes('+8.4') && riskHtml.includes('交易净收益 +10.1') && riskHtml.includes('资金费 -1.7'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsNegativeFeeCost":true', stdout)
        self.assertIn('"strategyShowsPositiveFeeRebate":true', stdout)
        self.assertIn('"strategyKeepsFundingDirection":true', stdout)
        self.assertIn('"riskShowsSignedGuardedLiveNet":true', stdout)
        self.assertIn('"riskShowsSignedTrialGuardNet":true', stdout)

    def test_strategy_view_surfaces_lifecycle_diagnostic_entry_in_attribution_area(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategyView({
  latestDecision: {},
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    latest_bundle: {},
    recent_execution_bundles: [],
    recent_sleeve_intents: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    latest_applied_target: {},
    latest_allocation_decision: {},
    configured_parameters: { directional: {}, smart_arbitrage: {} },
    family_enablement: {},
    smart_arbitrage_cost_summary: {},
  },
  strategyAttribution: {
    summary: {
      sleeve_pnl_record_count: 2,
      combined_net_realized_pnl: 1.25,
      funding_fee_net_pnl: -0.6,
      protected_fill_count: 0,
      unprotected_fill_count: 2,
    },
    profitability_by_strategy_sleeve: [],
    sleeve_inventory_summary: [],
    profitability_by_attribution_type: [],
    profitability_by_strategy_bundle: [],
  },
  positionLifecycleAttribution: {
    summary: {
      lifecycle_count: 1,
      combined_net_realized_pnl: -0.33,
    },
    lifecycles: [
      {
        lifecycle_id: 'lifecycle:btc:strategy',
        symbol: 'BTC-USDT-SWAP',
        direction: 'long',
        timeframe: '15m',
        family: 'independent',
        combined_net_realized_pnl: -0.33,
        gross_realized_pnl: 0.12,
        total_fee_quote: 0.22,
        entry_fee_quote: 0.10,
        exit_fee_quote: 0.12,
        funding_fee_quote: -0.05,
        hold_seconds: 960,
        entry_fill_count: 1,
        exit_fill_count: 2,
        child_order_count: 3,
        closed_at: '2026-04-15T11:56:00Z',
        exit_reason_breakdown: [
          { reason: 'execution_health_degraded', transition_category: 'execution_guard_exit' },
        ],
      },
    ],
  },
  trialReviewSummary: { summary: {}, recommendation: {}, sections: {} },
  trialReviewHistory: {},
  forwardValidation: {},
  scalingReadiness: {},
});

console.log(JSON.stringify({
  showsLifecycleArea: html.includes('整笔仓位复盘'),
  showsWholePositionCopy: html.includes('整笔仓位口径'),
  showsDetailAction: html.includes('data-action="inspect-lifecycle-attribution"') && html.includes('lifecycle:btc:strategy'),
  showsExitReasonSummary: html.includes('执行健康度退化') && html.includes('执行守护退出'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"showsLifecycleArea":true', stdout)
        self.assertIn('"showsWholePositionCopy":true', stdout)
        self.assertIn('"showsDetailAction":true', stdout)
        self.assertIn('"showsExitReasonSummary":true', stdout)

    def test_family_cutover_ui_prefers_family_execution_summary_over_net_position_fields(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderHomeView } from './aats/api/static/modules/views/home-view.js';
import { renderOverviewView } from './aats/api/static/modules/views/overview-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const familyExecutionSummary = {
  summary_mode: 'multi_leg',
  family: 'independent',
  route_action: 'override_target',
  family_action: 'open_independent_book',
  leg_count: 2,
  position_intents: ['open_long', 'open_short'],
  directions: ['long', 'short'],
  leg_actions: ['open'],
  execution_modes: ['independent_long_book', 'independent_short_book'],
  parent_target_signal: 'flat',
  parent_current_signal: 'long',
  parent_effective_signal: 'long',
  signal_source: 'inventory',
  parent_lifecycle_state: 'inventory_only',
  parent_target_active: false,
  parent_inventory_active: true,
};

const latestDecision = {
  decision_id: 'dec-cutover',
  decision_time: '2026-03-30T12:00:00Z',
  decision_context: { as_of_ts: '2026-03-30T12:00:00Z', symbol: 'BTC-USDT-SWAP' },
  position_target: {
    position_intent: 'hold',
    target_exposure_side: 'flat',
    current_position_qty: 0,
    target_position_qty: 0,
    delta_position_qty: 0,
    family_execution_summary: familyExecutionSummary,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: { directional: {} },
    latest_applied_target: latestDecision.position_target,
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision,
});

const homeHtml = renderHomeView({
  latestDecision,
  executionLatest: {},
  reconciliationLatest: {},
  health: { halted: false },
  mode: { execution_route: 'derivatives_live' },
  runtime: { environment_capabilities: { exchange_submission_target: 'derivatives_live' } },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0 } },
  accountState: { connected: true, fresh: true, ready: true, blockers: [] },
  metrics: { current_open_order_count: 0 },
  uiHints: {},
});

const overviewHtml = renderOverviewView({
  latestDecision,
  executionLatest: {},
  reconciliationLatest: {},
  health: { runtime_state: 'healthy', overall_status: 'healthy' },
  mode: { default_symbol: 'BTC-USDT-SWAP' },
  runtime: { symbols: ['BTC-USDT-SWAP'] },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, realized_pnl: 0, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0, positions: [] } },
  positions: { local_instrument_positions: [] },
  metrics: {},
  uiHints: {},
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-cutover',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: latestDecision.position_target,
  decision_outcome: {
    final_action: 'enter',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
  },
  ai_decision_audit: {
    final_action: 'enter',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  strategyUsesFamilySummary: strategyHtml.includes('2 条腿联动') && strategyHtml.includes('开多') && strategyHtml.includes('开空'),
  homeUsesFamilySummary: homeHtml.includes('2 条腿联动') && homeHtml.includes('开多') && homeHtml.includes('开空'),
  overviewUsesFamilySummary: overviewHtml.includes('2 条腿联动') && overviewHtml.includes('开多') && overviewHtml.includes('开空'),
  drawerUsesFamilySummary: drawer.body.includes('2 条腿联动') && drawer.body.includes('开多') && drawer.body.includes('开空') && drawer.body.includes('双向'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyUsesFamilySummary":true', stdout)
        self.assertIn('"homeUsesFamilySummary":true', stdout)
        self.assertIn('"overviewUsesFamilySummary":true', stdout)
        self.assertIn('"drawerUsesFamilySummary":true', stdout)

    def test_strategy_and_overview_surface_terminal_no_fill_explanation(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderOverviewView } from './aats/api/static/modules/views/overview-view.js';

const latestDecision = {
  decision_id: 'decision-terminal-no-fill',
  decision_time: '2026-04-27T09:49:53Z',
  decision_context: { as_of_ts: '2026-04-27T09:49:53Z', symbol: 'BTC-USDT-SWAP' },
  baseline_assessment: { regime: 'trend' },
  position_target: {
    position_intent: 'open_short',
    target_exposure_side: 'short',
    current_position_qty: 0.0015,
    target_position_qty: -0.0033,
    delta_position_qty: -0.0048,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
};

const executionLatest = {
  latest_order: {
    client_order_id: 'client-terminal-no-fill',
    decision_id: 'decision-terminal-no-fill',
    status: 'BLOCKED',
    state: 'BLOCKED',
    requested_qty: 0.0048,
    created_at: '2026-04-27T09:49:54Z',
    last_update_ts: '2026-04-27T09:49:54Z',
  },
  terminal_no_fill_explanation: {
    classification: 'terminal_order_surface_without_fill',
    reason: 'terminal_order_blocked_before_fill',
    decision_id: 'decision-terminal-no-fill',
    latest_order_updated_at: '2026-04-27T09:49:54Z',
    terminal_states: ['BLOCKED'],
    terminal_position_intents: ['close_long', 'open_short'],
    terminal_execution_styles: ['semantic_dup_snapshot_blocked', 'taker'],
    execution_order_count: 2,
    terminal_execution_order_count: 2,
    fill_surface_present: false,
  },
};

const strategyHtml = renderStrategyView({
  latestDecision,
  executionLatest,
  recentDecisions: { decisions: [] },
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: { directional: {} },
    latest_applied_target: latestDecision.position_target,
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
});

const overviewHtml = renderOverviewView({
  latestDecision,
  executionLatest,
  health: { runtime_state: 'healthy', overall_status: 'healthy' },
  mode: { default_symbol: 'BTC-USDT-SWAP' },
  runtime: { symbols: ['BTC-USDT-SWAP'] },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, realized_pnl: 0, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0, positions: [] } },
  positions: { local_instrument_positions: [] },
  metrics: { fill_count: 73, current_open_order_count: 0 },
  aiRuntime: {},
  strategyRuntime: { summary: {}, entry_execution_guard: {} },
  reconciliationLatest: {},
  uiHints: {},
});

console.log(JSON.stringify({
  strategyExplainsNoFill:
    strategyHtml.includes('这次为什么没有成交')
    && strategyHtml.includes('下单前被阻断')
    && strategyHtml.includes('平多 / 开空')
    && strategyHtml.includes('无成交终局'),
  overviewExplainsNoFill:
    overviewHtml.includes('无成交终局')
    && overviewHtml.includes('终端无成交解释')
    && overviewHtml.includes('下单前被阻断')
    && overviewHtml.includes('平多 / 开空'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyExplainsNoFill":true', stdout)
        self.assertIn('"overviewExplainsNoFill":true', stdout)

    def test_home_and_overview_views_surface_parent_signal_summary_from_family_execution_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderHomeView } from './aats/api/static/modules/views/home-view.js';
import { renderOverviewView } from './aats/api/static/modules/views/overview-view.js';

const familyExecutionSummary = {
  summary_mode: 'single_leg',
  family: 'protective',
  route_action: 'override_target',
  family_action: 'close_protection_leg',
  leg_count: 1,
  position_intents: ['close_short'],
  directions: ['short'],
  leg_actions: ['close'],
  execution_modes: ['protective_overlay'],
  parent_target_signal: 'flat',
  parent_current_signal: 'long',
  parent_effective_signal: 'long',
  signal_source: 'inventory',
  parent_lifecycle_state: 'inventory_only',
  parent_target_active: false,
  parent_inventory_active: true,
};

const latestDecision = {
  decision_id: 'dec-parent-signal-home-overview',
  decision_time: '2026-03-30T12:00:00Z',
  decision_context: { as_of_ts: '2026-03-30T12:00:00Z', symbol: 'BTC-USDT-SWAP' },
  position_target: {
    position_intent: 'close_short',
    target_exposure_side: 'flat',
    current_position_qty: 0.03,
    target_position_qty: 0,
    delta_position_qty: -0.03,
    family_execution_summary: familyExecutionSummary,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
};

const homeHtml = renderHomeView({
  latestDecision,
  executionLatest: {},
  reconciliationLatest: {},
  health: { halted: false },
  mode: { execution_route: 'derivatives_live' },
  runtime: { environment_capabilities: { exchange_submission_target: 'derivatives_live' } },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0 } },
  accountState: { connected: true, fresh: true, ready: true, blockers: [] },
  metrics: { current_open_order_count: 0 },
  uiHints: {},
});

const overviewHtml = renderOverviewView({
  latestDecision,
  executionLatest: {},
  reconciliationLatest: {},
  health: { runtime_state: 'healthy', overall_status: 'healthy' },
  mode: { default_symbol: 'BTC-USDT-SWAP' },
  runtime: { symbols: ['BTC-USDT-SWAP'] },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, realized_pnl: 0, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0, positions: [] } },
  positions: { local_instrument_positions: [] },
  metrics: {},
  uiHints: {},
});

console.log(JSON.stringify({
  homeShowsParentSignals:
    homeHtml.includes('这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效'),
  overviewShowsParentSignals:
    overviewHtml.includes('这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"homeShowsParentSignals":true', stdout)
        self.assertIn('"overviewShowsParentSignals":true', stdout)

    def _obsolete_test_independent_expectancy_summary_surfaces_in_runtime_and_decision_ui(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const bookExpectancySummary = {
  source: 'independent_book',
  books: [
    {
      leg: 'long',
      expected_gross_edge_bps: 18.0,
      expected_signal_edge_bps: 18.0,
      expected_slippage_bps: 1.5,
      expected_cost_bps: 6.0,
      expected_net_edge_bps: 12.0,
    },
    {
      leg: 'short',
      expected_gross_edge_bps: 4.0,
      expected_signal_edge_bps: 4.0,
      expected_slippage_bps: 1.5,
      expected_cost_bps: 6.0,
      expected_net_edge_bps: -2.0,
    },
  ],
};

const familyExecutionSummary = {
  summary_mode: 'multi_leg',
  family: 'independent',
  route_action: 'override_target',
  family_action: 'open_independent_book',
  leg_count: 2,
  position_intents: ['open_long', 'open_short'],
  directions: ['long', 'short'],
  leg_actions: ['open'],
  execution_modes: ['independent_long_book', 'independent_short_book'],
  book_expectancy_summary: bookExpectancySummary,
};

const latestDecision = {
  decision_id: 'dec-independent-expectancy',
  decision_time: '2026-03-30T12:00:00Z',
  decision_context: { as_of_ts: '2026-03-30T12:00:00Z', symbol: 'BTC-USDT-SWAP' },
  baseline_assessment: { direction_bias: 'long', confidence: 0.84, composite_alpha_score: 0.32 },
  ai_assessment: { directional_edge: 0.2 },
  position_target: {
    position_intent: 'hold',
    target_exposure_side: 'flat',
    current_position_qty: 0,
    target_position_qty: 0,
    delta_position_qty: 0,
    family_execution_summary: familyExecutionSummary,
  },
  policy_decision: { execution_allowed: true, blocker_reasons: [], allow_reasons: [] },
  risk_decision: { approved: true, rejection_reasons: [], approval_reasons: [] },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: {
      candidates: [
        {
          family: 'independent',
          state: 'opening',
          route_action: 'override_target',
          family_action: 'open_independent_book',
          urgency: 'medium',
          target_position_qty: 0,
          delta_position_qty: 0,
          headline: 'Independent family candidate',
          reason_codes: ['independent_long_book_signal_above_entry_threshold'],
          metrics: {},
          book_expectancy_summary: bookExpectancySummary,
          legs: [
            { symbol: 'BTC-USDT-SWAP', product_type: 'derivatives', side: 'buy', execution_mode: 'independent_long_book' },
            { symbol: 'BTC-USDT-SWAP', product_type: 'derivatives', side: 'sell', execution_mode: 'independent_short_book' },
          ],
        },
      ],
      automation_decisions: [],
    },
    configured_parameters: { directional: {} },
    latest_applied_target: latestDecision.position_target,
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision,
});

const drawer = buildDecisionDrawer({
  decision_id: latestDecision.decision_id,
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: latestDecision.position_target,
  decision_outcome: {
    final_action: 'enter',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
  },
  ai_decision_audit: {
    configured_mode: 'baseline_only',
    assessment_operating_mode: 'baseline_only',
    final_action: 'enter',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
    market_snapshot_fresh: true,
    account_snapshot_fresh: true,
    safe_to_trade: true,
    recent_fee_drag_ratio: 0.03,
    recent_churn_ratio: 0.02,
    recent_low_edge_trade_streak: 1,
    current_open_order_count: 0,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  strategyShowsCandidateExpectancy:
    strategyHtml.includes('多书 毛/成本/净 18.00/6.00/12.00 基点')
    && strategyHtml.includes('空书 毛/成本/净 4.00/6.00/-2.00 基点'),
  drawerShowsExpectancy:
    drawer.body.includes('每条书预期边际')
    && drawer.body.includes('多书 毛/成本/净 18.00/6.00/12.00 基点')
    && drawer.body.includes('空书 毛/成本/净 4.00/6.00/-2.00 基点'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsCandidateExpectancy":true', stdout)
        self.assertIn('"drawerShowsExpectancy":true', stdout)

    def _obsolete_test_independent_expected_vs_realized_summary_surfaces_in_runtime_and_decision_ui(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const diagnostics = {
  family: 'independent',
  sample_count: 4,
  expected_sample_count: 4,
  realized_sample_count: 3,
  overlap_sample_count: 3,
  entry_count: 2,
  scale_in_count: 1,
  close_count: 1,
  de_risk_count: 0,
  weak_edge_entry_count: 1,
  avg_expected_net_edge_bps: 8.5,
  avg_realized_gross_bps: 7.2,
  avg_realized_fee_bps: 1.1,
  avg_realized_slippage_bps: 0.8,
  avg_realized_net_bps: 5.3,
  fee_drag_ratio: 0.24,
  churn_ratio: 0.25,
  passive_first_usage_ratio: 0.6667,
  expected_realized_net_gap_bps: -3.2,
  expected_realized_correlation: 0.72,
  close_reason_distribution: [{ reason: 'stale_thesis', count: 1 }],
  book_breakdown: [
    { leg: 'long', sample_count: 2, avg_expected_net_edge_bps: 9.0, avg_realized_net_bps: 6.0 },
    { leg: 'short', sample_count: 2, avg_expected_net_edge_bps: 8.0, avg_realized_net_bps: 4.6 },
  ],
};

const familyExecutionSummary = {
  summary_mode: 'multi_leg',
  family: 'independent',
  route_action: 'override_target',
  family_action: 'rebalance_independent_books',
  leg_count: 2,
  position_intents: ['open_long', 'close_short'],
  directions: ['long', 'short'],
  leg_actions: ['open', 'close'],
  execution_modes: ['independent_long_book', 'independent_short_book'],
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    independent_expected_vs_realized_summary: diagnostics,
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: {
      directional: {
        hedge_independent_emit_book_level_metrics: true,
        hedge_independent_emit_expected_vs_realized_metrics: true,
        hedge_independent_emit_close_reason_metrics: true,
        hedge_independent_emit_execution_policy_metrics: true,
      },
    },
    latest_applied_target: { family_execution_summary: familyExecutionSummary },
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    decision_id: 'dec-independent-evr',
    decision_time: '2026-03-30T12:00:00Z',
    decision_context: { as_of_ts: '2026-03-30T12:00:00Z', symbol: 'BTC-USDT-SWAP' },
    baseline_assessment: {},
    ai_assessment: {},
    position_target: { family_execution_summary: familyExecutionSummary },
    policy_decision: { execution_allowed: true, blocker_reasons: [], allow_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], approval_reasons: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-independent-evr',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.01 },
  position_target: { family_execution_summary: familyExecutionSummary },
  decision_outcome: {
    final_action: 'exit',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
  },
  ai_decision_audit: {
    configured_mode: 'baseline_only',
    assessment_operating_mode: 'baseline_only',
    final_action: 'exit',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
    independent_expected_vs_realized_summary: diagnostics,
    market_snapshot_fresh: true,
    account_snapshot_fresh: true,
    safe_to_trade: true,
    recent_fee_drag_ratio: 0.03,
    recent_churn_ratio: 0.02,
    recent_low_edge_trade_streak: 1,
    current_open_order_count: 0,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  strategyShowsDiagnostics:
    strategyHtml.includes('独立双书预期 vs 已实现')
    && strategyHtml.includes('样本 4（预期 4 / 已实现 3 / 重合 3）')
    && strategyHtml.includes('预期净边际 8.50 基点')
    && strategyHtml.includes('已实现净收益 5.30 基点')
    && strategyHtml.includes('被动优先 66.7%')
    && strategyHtml.includes('退出原因 thesis过期 1 次'),
  drawerShowsDiagnostics:
    drawer.body.includes('预期 vs 已实现')
    && drawer.body.includes('样本 4（预期 4 / 已实现 3 / 重合 3）')
    && drawer.body.includes('预期偏差 -3.20 基点')
    && drawer.body.includes('多书 样本 2 / 预期 9.00 / 已实现 6.00 基点')
    && drawer.body.includes('空书 样本 2 / 预期 8.00 / 已实现 4.60 基点'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsDiagnostics":true', stdout)
        self.assertIn('"drawerShowsDiagnostics":true', stdout)

    def test_decision_drawer_surfaces_independent_overlay_audit_and_leg_trial_guard(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const drawer = buildDecisionDrawer({
  decision_id: 'dec-independent',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.01 },
  position_target: { position_intent: 'hold', current_position_qty: 0.01, target_position_qty: 0.01 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  hedge_mode_audit: {
    position_mode: {
      configured_derivatives_position_mode: 'hedge',
      required_exchange_position_mode: 'long_short_mode',
      exchange_position_mode: 'long_short_mode',
      exchange_position_mode_matches_configured: true,
      position_mode_match_required: true,
      observed_position_modes: ['long_short_mode'],
      observed_pos_sides: ['long', 'short'],
      mode_change_detected: false,
      contract_mismatch_detected: false,
    },
    overlay: {
      configured_mode: 'independent',
      effective_mode: 'independent',
      overlay_source: 'independent_books',
      active: true,
      state: 'holding',
      long_leg_score: 0.74,
      short_leg_score: 0.68,
      long_leg_reason_codes: ['independent_long_book_signal_above_entry_threshold'],
      short_leg_reason_codes: ['independent_short_book_hold_above_entry_threshold'],
      long_leg_blocked_reasons: ['independent_long_book_trial_guard_active'],
      short_leg_blocked_reasons: [],
      items: [
        { pos_side: 'long', action: 'open', execution_mode: 'independent_long_book', target_position_qty: '0.03', delta_position_qty: '0.02', trigger_reason_codes: ['independent_long_book_signal_above_entry_threshold'] },
        { pos_side: 'short', action: 'reduce', execution_mode: 'independent_short_book', target_position_qty: '-0.01', delta_position_qty: '0.01', trigger_reason_codes: ['independent_short_book_hold_above_entry_threshold'] },
      ],
    },
    leg_trial_guard: {
      enabled: true,
      mode: 'independent',
      total_count: 2,
      active_count: 1,
      items: [
        { leg: 'long', status: 'blocked', active: true, recent_closed_trade_count: 5, recent_net_realized_pnl: '-12', recent_win_rate: 0.2, guardrail_flags: ['fee_drag_elevated'], cooldowns: {}, reason_code: 'independent_long_book_trial_guard_active' },
        { leg: 'short', status: 'clear', active: false, recent_closed_trade_count: 5, recent_net_realized_pnl: '8', recent_win_rate: 0.8, guardrail_flags: [], cooldowns: { min_hold_remaining_seconds: 30 } },
      ],
    },
    leg_orders: { total_count: 0, open_count: 0, reduce_count: 0, close_count: 0, pos_sides: [], symbols: [], items: [] },
    leg_reconciliation: { total_count: 0, missing_execution_chain_count: 0, items: [] },
  },
});

console.log(JSON.stringify({
  hasOverlayAudit: drawer.body.includes('Overlay 审计'),
  hasIndependentMode: drawer.body.includes('独立双书'),
  hasLegTrialGuard: drawer.body.includes('腿级试盘守护'),
  hasLongTrialGuardCopy: drawer.body.includes('long book 的试盘守护已经触发'),
  hasIndependentBookLabel: drawer.body.includes('独立多书'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hasOverlayAudit":true', result.stdout)
        self.assertIn('"hasIndependentMode":true', result.stdout)
        self.assertIn('"hasLegTrialGuard":true', result.stdout)
        self.assertIn('"hasLongTrialGuardCopy":true', result.stdout)
        self.assertIn('"hasIndependentBookLabel":true', result.stdout)

    def test_decision_drawer_surfaces_execution_discipline_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const familyExecutionSummary = {
  summary_mode: 'single_leg',
  family: 'opportunistic',
  route_action: 'override_target',
  family_action: 'open_opportunity_leg',
  leg_count: 1,
  position_intents: ['open_short'],
  directions: ['short'],
  leg_actions: ['open'],
  execution_modes: ['opportunistic_overlay'],
  book_runtime_states: [
    {
      leg: 'short',
      current_qty: '0',
      target_qty: '0.02',
      state: 'opening',
      book_action: 'open',
      policy_reason: 'opportunistic_entry_guarded_passive_first',
    },
  ],
  book_expectancy_summary: {
    source: 'opportunistic_overlay',
    books: [
      {
        leg: 'short',
        expected_gross_edge_bps: 8.0,
        expected_signal_edge_bps: 8.0,
        expected_slippage_bps: 1.0,
        expected_cost_bps: 4.0,
        expected_net_edge_bps: 4.0,
        required_safe_net_edge_bps: 6.0,
        max_acceptable_cost_bps: 7.5,
        weak_edge_execution_mode: 'report_only',
        weak_edge_report_only: true,
        passive_first_required: true,
      },
    ],
  },
};

const drawer = buildDecisionDrawer({
  decision_id: 'dec-opportunistic-discipline',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.05 },
  position_target: {
    symbol: 'BTC-USDT-SWAP',
    expected_signal_edge_bps: 30.0,
    expected_cost_bps: 9.0,
    expected_net_edge_bps: 21.0,
    family_execution_summary: familyExecutionSummary,
    book_expectancy_summary: familyExecutionSummary.book_expectancy_summary,
  },
  decision_outcome: {
    final_action: 'enter',
    final_direction: 'short',
    family_execution_summary: familyExecutionSummary,
  },
  ai_economic_actionability: {
    economically_actionable: true,
    min_required_net_edge_bps: 2.0,
    estimated_edge_bps: 15.0,
    estimated_cost_bps: 5.0,
    estimated_net_edge_bps: 10.0,
    target_expected_signal_edge_bps: 8.0,
    target_expected_cost_bps: 4.0,
    target_expected_net_edge_bps: 4.0,
    target_required_safe_net_edge_bps: 6.0,
    target_max_acceptable_cost_bps: 7.5,
    target_weak_edge_execution_mode: 'report_only',
    target_weak_edge_report_only: true,
    target_passive_first_required: true,
  },
  ai_decision_audit: {
    configured_mode: 'baseline_only',
    assessment_operating_mode: 'baseline_only',
    final_action: 'enter',
    final_direction: 'short',
    family_execution_summary: familyExecutionSummary,
    market_snapshot_fresh: true,
    account_snapshot_fresh: true,
    safe_to_trade: true,
    recent_fee_drag_ratio: 0.03,
    recent_churn_ratio: 0.02,
    recent_low_edge_trade_streak: 1,
    current_open_order_count: 0,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  hasEconomicDiscipline:
    drawer.body.includes('安全净边际 6 个基点')
    && drawer.body.includes('成本上限 7.5 个基点')
    && drawer.body.includes('弱边际 仅报告')
    && drawer.body.includes('本轮只保留报告')
    && drawer.body.includes('要求被动优先'),
  hasAuditExpectancy:
    drawer.body.includes('空腿 毛/成本/净 8.00/4.00/4.00 基点')
    && drawer.body.includes('安全净边际 6.00 基点')
    && drawer.body.includes('本轮只保留报告')
    && drawer.body.includes('要求被动优先'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasEconomicDiscipline":true', stdout)
        self.assertIn('"hasAuditExpectancy":true', stdout)

    def test_strategy_view_and_decision_drawer_surface_sizing_breakdown_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const target = {
  symbol: 'BTC-USDT-SWAP',
  product_type: 'derivatives',
  margin_mode: 'cross',
  position_intent: 'open_long',
  current_position_qty: '0',
  target_position_qty: '0.014625',
  delta_position_qty: '0.014625',
  target_exposure_side: 'long',
  target_leverage: 5.0,
  sizing_breakdown: {
    sizing_mode: 'balance_aware',
    available_equity: '390',
    margin_usage_fraction: '0.75',
    target_leverage: 5.0,
    last_price: '100000',
    legacy_reference_qty: '0.004',
    resolved_reference_qty: '0.014625',
    resolved_target_qty: '0.014625',
  },
};

const strategyHtml = renderStrategyView({
  latestDecision: {
    decision_id: 'dec-sizing-ui',
    decision_context: { symbol: 'BTC-USDT-SWAP', timeframe: '15m', as_of_ts: '2026-04-14T12:00:00Z' },
    baseline_assessment: { confidence: 0.82, composite_alpha_score: 0.31, direction_bias: 'long' },
    ai_assessment: {},
    position_target: target,
    policy_decision: { execution_allowed: true, allow_reasons: [] },
    risk_decision: { approved: true, approval_reasons: [] },
  },
  recentDecisions: { decisions: [] },
  executionLatest: {},
  strategyRuntime: { summary: {}, latest_applied_target: target },
  strategyAttribution: { summary: {} },
  metrics: {},
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-sizing-ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', timeframe: '15m' },
  position_target: target,
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  strategyShowsSizing:
    strategyHtml.includes('可用权益 390')
    && strategyHtml.includes('价格 100000')
    && strategyHtml.includes('目标 +0.014625'),
  drawerShowsSizing:
    drawer.body.includes('下单规模分解')
    && drawer.body.includes('保证金占用比例 0.75')
    && drawer.body.includes('基准 +0.004')
    && drawer.body.includes('-&gt; +0.014625'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"strategyShowsSizing":true', result.stdout)
        self.assertIn('"drawerShowsSizing":true', result.stdout)

    def test_decision_drawer_uses_truthful_hold_and_ai_adoption_copy(self) -> None:
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const drawer = buildDecisionDrawer({
  decision_id: 'decision_truthful_hold',
  decision_context: { symbol: 'BTC-USDT-SWAP', timeframe: '15m', current_position_qty: '0' },
  position_target: {
    product_type: 'derivatives',
    margin_mode: 'cross',
    position_intent: 'hold',
    current_position_qty: '0',
    target_position_qty: '0',
    delta_position_qty: '0',
    target_exposure_side: 'flat',
    target_leverage: 1,
    sizing_breakdown: {
      sizing_mode: 'fixed_order_qty',
      available_equity: '393.7345',
      margin_usage_fraction: '0.75',
      target_leverage: 1,
      last_price: '77400.5',
      legacy_reference_qty: '0',
      resolved_reference_qty: '0',
      resolved_target_qty: '0',
    },
  },
  policy_decision: { execution_allowed: true, blocker_reasons: [] },
  risk_decision: { approved: true, rejection_reasons: [] },
  ai_assessment: {
    provider_name: 'deepseek',
    model_name: 'deepseek-v4-flash',
    model_version: '1.0.0',
    prompt_version: '0.3.0',
    provider_latency_ms: 16119.58,
    output_valid: true,
    fallback_used: false,
  },
  decision_outcome: {
    decision_source: 'baseline_fallback',
    decision_authority: 'final_decision',
    decision_blocked_reasons: [
      'ai_confidence_below_threshold',
      'ai_not_economically_actionable',
    ],
  },
  ai_economic_actionability: {
    economically_actionable: false,
    estimated_edge_bps: 0,
    estimated_cost_bps: 11,
    estimated_net_edge_bps: -11,
    min_required_net_edge_bps: 2,
    noise_buffer_bps: 2,
    required_total_edge_bps: 15,
    target_expected_signal_edge_bps: 3.97,
    target_expected_cost_bps: 6,
    target_expected_net_edge_bps: -4.03,
    validation_flags: ['low_edge'],
    rejection_flags: [],
    market_snapshot_fresh: true,
    account_snapshot_fresh: true,
    safe_to_trade: true,
    execution_condition: 'normal',
    current_open_order_count: 0,
    recent_fee_drag_ratio: 0,
    recent_churn_ratio: 0,
    recent_low_edge_trade_streak: 0,
  },
  ai_decision_audit: {
    configured_mode: 'ai_decision_maker',
    assessment_operating_mode: 'ai_decision_maker',
    provider_name: 'deepseek',
    baseline_direction: 'flat',
    ai_direction: 'flat',
    final_direction: 'flat',
    final_action: 'hold',
    decision_source: 'baseline_fallback',
    decision_authority: 'final_decision',
    book_runtime_states: [
      { leg: 'long', state: 'inactive', book_state: 'flat', book_action: 'inactive' },
      { leg: 'short', state: 'inactive', book_state: 'flat', book_action: 'inactive' },
    ],
  },
  hedge_mode_audit: {
    position_mode: {
      configured_derivatives_position_mode: 'hedge',
      required_exchange_position_mode: 'long_short_mode',
      exchange_position_mode: 'long_short_mode',
      exchange_position_mode_matches_configured: true,
      position_mode_match_required: true,
    },
    overlay: {
      configured_mode: 'independent',
      effective_mode: 'independent',
      overlay_source: 'independent_books',
      state: 'inactive',
      active: false,
      parent_target_signal: null,
      parent_current_signal: null,
      parent_effective_signal: null,
      signal_source: null,
      parent_lifecycle_state: null,
      parent_target_active: null,
      parent_inventory_active: null,
      parent_source_of_truth: null,
      parent_target_qty: null,
      parent_current_qty: null,
      parent_effective_qty: null,
      reason_codes: [
        'independent_long_book_signal_below_entry_threshold',
        'independent_short_book_signal_below_entry_threshold',
      ],
      blocked_reasons: [],
      long_leg_score: 0.04,
      short_leg_score: 0.01,
      items: [],
    },
  },
  ai_execution_suggestion: {
    configured_mode: 'enabled_live',
    status: 'absent',
    suggestion_present: false,
    translation_present: false,
    latest_translation: null,
    live_limit_price: null,
  },
});

console.log(JSON.stringify({
  summarySaysNoOrder: drawer.summary.includes('本轮没有下单目标'),
  hidesOrderSizingForHold: !drawer.body.includes('下单规模分解') && drawer.body.includes('本轮下单规模') && drawer.body.includes('无新增订单'),
  gateCopyIsNotBlocked: drawer.body.includes('策略门禁') && drawer.body.includes('未阻断') && drawer.body.includes('策略门禁未阻断，但本轮没有下单意图') && drawer.body.includes('风控未阻断，但本轮没有下单意图'),
  aiCopyIsAdoptionGate: drawer.body.includes('AI 采纳门槛') && drawer.body.includes('AI 未被采纳，沿用基础策略') && !drawer.body.includes('直接接管') && !drawer.body.includes('回退到基础策略'),
  showsModelLatency: drawer.body.includes('deepseek-v4-flash') && drawer.body.includes('输出有效') && drawer.body.includes('耗时 16.12 秒') && drawer.body.includes('prompt 0.3.0'),
  hidesEmptyExecutionSuggestion: !drawer.body.includes('AI 受限执行建议'),
  avoidsFakeOverlayParentSignal: !drawer.body.includes('当前驱动来源待确认驱动') && !drawer.body.includes('最终按待确认方向生效'),
  belowEntryMeansNoOpen: drawer.body.includes('本轮不打开多书') && drawer.body.includes('本轮不打开空书') && !drawer.body.includes('系统开始收口这条多书'),
  bookRuntimeHasReadableSpacing: drawer.body.includes('多书 当前未介入') && drawer.body.includes('空书 当前未介入'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"summarySaysNoOrder":true', stdout)
        self.assertIn('"hidesOrderSizingForHold":true', stdout)
        self.assertIn('"gateCopyIsNotBlocked":true', stdout)
        self.assertIn('"aiCopyIsAdoptionGate":true', stdout)
        self.assertIn('"showsModelLatency":true', stdout)
        self.assertIn('"hidesEmptyExecutionSuggestion":true', stdout)
        self.assertIn('"avoidsFakeOverlayParentSignal":true', stdout)
        self.assertIn('"belowEntryMeansNoOpen":true', stdout)
        self.assertIn('"bookRuntimeHasReadableSpacing":true', stdout)

    def test_decision_drawer_surfaces_no_trade_classification_as_primary_cause(self) -> None:
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const detail = {
  decision_id: 'decision-no-trade-classified',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_open_orders: [] },
  position_target: {
    strategy_family: 'independent',
    position_intent: 'hold',
    current_position_qty: '0',
    target_position_qty: '0',
    delta_position_qty: '0',
  },
  decision_outcome: {
    final_action: 'hold',
    final_target_qty: '0',
    selected_strategy_family: 'independent',
    no_trade_classification: {
      is_no_trade: true,
      classification: 'no_executable_independent_legs_due_signal_threshold',
      scope: 'strategy_signal_or_net_edge_gate',
      strategy_family: 'independent',
      final_action: 'hold',
      final_target_qty: '0',
      order_count: 0,
      fill_count: 0,
      policy_risk_active_blocker: false,
      reason_codes: ['independent_short_book_signal_below_entry_threshold'],
      book_runtime_states: [
        {
          leg: 'short',
          state: 'inactive',
          score: 0.15,
          entry_threshold: 0.25,
          expected_net_edge_bps: -3.2,
          reason_codes: ['independent_short_book_signal_below_entry_threshold'],
        },
      ],
    },
  },
  policy_decision: { execution_allowed: true, submission_allowed: true },
  risk_decision: { approved: true },
};

const drawer = buildDecisionDrawer(detail);
console.log(JSON.stringify({
  summaryUsesClassifier: drawer.summary.includes('独立双书的 long/short 账本分数没有达到开仓阈值'),
  hasNoTradeCard: drawer.body.includes('无交易原因') && drawer.body.includes('双书信号未达阈值'),
  showsBookEvidence: drawer.body.includes('净边际 -3.2bp') && drawer.body.includes('short book 的双书分低于开仓阈值'),
  avoidsGenericNoOrder: !drawer.summary.includes('本轮没有下单目标'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"summaryUsesClassifier":true', stdout)
        self.assertIn('"hasNoTradeCard":true', stdout)
        self.assertIn('"showsBookEvidence":true', stdout)
        self.assertIn('"avoidsGenericNoOrder":true', stdout)

    def test_decision_drawer_surfaces_pre_order_feasibility_dimensions(self) -> None:
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const detail = {
  decision_id: 'decision-pre-order-feasibility',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_open_orders: [] },
  position_target: {
    strategy_family: 'independent',
    position_intent: 'hold',
    current_position_qty: '0',
    target_position_qty: '0',
    delta_position_qty: '0',
  },
  decision_outcome: {
    final_action: 'hold',
    final_target_qty: '0',
    selected_strategy_family: 'independent',
    no_trade_classification: {
      is_no_trade: true,
      classification: 'no_executable_independent_legs_due_signal_and_net_edge_gates',
      scope: 'strategy_signal_or_net_edge_gate',
      strategy_family: 'independent',
      final_action: 'hold',
      final_target_qty: '0',
      order_count: 0,
      fill_count: 0,
      policy_risk_active_blocker: false,
      reason_codes: [
        'independent_long_book_signal_below_entry_threshold',
        'independent_long_book_expected_net_edge_below_safe_threshold',
      ],
      book_runtime_states: [
        {
          leg: 'long',
          state: 'blocked',
          score: 0.22,
          entry_threshold: 0.25,
          expected_cost_bps: 6.0,
          expected_net_edge_bps: -4.4,
          required_safe_net_edge_bps: 2.0,
          liquidity_quality_score: 0.42,
          execution_health_state: 'degraded',
          blocking_reasons: [
            'independent_long_book_signal_below_entry_threshold',
            'independent_long_book_expected_net_edge_below_safe_threshold',
          ],
        },
      ],
      pre_order_feasibility: {
        status: 'blocked',
        evidence_available: true,
        blocked_dimensions: ['signal_threshold', 'net_edge', 'book_state'],
        dimensions: {
          signal_threshold: {
            status: 'blocked',
            evidence_available: true,
            legs: [{ leg: 'long', status: 'blocked', score: 0.22, entry_threshold: 0.25 }],
          },
          net_edge: {
            status: 'blocked',
            evidence_available: true,
            legs: [{
              leg: 'long',
              status: 'blocked',
              expected_cost_bps: 6.0,
              expected_net_edge_bps: -4.4,
              required_safe_net_edge_bps: 2.0,
            }],
          },
          cost: {
            status: 'observed',
            evidence_available: true,
            legs: [{ leg: 'long', status: 'observed', expected_cost_bps: 6.0 }],
          },
          book_state: {
            status: 'blocked',
            evidence_available: true,
            legs: [{ leg: 'long', status: 'blocked', state: 'blocked' }],
          },
          liquidity: {
            status: 'observed',
            evidence_available: true,
            legs: [{ leg: 'long', status: 'observed', liquidity_quality_score: 0.42, execution_health_state: 'degraded' }],
          },
          policy_gate: { status: 'passed', evidence_available: true, reason_codes: [] },
          risk_gate: { status: 'passed', evidence_available: true, reason_codes: [] },
        },
      },
    },
  },
  policy_decision: { execution_allowed: true, submission_allowed: true },
  risk_decision: { approved: true },
};

const drawer = buildDecisionDrawer(detail);
console.log(JSON.stringify({
  hasFeasibilitySummary: drawer.body.includes('执行可行性') && drawer.body.includes('阻断维度：信号阈值 / 净边际 / 盘口/账本状态'),
  hasSignalDimension: drawer.body.includes('信号阈值') && drawer.body.includes('分数 0.22 / 阈值 0.25'),
  hasNetEdgeDimension: drawer.body.includes('净边际') && drawer.body.includes('净边际 -4.4bp') && drawer.body.includes('安全门槛 2bp'),
  hasCostDimension: drawer.body.includes('成本证据') && drawer.body.includes('成本 6bp') && drawer.body.includes('最大可接受成本缺失'),
  hasLiquidityDimension: drawer.body.includes('盘口/流动性') && drawer.body.includes('流动性分 0.42') && drawer.body.includes('执行健康'),
  hasGateDimensions: drawer.body.includes('策略门禁') && drawer.body.includes('风控门禁') && drawer.body.includes('通过'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasFeasibilitySummary":true', stdout)
        self.assertIn('"hasSignalDimension":true', stdout)
        self.assertIn('"hasNetEdgeDimension":true', stdout)
        self.assertIn('"hasCostDimension":true', stdout)
        self.assertIn('"hasLiquidityDimension":true', stdout)
        self.assertIn('"hasGateDimensions":true', stdout)

    def test_strategy_view_surfaces_no_trade_classification_in_summary_and_history(self) -> None:
        script = """
import { renderStrategySections } from './aats/api/static/modules/views/strategy-view.js';

const noTradeClassification = {
  is_no_trade: true,
  classification: 'no_executable_independent_legs_due_signal_threshold',
  scope: 'strategy_signal_or_net_edge_gate',
  strategy_family: 'independent',
  final_action: 'hold',
  final_target_qty: '0',
  order_count: 0,
  fill_count: 0,
  policy_risk_active_blocker: false,
  reason_codes: ['independent_short_book_signal_below_entry_threshold'],
  book_runtime_states: [
    { leg: 'short', state: 'inactive', score: 0.15, entry_threshold: 0.25, expected_net_edge_bps: -3.2 },
  ],
  pre_order_feasibility: {
    status: 'blocked',
    evidence_available: true,
    blocked_dimensions: ['signal_threshold', 'liquidity'],
    dimensions: {
      signal_threshold: {
        status: 'blocked',
        evidence_available: true,
        legs: [{ leg: 'short', status: 'blocked', score: 0.15, entry_threshold: 0.25 }],
      },
      liquidity: {
        status: 'observed',
        evidence_available: true,
        legs: [{ leg: 'short', status: 'observed', liquidity_quality_score: 0.48, execution_health_state: 'ok' }],
      },
      policy_gate: { status: 'passed', evidence_available: true, reason_codes: [] },
      risk_gate: { status: 'passed', evidence_available: true, reason_codes: [] },
    },
  },
};

const sections = renderStrategySections({
  latestDecision: {
    decision_id: 'decision-no-trade-classified',
    decision_time: '2026-04-25T18:00:00Z',
    decision_context: { symbol: 'BTC-USDT-SWAP', current_open_orders: [] },
    baseline_assessment: { regime: 'trend' },
    position_target: {
      product_type: 'derivatives',
      strategy_family: 'independent',
      position_intent: 'hold',
      current_position_qty: '0',
      target_position_qty: '0',
      delta_position_qty: '0',
      margin_mode: 'cross',
    },
    decision_outcome: {
      selected_strategy_family: 'independent',
      no_trade_classification: noTradeClassification,
    },
    policy_decision: { execution_allowed: true, submission_allowed: true },
    risk_decision: { approved: true },
  },
  recentDecisions: {
    decisions: [{
      decision_id: 'decision-no-trade-classified',
      decision_time: '2026-04-25T18:00:00Z',
      symbol: 'BTC-USDT-SWAP',
      timeframe: '15m',
      product_type: 'derivatives',
      position_intent: 'hold',
      current_position_qty: '0',
      target_position_qty: '0',
      delta_position_qty: '0',
      policy_result: true,
      risk_result: true,
      no_trade_classification: noTradeClassification,
    }],
    total_available: 1,
    limit: 20,
    has_more: false,
  },
  executionLatest: {},
  strategyRuntime: {},
});

const workbenchHtml = sections.strategyDecisionWorkbench;
const historyHtml = sections.strategyHistory;
const html = sections.strategyHero + workbenchHtml + historyHtml;
console.log(JSON.stringify({
  heroShowsNoTradePill: html.includes('无交易：双书信号未达阈值'),
  workbenchShowsNoTradeRow: html.includes('无交易分类') && html.includes('范围 策略信号或净边际门禁'),
  workbenchShowsPreOrderFeasibility: workbenchHtml.includes('执行可行性') && workbenchHtml.includes('阻断维度：信号阈值 / 盘口/流动性') && workbenchHtml.includes('信号阈值') && workbenchHtml.includes('盘口/流动性'),
  historyShowsClassification: html.includes('双书信号未达阈值') && !html.includes('AI 超时'),
  historyShowsPreOrderFeasibility: historyHtml.includes('执行可行性') && historyHtml.includes('信号阈值') && historyHtml.includes('盘口/流动性'),
  historyNarrativeIncludesFeasibility: historyHtml.includes('双书信号未达阈值 | 执行可行性：阻断'),
  narrativeUsesClassifier: html.includes('独立双书的 long/short 账本分数没有达到开仓阈值'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"heroShowsNoTradePill":true', stdout)
        self.assertIn('"workbenchShowsNoTradeRow":true', stdout)
        self.assertIn('"workbenchShowsPreOrderFeasibility":true', stdout)
        self.assertIn('"historyShowsClassification":true', stdout)
        self.assertIn('"historyShowsPreOrderFeasibility":true', stdout)
        self.assertIn('"historyNarrativeIncludesFeasibility":true', stdout)
        self.assertIn('"narrativeUsesClassifier":true', stdout)

    def test_decision_drawer_surfaces_book_runtime_state_summary(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const familyExecutionSummary = {
  summary_mode: 'multi_leg',
  family: 'independent',
  route_action: 'override_target',
  family_action: 'rebalance_independent_books',
  leg_count: 2,
  position_intents: ['open_long', 'close_short'],
  directions: ['long', 'short'],
  leg_actions: ['open', 'close'],
  execution_modes: ['independent_long_book', 'independent_short_book'],
  book_runtime_states: [
    {
      leg: 'long',
      current_qty: '0',
      target_qty: '0.01',
      state: 'opening',
      book_action: 'open',
      policy_reason: 'independent_entry_strong_edge_aggressive',
    },
    {
      leg: 'short',
      current_qty: '0.02',
      target_qty: '0',
      state: 'closing',
      book_action: 'close_failed_thesis',
      close_reason: 'failed_thesis',
      policy_reason: 'independent_failed_thesis_force_exit',
      execution_policy_urgency: 'high',
    },
  ],
};

const drawer = buildDecisionDrawer({
  decision_id: 'dec-independent-book-runtime',
  decision_outcome: {
    final_action: 'exit',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
  },
  ai_decision_audit: {
    configured_mode: 'baseline_only',
    assessment_operating_mode: 'baseline_only',
    final_action: 'exit',
    final_direction: 'flat',
    family_execution_summary: familyExecutionSummary,
    book_runtime_states: familyExecutionSummary.book_runtime_states,
    market_snapshot_fresh: true,
    account_snapshot_fresh: true,
    safe_to_trade: true,
    recent_fee_drag_ratio: 0.01,
    recent_churn_ratio: 0.01,
    recent_low_edge_trade_streak: 0,
    current_open_order_count: 0,
  },
});

    console.log(JSON.stringify({
          hasRuntimeStateRow:
            drawer.body.includes('每条书当前状态')
            && drawer.body.includes('多书 准备开仓')
            && drawer.body.includes('空书 准备收口')
            && drawer.body.includes('thesis失效'),
    }));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hasRuntimeStateRow":true', result.stdout or "")

    def _obsolete_test_overlay_parent_signal_summary_surfaces_in_runtime_and_decision_drawer(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const overlay = {
  enabled: true,
  runtime_supported: true,
  configured_mode: 'protective',
  effective_mode: 'protective',
  active: true,
  state: 'holding',
  main_leg_signal: 'long',
  hedge_leg_signal: 'short',
  main_leg_current_qty: 0.05,
  hedge_leg_current_qty: 0.01,
  main_leg_target_qty: 0.00,
  hedge_leg_target_qty: 0.01,
  hedge_ratio: 0.2,
  max_ratio: 0.5,
  pressure_score: 0.72,
  open_threshold: 0.58,
  close_threshold: 0.42,
  parent_target_signal: 'flat',
  parent_current_signal: 'long',
  parent_effective_signal: 'long',
  signal_source: 'inventory',
  parent_lifecycle_state: 'inventory_only',
  parent_target_active: false,
  parent_inventory_active: true,
  reason_codes: ['protective_overlay_main_signal_inferred_from_inventory'],
  blocked_reasons: [],
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: {
        product_type: 'derivatives',
        hedge_overlay_enabled: true,
        hedge_overlay_mode: 'protective',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_effective_enabled: true,
      },
    },
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    baseline_assessment: {},
    ai_assessment: {},
    position_target: {
      position_intent: 'hold',
      target_exposure_side: 'flat',
      hedge_overlay_decision: overlay,
    },
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-overlay-parent-signal',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.04 },
  position_target: { position_intent: 'hold', hedge_overlay_decision: overlay },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  hedge_mode_audit: {
    position_mode: {
      configured_derivatives_position_mode: 'hedge',
      required_exchange_position_mode: 'long_short_mode',
      exchange_position_mode: 'long_short_mode',
      exchange_position_mode_matches_configured: true,
      position_mode_match_required: true,
      observed_position_modes: ['long_short_mode'],
      observed_pos_sides: ['long', 'short'],
      mode_change_detected: false,
      contract_mismatch_detected: false,
    },
    overlay: {
      ...overlay,
      overlay_source: 'protective',
      items: [],
    },
    leg_trial_guard: {},
    leg_orders: { total_count: 0, open_count: 0, reduce_count: 0, close_count: 0, pos_sides: [], symbols: [], items: [] },
    leg_reconciliation: { total_count: 0, missing_execution_chain_count: 0, items: [] },
  },
});

console.log(JSON.stringify({
  strategyShowsParentSignals:
    strategyHtml.includes('这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效'),
  drawerShowsParentSignals:
    drawer.body.includes('父腿暴露信号')
    && drawer.body.includes('这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsParentSignals":true', stdout)
        self.assertIn('"drawerShowsParentSignals":true', stdout)

    def test_overlay_parent_postmortem_summary_surfaces_in_decision_drawer_and_risk_view(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const overlayParentSummary = {
  parent_family: 'directional',
  symbol: 'BTC-USDT-SWAP',
  target_leverage: 2,
  margin_mode: 'cross',
  target_long_qty: 0,
  target_short_qty: 0,
  current_long_qty: 0.03,
  current_short_qty: 0,
  target_qty: 0,
  current_qty: 0.03,
  effective_qty: 0.03,
  target_signal: 'flat',
  current_signal: 'long',
  effective_signal: 'long',
  signal_source: 'inventory',
  source_of_truth: 'inventory',
  lifecycle_state: 'inventory_only',
  target_active: false,
  inventory_active: true,
};

const drawer = buildDecisionDrawer({
  decision_id: 'dec-overlay-parent-postmortem',
  decision_outcome: {
    final_action: 'exit',
    final_direction: 'flat',
  },
  ai_decision_audit: {
    configured_mode: 'baseline_only',
    assessment_operating_mode: 'baseline_only',
    final_action: 'exit',
    final_direction: 'flat',
    overlay_parent_exposure_summary: overlayParentSummary,
    market_snapshot_fresh: true,
    account_snapshot_fresh: true,
    safe_to_trade: true,
    recent_fee_drag_ratio: 0,
    recent_churn_ratio: 0,
    recent_low_edge_trade_streak: 0,
    current_open_order_count: 0,
  },
});

const riskHtml = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  systemRecovery: {
    recovery: {
      safe_to_trade: true,
      review_required: false,
      resume_eligible: true,
      halted: false,
      rebaseline_available: false,
      resume_blocked_reasons: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-clean',
      severity: 'CLEAN',
      halt_required: false,
      review_required: false,
      observational_only: false,
      recommended_operator_action: null,
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-03-31T16:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'dec-overlay-parent-postmortem',
      validated_at: '2026-03-31T16:00:00Z',
      overlay_parent_exposure_summary: overlayParentSummary,
    },
  },
  metrics: {},
  health: { runtime_state: 'healthy' },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  drawerShowsPostmortem:
    drawer.body.includes('父腿暴露复盘')
    && drawer.body.includes('方向策略 / BTC-USDT-SWAP / 全仓 / 2x / 按真实库存判定')
    && drawer.body.includes('目标多头 0 / 目标空头 0 / 当前多头 +0.03 / 当前空头 0'),
  riskShowsReplayPostmortem:
    riskHtml.includes('回放父腿复盘')
    && riskHtml.includes('方向策略 / BTC-USDT-SWAP / 全仓 / 2x / 按真实库存判定')
    && riskHtml.includes('目标多头 0 / 目标空头 0 / 当前多头 +0.03 / 当前空头 0'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"drawerShowsPostmortem":true', stdout)
        self.assertIn('"riskShowsReplayPostmortem":true', stdout)

    def test_risk_view_surfaces_replay_overlay_parent_history_table(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const inventorySummary = {
  parent_family: 'directional',
  symbol: 'BTC-USDT-SWAP',
  target_leverage: 2,
  margin_mode: 'cross',
  target_long_qty: 0,
  target_short_qty: 0,
  current_long_qty: 0.03,
  current_short_qty: 0,
  target_qty: 0,
  current_qty: 0.03,
  effective_qty: 0.03,
  target_signal: 'flat',
  current_signal: 'long',
  effective_signal: 'long',
  signal_source: 'inventory',
  source_of_truth: 'inventory',
  lifecycle_state: 'inventory_only',
  target_active: false,
  inventory_active: true,
};

const mixedSummary = {
  parent_family: 'directional',
  symbol: 'BTC-USDT-SWAP',
  target_leverage: 3,
  margin_mode: 'cross',
  target_long_qty: 0.02,
  target_short_qty: 0,
  current_long_qty: 0.03,
  current_short_qty: 0,
  target_qty: 0.02,
  current_qty: 0.03,
  effective_qty: 0.03,
  target_signal: 'long',
  current_signal: 'long',
  effective_signal: 'long',
  signal_source: 'mixed',
  source_of_truth: 'mixed',
  lifecycle_state: 'target_and_inventory',
  target_active: true,
  inventory_active: true,
};

const html = renderRiskView({
  blockerControl: { blockers: [], secondary_blockers: [], next_step_summary: '' },
  systemRecovery: {
    recovery: {
      safe_to_trade: true,
      review_required: false,
      resume_eligible: true,
      halted: false,
      rebaseline_available: false,
      resume_blocked_reasons: [],
    },
  },
  reconciliationLatest: {
    reconciliation: {
      reconciliation_id: 'recon-clean',
      severity: 'CLEAN',
      halt_required: false,
      review_required: false,
      observational_only: false,
      recommended_operator_action: null,
    },
  },
  accountState: { fresh: true, last_refresh_timestamp: '2026-03-31T16:00:00Z', ready: true, blockers: [] },
  portfolio: { portfolio: { total_equity: 200, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'dec-overlay-parent-postmortem',
      validated_at: '2026-03-31T16:00:00Z',
      overlay_parent_exposure_summary: inventorySummary,
    },
    recent_validations: [
      {
        decision_id: 'dec-overlay-parent-postmortem',
        validated_at: '2026-03-31T16:00:00Z',
        healthy: true,
        divergence_count: 0,
        chain_health_score: 0.98,
        overlay_parent_exposure_summary: inventorySummary,
      },
      {
        decision_id: 'dec-overlay-parent-mixed',
        validated_at: '2026-03-31T15:00:00Z',
        healthy: true,
        divergence_count: 1,
        chain_health_score: 0.9,
        overlay_parent_exposure_summary: mixedSummary,
      },
    ],
  },
  metrics: {},
  health: { runtime_state: 'healthy' },
  uiHints: { recoveryReasonsText: '', controlPermissionMessage: '' },
});

console.log(JSON.stringify({
  hasReplayHistoryCard:
    html.includes('回放父腿历史')
    && html.includes('回放时间 / 决策')
    && html.includes('父腿阶段')
    && html.includes('契约口径')
    && html.includes('双腿数量拆解'),
  hasInventoryHistoryRow:
    html.includes('方向策略 / BTC-USDT-SWAP / 全仓 / 2x / 按真实库存判定')
    && html.includes('目标多头 0 / 目标空头 0 / 当前多头 +0.03 / 当前空头 0'),
  hasMixedHistoryRow:
    html.includes('方向策略 / BTC-USDT-SWAP / 全仓 / 3x / 按目标与库存判定')
    && html.includes('目标多头 +0.02 / 目标空头 0 / 当前多头 +0.03 / 当前空头 0'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasReplayHistoryCard":true', stdout)
        self.assertIn('"hasInventoryHistoryRow":true', stdout)
        self.assertIn('"hasMixedHistoryRow":true', stdout)

    def test_terms_prioritize_manual_review_over_only_reduce_labels(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { tradingStatusLabel, recoveryStatusLabel } from './aats/api/static/modules/terms.js';

const recovery = {
  recovery_state: 'review_required',
  review_required: true,
  only_reduce_required: true,
  safe_to_trade: false,
  resume_eligible: false,
  halted: false,
};

console.log(JSON.stringify({
  tradingReview: tradingStatusLabel(recovery) === '待人工确认',
  recoveryReview: recoveryStatusLabel(recovery) === '待人工确认',
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"tradingReview":true', result.stdout)
        self.assertIn('"recoveryReview":true', result.stdout)

    def _obsolete_test_overlay_parent_quantity_summary_surfaces_in_views_and_drawer(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderHomeView } from './aats/api/static/modules/views/home-view.js';
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const familyExecutionSummary = {
  summary_mode: 'single_leg',
  family: 'protective',
  route_action: 'override_target',
  family_action: 'close_protection_leg',
  leg_count: 1,
  position_intents: ['close_short'],
  directions: ['short'],
  leg_actions: ['close'],
  execution_modes: ['protective_overlay'],
  parent_target_signal: 'flat',
  parent_current_signal: 'long',
  parent_effective_signal: 'long',
  signal_source: 'inventory',
  parent_lifecycle_state: 'inventory_only',
  parent_target_active: false,
  parent_inventory_active: true,
  parent_source_of_truth: 'inventory',
  parent_target_qty: 0,
  parent_current_qty: 0.03,
  parent_effective_qty: 0.03,
};

const overlayDecision = {
  ...familyExecutionSummary,
  configured_mode: 'protective',
  effective_mode: 'protective',
  overlay_source: 'protective',
  active: true,
  state: 'holding',
  main_leg_signal: 'long',
  hedge_leg_signal: 'short',
};

const latestDecision = {
  decision_id: 'dec-parent-qty-ui',
  decision_time: '2026-03-31T12:00:00Z',
  decision_context: { as_of_ts: '2026-03-31T12:00:00Z', symbol: 'BTC-USDT-SWAP' },
  position_target: {
    position_intent: 'close_short',
    target_exposure_side: 'flat',
    current_position_qty: 0.03,
    target_position_qty: 0,
    delta_position_qty: -0.03,
    family_execution_summary: familyExecutionSummary,
    hedge_overlay_decision: overlayDecision,
  },
  decision_outcome: {
    final_action: 'exit',
    final_direction: 'short',
    family_execution_summary: familyExecutionSummary,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: { directional: {} },
    latest_applied_target: latestDecision.position_target,
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision,
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const homeHtml = renderHomeView({
  latestDecision,
  executionLatest: {},
  reconciliationLatest: {},
  health: { halted: false },
  mode: { execution_route: 'derivatives_live' },
  runtime: { environment_capabilities: { exchange_submission_target: 'derivatives_live' } },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0 } },
  accountState: { connected: true, fresh: true, ready: true, blockers: [] },
  metrics: { current_open_order_count: 0 },
  uiHints: {},
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-parent-qty-ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.03 },
  position_target: latestDecision.position_target,
  decision_outcome: latestDecision.decision_outcome,
  ai_decision_audit: {
    final_action: 'exit',
    final_direction: 'short',
    family_execution_summary: familyExecutionSummary,
  },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  hedge_mode_audit: {
    overlay: {
      ...overlayDecision,
      items: [],
    },
    position_mode: {},
    leg_trial_guard: {},
    leg_orders: { total_count: 0, open_count: 0, reduce_count: 0, close_count: 0, pos_sides: [], symbols: [], items: [] },
    leg_reconciliation: { total_count: 0, missing_execution_chain_count: 0, items: [] },
  },
});

const fragment = '这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效，生效仓位 +0.03，目标 / 当前 0 / +0.03';

console.log(JSON.stringify({
  strategyShowsParentQty: strategyHtml.includes(fragment),
  homeShowsParentQty: homeHtml.includes(fragment),
  drawerShowsParentQty: drawer.body.includes(fragment),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsParentQty":true', stdout)
        self.assertIn('"homeShowsParentQty":true', stdout)
        self.assertIn('"drawerShowsParentQty":true', stdout)


    def test_strategy_view_surfaces_overlay_residual_close_summary_copy(self) -> None:
        result = _render_strategy_view_with_hidden_strings([
            "当前选中的策略家族正在收回保护腿。",
            "收回保护腿",
            "当前 allocator v2 已批准收回保护腿的账户级执行目标。",
            "当前选中的策略家族正在收回机会腿。",
            "收回机会腿",
            "当前 allocator v2 已批准收回机会腿的账户级执行目标。",
        ])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesAll":true', result.stdout or "")

    def test_strategy_view_uses_reason_copy_for_blocked_smart_arbitrage_intents_and_multi_pair_targets(self) -> None:
        result = _render_strategy_view_with_hidden_strings([
            "按多组套利对分别执行",
            "Positive basis pair is ready.",
            "当前是正基差，但这组配对没有开放正向现货套利模式，系统暂不执行。",
            "execution_modes 配置非法",
        ])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesAll":true', result.stdout or "")

    def test_strategy_view_localizes_negative_basis_reason_copy_across_advisory_opening_and_blocked_states(self) -> None:
        result = _render_strategy_view_with_hidden_strings([
            "当前是负基差，但自动执行只支持正基差双腿；现货现金模式不能自动做空。",
            "当前是负基差，且保证金融券反套链路已就绪，系统会按借币卖出现货并买入合约的模式生成双腿计划。",
            "当前识别到负基差，但账户里没有可用于反套的现货余额，不能自动生成库存反套执行计划。",
            "当前识别到负基差，配置要求走保证金融券反套，但这条执行模式当前未启用。",
            "当前识别到负基差，但账户里没有可用于反套的现货余额。",
            "当前识别到负基差，但保证金融券反套模式当前未启用。",
            "Negative basis is detected, but reverse-carry auto execution is not available.",
            "Negative basis reverse carry is ready with margin-backed spot execution.",
            "Negative basis is detected, but the configured reverse-carry execution path is blocked.",
        ])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesAll":true', result.stdout or "")

    def test_strategy_view_compacts_observe_only_smart_arbitrage_copy(self) -> None:
        result = _render_strategy_view_with_hidden_strings([
            "当前基差 -4.7 个基点，还没有达到入场阈值 40 个基点，系统继续观察。",
            "本轮不入场",
            "暂不生成套利双腿",
            "当前还没有生成套利双腿。",
            "BTC-USDT <-> BTC-USDT-SWAP | 基差 -4.7 个基点 | 入场阈值 待确认",
        ])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesAll":true', result.stdout or "")

    def test_strategy_view_distinguishes_waiting_exit_vs_kill_switch_blocked_exit_and_short_card_states(self) -> None:
        result = _render_strategy_view_with_hidden_strings([
            "这不是挂单未成",
            "平仓提交被 kill switch 阻断",
            "配置允许，但当前运行线已暂停",
            "交易所里并没有新的退出挂单",
            "配置允许自动做空",
            "当前这轮基础信号并不偏空",
            "当前已经识别到偏空机会，但 kill switch 正在阻断任何新增暴露。",
        ])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesAll":true', result.stdout or "")

    def test_independent_expectancy_summary_surfaces_in_runtime_and_decision_ui(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const bookExpectancySummary = {
  source: 'independent_book',
  books: [
    { leg: 'long', expected_gross_edge_bps: 18.0, expected_signal_edge_bps: 18.0, expected_slippage_bps: 1.5, expected_cost_bps: 6.0, expected_net_edge_bps: 12.0 },
    { leg: 'short', expected_gross_edge_bps: 4.0, expected_signal_edge_bps: 4.0, expected_slippage_bps: 1.5, expected_cost_bps: 6.0, expected_net_edge_bps: -2.0 },
  ],
};

const familyExecutionSummary = {
  summary_mode: 'multi_leg',
  family: 'independent',
  route_action: 'override_target',
  family_action: 'open_independent_book',
  leg_count: 2,
  position_intents: ['open_long', 'open_short'],
  directions: ['long', 'short'],
  leg_actions: ['open'],
  execution_modes: ['independent_long_book', 'independent_short_book'],
  book_expectancy_summary: bookExpectancySummary,
};

const latestDecision = {
  decision_id: 'dec-independent-expectancy',
  decision_context: { symbol: 'BTC-USDT-SWAP' },
  position_target: { family_execution_summary: familyExecutionSummary, position_intent: 'hold', target_position_qty: 0, current_position_qty: 0, delta_position_qty: 0 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [{ family: 'independent', state: 'opening', route_action: 'override_target', book_expectancy_summary: bookExpectancySummary }], automation_decisions: [] },
    latest_applied_target: latestDecision.position_target,
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision,
});

const drawer = buildDecisionDrawer({
  decision_id: latestDecision.decision_id,
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: latestDecision.position_target,
  decision_outcome: { final_action: 'enter', final_direction: 'flat', family_execution_summary: familyExecutionSummary },
  ai_decision_audit: { family_execution_summary: familyExecutionSummary, market_snapshot_fresh: true, account_snapshot_fresh: true, safe_to_trade: true, recent_fee_drag_ratio: 0.03, recent_churn_ratio: 0.02, recent_low_edge_trade_streak: 1, current_open_order_count: 0 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  strategyHidesCandidateExpectancy: !strategyHtml.includes('多书 毛/成本/净 18.00/6.00/12.00 基点') && !strategyHtml.includes('空书 毛/成本/净 4.00/6.00/-2.00 基点'),
  drawerShowsExpectancy: drawer.body.includes('每条书预期边际') && drawer.body.includes('多书 毛/成本/净 18.00/6.00/12.00 基点') && drawer.body.includes('空书 毛/成本/净 4.00/6.00/-2.00 基点'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyHidesCandidateExpectancy":true', stdout)
        self.assertIn('"drawerShowsExpectancy":true', stdout)

    def test_independent_expected_vs_realized_summary_surfaces_in_runtime_and_decision_ui(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const diagnostics = {
  family: 'independent',
  sample_count: 4,
  expected_sample_count: 4,
  realized_sample_count: 3,
  overlap_sample_count: 3,
  avg_expected_net_edge_bps: 8.5,
  avg_realized_net_bps: 5.3,
  passive_first_usage_ratio: 0.6667,
  expected_realized_net_gap_bps: -3.2,
  book_breakdown: [
    { leg: 'long', sample_count: 2, avg_expected_net_edge_bps: 9.0, avg_realized_net_bps: 6.0 },
    { leg: 'short', sample_count: 2, avg_expected_net_edge_bps: 8.0, avg_realized_net_bps: 4.6 },
  ],
  close_reason_distribution: [{ reason: 'stale_thesis', count: 1 }],
};

const familyExecutionSummary = {
  summary_mode: 'multi_leg',
  family: 'independent',
  route_action: 'override_target',
  family_action: 'rebalance_independent_books',
  leg_count: 2,
  position_intents: ['open_long', 'close_short'],
  directions: ['long', 'short'],
  leg_actions: ['open', 'close'],
  execution_modes: ['independent_long_book', 'independent_short_book'],
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    independent_expected_vs_realized_summary: diagnostics,
    latest_snapshot: { candidates: [], automation_decisions: [] },
    latest_applied_target: { family_execution_summary: familyExecutionSummary },
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: { position_target: { family_execution_summary: familyExecutionSummary }, policy_decision: { execution_allowed: true }, risk_decision: { approved: true } },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-independent-evr',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.01 },
  position_target: { family_execution_summary: familyExecutionSummary },
  decision_outcome: { final_action: 'exit', final_direction: 'flat', family_execution_summary: familyExecutionSummary },
  ai_decision_audit: { family_execution_summary: familyExecutionSummary, independent_expected_vs_realized_summary: diagnostics, market_snapshot_fresh: true, account_snapshot_fresh: true, safe_to_trade: true, recent_fee_drag_ratio: 0.03, recent_churn_ratio: 0.02, recent_low_edge_trade_streak: 1, current_open_order_count: 0 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
});

console.log(JSON.stringify({
  strategyHidesDiagnostics: !strategyHtml.includes('独立双书预期 vs 已实现') && !strategyHtml.includes('样本 4（预期 4 / 已实现 3 / 重合 3）') && !strategyHtml.includes('预期净边际 8.50 基点') && !strategyHtml.includes('已实现净收益 5.30 基点') && !strategyHtml.includes('被动优先 66.7%') && !strategyHtml.includes('退出原因 thesis过期 1 次'),
  drawerShowsDiagnostics: drawer.body.includes('预期 vs 已实现') && drawer.body.includes('样本 4（预期 4 / 已实现 3 / 重合 3）') && drawer.body.includes('预期偏差 -3.20 基点') && drawer.body.includes('多书 样本 2 / 预期 9.00 / 已实现 6.00 基点') && drawer.body.includes('空书 样本 2 / 预期 8.00 / 已实现 4.60 基点'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyHidesDiagnostics":true', stdout)
        self.assertIn('"drawerShowsDiagnostics":true', stdout)

    def test_overlay_parent_signal_summary_surfaces_in_runtime_and_decision_drawer(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const overlay = {
  configured_mode: 'protective',
  effective_mode: 'protective',
  parent_target_signal: 'flat',
  parent_current_signal: 'long',
  parent_effective_signal: 'long',
  parent_source_of_truth: 'inventory',
  parent_lifecycle_state: 'inventory_only',
  parent_target_active: false,
  parent_inventory_active: true,
  parent_target_qty: 0,
  parent_current_qty: 0.03,
  parent_effective_qty: 0.03,
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    latest_applied_target: { hedge_overlay_decision: overlay },
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: { position_target: { hedge_overlay_decision: overlay }, policy_decision: { execution_allowed: true }, risk_decision: { approved: true } },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-overlay-parent-signal',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.04 },
  position_target: { position_intent: 'hold', hedge_overlay_decision: overlay },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  hedge_mode_audit: {
    position_mode: { exchange_position_mode_matches_configured: true, position_mode_match_required: true, observed_position_modes: ['long_short_mode'], observed_pos_sides: ['long', 'short'] },
    overlay: { ...overlay, overlay_source: 'protective', items: [] },
    leg_trial_guard: {},
    leg_orders: { total_count: 0, open_count: 0, reduce_count: 0, close_count: 0, pos_sides: [], symbols: [], items: [] },
    leg_reconciliation: { total_count: 0, missing_execution_chain_count: 0, items: [] },
  },
});

console.log(JSON.stringify({
  strategyHidesParentSignals: !strategyHtml.includes('这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效'),
  drawerShowsParentSignals: drawer.body.includes('父腿暴露信号') && drawer.body.includes('这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyHidesParentSignals":true', stdout)
        self.assertIn('"drawerShowsParentSignals":true', stdout)

    def test_overlay_parent_quantity_summary_surfaces_in_views_and_drawer(self) -> None:
        script = """
import { renderHomeView } from './aats/api/static/modules/views/home-view.js';
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const familyExecutionSummary = {
  summary_mode: 'single_leg',
  family: 'protective',
  route_action: 'override_target',
  family_action: 'close_protection_leg',
  leg_count: 1,
  position_intents: ['close_short'],
  directions: ['short'],
  leg_actions: ['close'],
  execution_modes: ['protective_overlay'],
  parent_target_signal: 'flat',
  parent_current_signal: 'long',
  parent_effective_signal: 'long',
  signal_source: 'inventory',
  parent_lifecycle_state: 'inventory_only',
  parent_target_active: false,
  parent_inventory_active: true,
  parent_source_of_truth: 'inventory',
  parent_target_qty: 0,
  parent_current_qty: 0.03,
  parent_effective_qty: 0.03,
};

const overlayDecision = { ...familyExecutionSummary, configured_mode: 'protective', effective_mode: 'protective', overlay_source: 'protective', active: true, state: 'holding', main_leg_signal: 'long', hedge_leg_signal: 'short' };

const latestDecision = {
  decision_id: 'dec-parent-qty-ui',
  decision_context: { symbol: 'BTC-USDT-SWAP' },
  position_target: { position_intent: 'close_short', target_exposure_side: 'flat', current_position_qty: 0.03, target_position_qty: 0, delta_position_qty: -0.03, family_execution_summary: familyExecutionSummary, hedge_overlay_decision: overlayDecision },
  decision_outcome: { final_action: 'exit', final_direction: 'short', family_execution_summary: familyExecutionSummary },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    summary: {},
    latest_snapshot: { candidates: [], automation_decisions: [] },
    configured_parameters: { directional: {} },
    latest_applied_target: latestDecision.position_target,
    latest_bundle: {},
    latest_allocation_decision: {},
    recent_sleeve_intents: [],
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision,
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const homeHtml = renderHomeView({
  latestDecision,
  executionLatest: {},
  reconciliationLatest: {},
  health: { halted: false },
  mode: { execution_route: 'derivatives_live' },
  runtime: { environment_capabilities: { exchange_submission_target: 'derivatives_live' } },
  systemRecovery: { recovery: { safe_to_trade: true, halted: false, resume_eligible: true } },
  blockers: { blockers: [] },
  portfolio: { portfolio: { total_equity: 1200, unrealized_pnl: 0, gross_exposure: 0, net_exposure: 0 } },
  accountState: { connected: true, fresh: true, ready: true, blockers: [] },
  metrics: { current_open_order_count: 0 },
  uiHints: {},
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-parent-qty-ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0.03 },
  position_target: latestDecision.position_target,
  decision_outcome: latestDecision.decision_outcome,
  ai_decision_audit: { final_action: 'exit', final_direction: 'short', family_execution_summary: familyExecutionSummary },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  hedge_mode_audit: {
    overlay: { ...overlayDecision, items: [] },
    position_mode: {},
    leg_trial_guard: {},
    leg_orders: { total_count: 0, open_count: 0, reduce_count: 0, close_count: 0, pos_sides: [], symbols: [], items: [] },
    leg_reconciliation: { total_count: 0, missing_execution_chain_count: 0, items: [] },
  },
});

const fragment = '这次判断主要由真实库存驱动，库存延续，最终按偏多方向生效，生效仓位 +0.03，目标 / 当前 0 / +0.03';

console.log(JSON.stringify({
  strategyHidesParentQty: !strategyHtml.includes(fragment),
  homeShowsParentQty: homeHtml.includes(fragment),
  drawerShowsParentQty: drawer.body.includes(fragment),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyHidesParentQty":true', stdout)
        self.assertIn('"homeShowsParentQty":true', stdout)
        self.assertIn('"drawerShowsParentQty":true', stdout)

    def test_strategy_view_surfaces_opportunistic_execution_discipline_summary(self) -> None:
        result = _render_strategy_view_with_hidden_strings([
            "空腿 毛/成本/净 8.00/4.00/4.00 基点",
            "安全净边际 6.00 基点",
            "弱边际 仅报告",
            "本轮只保留报告",
            "要求被动优先",
        ])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"hidesAll":true', result.stdout or "")

class TestReplayWorkspaceUI(unittest.TestCase):
    def test_replay_workspace_route_and_module_are_available(self) -> None:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(ui_router)
        app.state.runtime = SimpleNamespace(settings=AATSSettings.model_validate({}))

        with TestClient(app) as client:
            replay = client.get("/ui/replay")
            replay_js = client.get("/ui/modules/views/replay-view.js")

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay_js.status_code, 200)
        self.assertIn("/ui/replay", replay.text)
        self.assertIn('data-view="replay"', replay.text)
        self.assertIn("renderReplayView", replay_js.text)
        self.assertIn("回放父腿历史", replay_js.text)
        self.assertIn("父腿复盘与腿级对账联读", replay_js.text)

    def test_replay_view_surfaces_filter_collapse_and_linked_reconciliation(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderReplayView } from './aats/api/static/modules/views/replay-view.js';

const inventorySummary = {
  parent_family: 'directional',
  symbol: 'BTC-USDT-SWAP',
  target_leverage: 2,
  margin_mode: 'cross',
  target_long_qty: 0,
  target_short_qty: 0,
  current_long_qty: 0.03,
  current_short_qty: 0,
  target_qty: 0,
  current_qty: 0.03,
  effective_qty: 0.03,
  target_signal: 'flat',
  current_signal: 'long',
  effective_signal: 'long',
  signal_source: 'inventory',
  source_of_truth: 'inventory',
  lifecycle_state: 'inventory_only',
  target_active: false,
  inventory_active: true,
};

const targetSummary = {
  parent_family: 'directional',
  symbol: 'BTC-USDT-SWAP',
  target_leverage: 3,
  margin_mode: 'cross',
  target_long_qty: 0.02,
  target_short_qty: 0,
  current_long_qty: 0,
  current_short_qty: 0,
  target_qty: 0.02,
  current_qty: 0,
  effective_qty: 0.02,
  target_signal: 'long',
  current_signal: 'flat',
  effective_signal: 'long',
  signal_source: 'target_position',
  source_of_truth: 'target_position',
  lifecycle_state: 'target_only',
  target_active: true,
  inventory_active: false,
};

const html = renderReplayView({
  replayStatus: {
    healthy: false,
    last_validation: {
      decision_id: 'dec-inventory',
      validated_at: '2026-03-31T16:00:00Z',
      healthy: false,
      divergence_count: 2,
      replayed_event_count: 15,
      chain_health_score: 0.87,
      overlay_parent_exposure_summary: inventorySummary,
    },
  },
  replayRecentValidations: {
    validations: [
      {
        decision_id: 'dec-inventory',
        validated_at: '2026-03-31T16:00:00Z',
        healthy: false,
        divergence_count: 2,
        chain_health_score: 0.87,
        overlay_parent_exposure_summary: inventorySummary,
      },
      {
        decision_id: 'dec-target',
        validated_at: '2026-03-31T15:00:00Z',
        healthy: true,
        divergence_count: 0,
        chain_health_score: 0.98,
        overlay_parent_exposure_summary: targetSummary,
      },
    ],
    has_more: true,
    total_available: 14,
    limit: 20,
  },
  reconciliationLatest: {
    reconciliation: {
      severity: 'SOFT_MISMATCH',
      halt_required: false,
    },
    mismatch_summary: {
      leg_mismatch_summary: {
        total_count: 2,
        missing_execution_chain_count: 1,
        items: [
          {
            symbol: 'BTC-USDT-SWAP',
            leg_side: 'short',
            stored_qty: 0,
            exchange_qty: 0.01,
          },
        ],
      },
    },
  },
}, { parentFilter: 'inventory_only' }, { recentReplayValidationsLimit: 20, defaultReplayValidationsLimit: 8 });

console.log(JSON.stringify({
  hasReplayWorkspace: html.includes('父腿复盘与腿级对账联读') && html.includes('回放父腿历史'),
  hasFilterButtons:
    html.includes('全部阶段')
    && html.includes('仅库存活跃')
    && html.includes('仅目标活跃')
    && html.includes('目标与库存'),
  hasPagingButtons: html.includes('查看更多') && html.includes('收起历史'),
  hasLinkedRead:
    html.includes('父腿仍靠真实库存维持，同时最新对账还有腿级差异')
    && html.includes('缺少执行链 1 条'),
  inventoryRowRetained: html.includes('dec-inventory'),
  targetRowFilteredOut: !html.includes('dec-target'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasReplayWorkspace":true', stdout)
        self.assertIn('"hasFilterButtons":true', stdout)
        self.assertIn('"hasPagingButtons":true', stdout)
        self.assertIn('"hasLinkedRead":true', stdout)
        self.assertIn('"inventoryRowRetained":true', stdout)
        self.assertIn('"targetRowFilteredOut":true', stdout)

    def test_replay_view_surfaces_independent_version_diagnostics(self) -> None:
        script = """
import { renderReplayView } from './aats/api/static/modules/views/replay-view.js';

const html = renderReplayView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_replay_versions',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_state_version: 2,
      independent_score_stability_semantics_version: 2,
    },
  },
  replayRecentValidations: { validations: [] },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
});

console.log(JSON.stringify({
  hasVersionCard: html.includes('独立双书回放代际'),
  showsStateVersion: html.includes('状态机版本') && html.includes('>2<'),
  showsSemanticsVersion: html.includes('稳定性语义版本') && html.includes('decision_replay_versions'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"hasVersionCard":true', stdout)
        self.assertIn('"showsStateVersion":true', stdout)
        self.assertIn('"showsSemanticsVersion":true', stdout)

    def _obsolete_test_independent_adaptive_summary_surfaces_in_strategy_drawer_and_replay_view(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderReplayView } from './aats/api/static/modules/views/replay-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const adaptiveSummary = {
  family: 'independent',
  shadow_only: false,
  rollout_enabled: true,
  live_applied: true,
  health_enforcement_enabled: true,
  size_down_entry_enabled: true,
  long_short_asymmetry_enabled: true,
  reason_codes: ['adaptive_shadow_confidence_adjusted', 'independent_short_book_asymmetry_penalty_applied'],
  long_leg: {
    leg: 'long',
    live_applied: true,
    entry_threshold: 0.60,
    adaptive_entry_threshold: 0.66,
    effective_entry_threshold: 0.66,
    close_threshold: 0.48,
    adaptive_close_threshold: 0.50,
    effective_close_threshold: 0.50,
    scale_in_threshold: 0.90,
    adaptive_scale_in_threshold: 0.96,
    effective_scale_in_threshold: 0.96,
    thesis_age_seconds: 1800,
    adaptive_thesis_age_seconds: 1500,
    de_risk_net_edge_bps: 2.0,
    adaptive_de_risk_net_edge_bps: 2.6,
  },
  short_leg: {
    leg: 'short',
    live_applied: true,
    entry_threshold: 0.60,
    adaptive_entry_threshold: 0.68,
    effective_entry_threshold: 0.68,
    close_threshold: 0.48,
    adaptive_close_threshold: 0.50,
    effective_close_threshold: 0.50,
    scale_in_threshold: 0.90,
    adaptive_scale_in_threshold: 0.97,
    effective_scale_in_threshold: 0.97,
    thesis_age_seconds: 1800,
    adaptive_thesis_age_seconds: 1500,
    de_risk_net_edge_bps: 2.0,
    adaptive_de_risk_net_edge_bps: 2.7,
  },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: { product_type: 'derivatives' },
      independent: {
        adaptive_rollout_enabled: true,
        health_enforcement_enabled: true,
        size_down_entry_enabled: true,
        long_short_asymmetry_enabled: true,
      },
    },
    independent_adaptive_summary: adaptiveSummary,
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    position_target: {},
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'decision_adaptive_ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: { position_intent: 'open_long', current_position_qty: 0, target_position_qty: 0.02 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  decision_outcome: {
    decision_source: 'baseline',
    decision_authority: 'reference_only',
  },
  ai_decision_audit: {
    independent_adaptive_summary: adaptiveSummary,
  },
});

const replayHtml = renderReplayView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_adaptive_ui',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_adaptive_summary: adaptiveSummary,
    },
  },
  replayRecentValidations: { validations: [] },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
});

console.log(JSON.stringify({
  strategyShowsAdaptiveCard: strategyHtml.includes('独立双书自适应阈值') && strategyHtml.includes('当前已按动态阈值重评估'),
  drawerShowsAdaptiveSummary: drawer.body.includes('自适应阈值与仓位因子') && drawer.body.includes('当前已按动态阈值重评估'),
  replayShowsAdaptivePostmortem: replayHtml.includes('最新自适应复盘') && replayHtml.includes('当前已按动态阈值重评估'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsAdaptiveCard":true', stdout)
        self.assertIn('"drawerShowsAdaptiveSummary":true', stdout)
        self.assertIn('"replayShowsAdaptivePostmortem":true', stdout)

    def _obsolete_test_transition_exception_summary_surfaces_in_strategy_drawer_replay_and_risk_views(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderReplayView } from './aats/api/static/modules/views/replay-view.js';
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const transitionSummary = {
  family: 'independent',
  total_books: 2,
  invalid_transition_count: 1,
  affected_legs: ['long'],
  violation_reasons: ['independent_transition_invalid:cooldown->building'],
  blocking: true,
  items: [
    {
      leg: 'long',
      state: 'blocked',
      book_state: 'holding',
      guard_state: 'cooldown',
      prior_book_state: 'holding',
      prior_guard_state: 'cooldown',
      book_action: 'blocked',
      transition_valid: false,
      transition_violation_reason: 'independent_transition_invalid:cooldown->building',
    },
  ],
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    configured_parameters: {
      directional: { product_type: 'derivatives' },
      independent: {},
    },
    independent_transition_exception_summary: transitionSummary,
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: {
    position_target: {},
    policy_decision: { execution_allowed: true, rejection_reasons: [] },
    risk_decision: { approved: true, rejection_reasons: [], constraints_applied: [] },
  },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'decision_transition_ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: { position_intent: 'open_long', current_position_qty: 0, target_position_qty: 0.02 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  decision_outcome: {
    decision_source: 'baseline',
    decision_authority: 'reference_only',
  },
  ai_decision_audit: {
    independent_transition_exception_summary: transitionSummary,
  },
});

const replayHtml = renderReplayView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_transition_ui',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_transition_exception_summary: transitionSummary,
    },
  },
  replayRecentValidations: { validations: [] },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
});

const riskHtml = renderRiskView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_transition_ui',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_transition_exception_summary: transitionSummary,
    },
    recent_validations: [],
  },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
  blockerControl: { blockers: [], secondary_blockers: [] },
  blockers: { blockers: [] },
  systemRecovery: { recovery: { safe_to_trade: true, resume_eligible: true, review_required: false } },
  accountState: { fresh: true },
  portfolio: { portfolio: { total_equity: 0, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  positions: { local_instrument_positions: [] },
  health: { runtime_state: 'healthy' },
});

console.log(JSON.stringify({
  strategyShowsTransitionCard: strategyHtml.includes('独立双书迁移异常') && strategyHtml.includes('非法迁移'),
  drawerShowsTransitionSummary: drawer.body.includes('迁移异常摘要') && drawer.body.includes('非法迁移'),
  replayShowsTransitionPostmortem: replayHtml.includes('最新迁移异常复盘') && replayHtml.includes('非法迁移'),
  riskShowsTransitionPostmortem: riskHtml.includes('回放迁移异常') && riskHtml.includes('非法迁移'),
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyShowsTransitionCard":true', stdout)
        self.assertIn('"drawerShowsTransitionSummary":true', stdout)
        self.assertIn('"replayShowsTransitionPostmortem":true', stdout)
        self.assertIn('"riskShowsTransitionPostmortem":true', stdout)


    def test_independent_adaptive_summary_surfaces_in_strategy_drawer_and_replay_view(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderReplayView } from './aats/api/static/modules/views/replay-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const adaptiveSummary = {
  family: 'independent',
  shadow_only: false,
  rollout_enabled: true,
  live_applied: true,
  health_enforcement_enabled: true,
  size_down_entry_enabled: true,
  long_short_asymmetry_enabled: true,
  reason_codes: ['adaptive_shadow_confidence_adjusted'],
  long_leg: { leg: 'long', effective_entry_threshold: 0.66 },
  short_leg: { leg: 'short', effective_entry_threshold: 0.68 },
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    configured_parameters: { directional: { product_type: 'derivatives' }, independent: { adaptive_rollout_enabled: true } },
    independent_adaptive_summary: adaptiveSummary,
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: { position_target: {}, policy_decision: { execution_allowed: true }, risk_decision: { approved: true } },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'decision_adaptive_ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: { position_intent: 'open_long', current_position_qty: 0, target_position_qty: 0.02 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  decision_outcome: { decision_source: 'baseline', decision_authority: 'reference_only' },
  ai_decision_audit: { independent_adaptive_summary: adaptiveSummary },
});

const replayHtml = renderReplayView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_adaptive_ui',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_adaptive_summary: adaptiveSummary,
    },
  },
  replayRecentValidations: { validations: [] },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
});

console.log(JSON.stringify({
  strategyHidesAdaptiveCard: !strategyHtml.includes('独立双书自适应阈值') && !strategyHtml.includes('当前已按动态阈值重评估'),
  drawerShowsAdaptiveSummary: drawer.body.includes('自适应阈值与仓位因子') && drawer.body.includes('当前已按动态阈值重评估'),
  replayShowsAdaptivePostmortem: replayHtml.includes('最新自适应复盘') && replayHtml.includes('当前已按动态阈值重评估'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyHidesAdaptiveCard":true', stdout)
        self.assertIn('"drawerShowsAdaptiveSummary":true', stdout)
        self.assertIn('"replayShowsAdaptivePostmortem":true', stdout)

    def test_transition_exception_summary_surfaces_in_strategy_drawer_replay_and_risk_views(self) -> None:
        script = """
import { renderStrategyView } from './aats/api/static/modules/views/strategy-view.js';
import { renderReplayView } from './aats/api/static/modules/views/replay-view.js';
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const transitionSummary = {
  family: 'independent',
  total_books: 2,
  invalid_transition_count: 1,
  affected_legs: ['long'],
  violation_reasons: ['independent_transition_invalid:cooldown->building'],
  blocking: true,
  items: [{ leg: 'long', transition_valid: false, transition_violation_reason: 'independent_transition_invalid:cooldown->building' }],
};

const strategyHtml = renderStrategyView({
  strategyRuntime: {
    configured_parameters: { directional: { product_type: 'derivatives' }, independent: {} },
    independent_transition_exception_summary: transitionSummary,
    latest_snapshot: { candidates: [], automation_decisions: [] },
    summary: {},
    recent_sleeve_intents: [],
    latest_bundle: {},
    latest_allocation_decision: {},
    latest_applied_target: {},
    recent_execution_bundles: [],
    recent_budget_snapshots: [],
    recent_conflict_resolutions: [],
    recent_netting_decisions: [],
    family_enablement: {},
  },
  latestDecision: { position_target: {}, policy_decision: { execution_allowed: true }, risk_decision: { approved: true } },
  strategyAttribution: { summary: {}, profitability_by_strategy_sleeve: [], sleeve_inventory_summary: [] },
  trialReviewSummary: { summary: {}, sections: {} },
});

const drawer = buildDecisionDrawer({
  decision_id: 'decision_transition_ui',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: { position_intent: 'open_long', current_position_qty: 0, target_position_qty: 0.02 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  decision_outcome: { decision_source: 'baseline', decision_authority: 'reference_only' },
  ai_decision_audit: { independent_transition_exception_summary: transitionSummary },
});

const replayHtml = renderReplayView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_transition_ui',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_transition_exception_summary: transitionSummary,
    },
  },
  replayRecentValidations: { validations: [] },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
});

const riskHtml = renderRiskView({
  replayStatus: {
    healthy: true,
    last_validation: {
      decision_id: 'decision_transition_ui',
      validated_at: '2026-04-01T12:00:00Z',
      healthy: true,
      divergence_count: 0,
      replayed_event_count: 12,
      chain_health_score: 1,
      independent_transition_exception_summary: transitionSummary,
    },
    recent_validations: [],
  },
  reconciliationLatest: { mismatch_summary: { leg_mismatch_summary: { total_count: 0, missing_execution_chain_count: 0, items: [] } } },
  blockerControl: { blockers: [], secondary_blockers: [] },
  blockers: { blockers: [] },
  systemRecovery: { recovery: { safe_to_trade: true, resume_eligible: true, review_required: false } },
  accountState: { fresh: true },
  portfolio: { portfolio: { total_equity: 0, realized_pnl: 0, unrealized_pnl: 0, margin_usage: 0, gross_exposure: 0 } },
  positions: { local_instrument_positions: [] },
  health: { runtime_state: 'healthy' },
});

console.log(JSON.stringify({
  strategyHidesTransitionCard: !strategyHtml.includes('独立双书迁移异常') && !strategyHtml.includes('非法迁移'),
  drawerShowsTransitionSummary: drawer.body.includes('迁移异常摘要') && drawer.body.includes('非法迁移'),
  replayShowsTransitionPostmortem: replayHtml.includes('最新迁移异常复盘') && replayHtml.includes('非法迁移'),
  riskShowsTransitionPostmortem: riskHtml.includes('回放迁移异常') && riskHtml.includes('非法迁移'),
}));
"""
        result = _run_node_module(script, encoding="utf-8")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout or ""
        self.assertIn('"strategyHidesTransitionCard":true', stdout)
        self.assertIn('"drawerShowsTransitionSummary":true', stdout)
        self.assertIn('"replayShowsTransitionPostmortem":true', stdout)
        self.assertIn('"riskShowsTransitionPostmortem":true', stdout)

if __name__ == "__main__":
    unittest.main()

