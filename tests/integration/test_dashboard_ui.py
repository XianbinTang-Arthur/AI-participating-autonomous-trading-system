from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.ui import ui_router
from aats.bootstrap.settings import AATSSettings


class TestDashboardUI(unittest.TestCase):
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
                "ai_redirect": client.get("/ui/ai", follow_redirects=False),
                "ai_analysis": client.get("/ui/ai-analysis"),
                "ai_config": client.get("/ui/ai-config"),
                "css": client.get("/ui/app.css"),
                "js": client.get("/ui/app.js"),
                "store_js": client.get("/ui/modules/store.js"),
                "refresh_interactivity_js": client.get("/ui/modules/refresh-interactivity.js"),
                "ai_view_js": client.get("/ui/modules/views/ai-view.js"),
                "ai_analysis_js": client.get("/ui/modules/views/ai-analysis-view.js"),
                "ai_config_js": client.get("/ui/modules/views/ai-config-view.js"),
                "strategy_js": client.get("/ui/modules/views/strategy-view.js"),
                "risk_js": client.get("/ui/modules/views/risk-view.js"),
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
        self.assertIn("/ui/ai-analysis", root_text)
        self.assertIn("/ui/ai-config", root_text)
        self.assertIn('data-view="aiAnalysis"', root_text)

        js_text = responses["js"].text
        self.assertIn("renderAIAnalysisView", js_text)
        self.assertIn('aiAnalysis: "/ui/ai-analysis"', js_text)
        self.assertIn("fetchDashboardBundle", js_text)
        self.assertIn("buildDashboardBundlePath", js_text)
        self.assertIn("syncRefreshDisabledButtons", js_text)
        self.assertIn("currentRefreshInteractivityRoots", js_text)
        self.assertIn("fetchDashboardBundle(buildDashboardBundlePath(refreshingView, state))", js_text)
        self.assertIn("当前正在刷新，已排队一次新的刷新请求。", js_text)
        self.assertIn("当前已在${VIEW_LABELS[nextView] || \"当前页面\"}，已刷新当前状态。", js_text)
        self.assertNotIn("refreshBackgroundPanels", js_text)
        self.assertNotIn("backgroundGenerations", js_text)
        self.assertNotIn("backgroundControllers", js_text)
        self.assertNotIn("cancelBackgroundRefresh", js_text)
        self.assertIn('document.addEventListener("visibilitychange", handleVisibilityChange);', js_text)
        self.assertIn('if (document.visibilityState !== "visible") return;', js_text)
        self.assertNotIn('ai: "/ui/ai"', js_text)

        store_text = responses["store_js"].text
        self.assertIn('["profileControlSummary", "/reports/profile-control-summary"]', store_text)
        self.assertIn('["trialReviewSummary", "/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4"]', store_text)
        self.assertIn('["trialReviewHistory", "/reports/trial-review-history?limit=5&offset=0"]', store_text)
        self.assertIn("aiAnalysis", store_text)
        self.assertIn('["aiRecent", `/ai/recent?limit=${limits.recentAIAssessments}&offset=0`]', store_text)
        self.assertIn('["aiShadowRecent", `/ai/shadow/recent?limit=${limits.recentAIShadowDecisions}&offset=0`]', store_text)
        self.assertIn('["aiShadowEvaluations", `/ai/shadow/evaluations?limit=${limits.recentAIShadowEvaluations}&offset=0`]', store_text)
        self.assertNotIn("viewBackgroundSpecs", store_text)
        self.assertIn('["guardedLivePreflight", "/system/guarded-live-preflight"]', store_text)
        self.assertIn('["guardedLiveRunPacket", "/reports/guarded-live-run-packet"]', store_text)
        self.assertIn('["positions", "/positions"]', store_text)
        self.assertIn('["strategyRuntime", "/strategy/runtime"]', store_text)
        self.assertIn('["strategyAttribution", "/reports/strategy-attribution?limit=200"]', store_text)
        self.assertIn("dashboardBundlePanelKeys", store_text)
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
        self.assertIn("renderStrategyCandidateTable", strategy_text)
        self.assertIn("renderDirectionalShortConfigCard", strategy_text)
        self.assertIn("directionalHedgeOverlayStatus", strategy_text)
        self.assertIn("renderSmartArbitrageConfigCard", strategy_text)
        self.assertIn("renderAllocatorBudgetSnapshotTable", strategy_text)
        self.assertIn("renderAllocatorConflictResolutionTable", strategy_text)
        self.assertIn("renderAllocatorNettingDecisionTable", strategy_text)
        self.assertIn("short_entry_min_signal_edge_bps", strategy_text)
        self.assertIn("short_reversal_confidence_min", strategy_text)
        self.assertIn("smart_arbitrage_quote_budget_per_trade", strategy_text)
        self.assertIn("smart_arbitrage_margin_short_execution_ready", strategy_text)
        self.assertIn("strategyFamilyEnablement", strategy_text)
        self.assertIn("策略归因", strategy_text)
        self.assertIn("自动预算与启停", strategy_text)
        self.assertIn("strategyAttribution", strategy_text)
        self.assertIn("预算快照", strategy_text)
        self.assertIn("冲突解算", strategy_text)
        self.assertIn("净额决策", strategy_text)

        risk_text = responses["risk_js"].text
        self.assertIn("启盘前自检", risk_text)
        self.assertIn("小资金运行包", risk_text)
        self.assertIn("guardedLivePreflight", risk_text)
        self.assertIn("guardedLiveRunPacket", risk_text)
        self.assertIn("你现在先做什么", risk_text)
        self.assertIn("当前主任务", risk_text)
        self.assertIn("为什么先做这一步", risk_text)
        self.assertIn("做完后会怎样", risk_text)
        self.assertIn("重新对账（刷新交易所状态）", risk_text)
        self.assertIn("接受当前状态为新基线", risk_text)
        self.assertIn("轻度差异，建议观察", risk_text)
        self.assertIn("系统仍处于人工确认流程", risk_text)
        self.assertIn('action.client_action === "navigate-view" && action.value === "risk"', risk_text)
        self.assertNotIn("继续保持暂停", risk_text)

        refresh_interactivity_text = responses["refresh_interactivity_js"].text
        self.assertIn("syncRefreshDisabledButtons", refresh_interactivity_text)
        self.assertIn("当前区域正在刷新，请等待刷新完成后再操作。", refresh_interactivity_text)

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
  hasOverlayLabel: html.includes('保护性对冲'),
  hasProtectiveSwitch: html.includes('strategy_hedge_protective_enabled'),
  hasOverlayThresholds: html.includes('strategy_hedge_open_threshold / strategy_hedge_close_threshold'),
  hasOverlayRatio: html.includes('对冲比例'),
  hasOverlayReason: html.includes('保护性压力已经超过开仓阈值'),
  hasOverlayRuntimeCopy: html.includes('当前是合约 hedge mode'),
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
        self.assertIn('"hasOverlayLabel":true', result.stdout)
        self.assertIn('"hasProtectiveSwitch":true', result.stdout)
        self.assertIn('"hasOverlayThresholds":true', result.stdout)
        self.assertIn('"hasOverlayRatio":true', result.stdout)
        self.assertIn('"hasOverlayReason":true', result.stdout)
        self.assertIn('"hasOverlayRuntimeCopy":true', result.stdout)

    def test_strategy_view_surfaces_opportunistic_overlay_config_and_state(self) -> None:
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
        hedge_overlay_mode: 'opportunistic',
        hedge_overlay_runtime_supported: true,
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
  hasOverlayLabel: html.includes('机会型对冲'),
  hasOpportunisticThresholds: html.includes('strategy_hedge_opportunistic_open_threshold / strategy_hedge_opportunistic_close_threshold'),
  hasOpportunityLeg: html.includes('机会腿'),
  hasOpportunityReason: html.includes('机会分已经超过开仓阈值'),
  hasModeCopy: html.includes('机会型对冲 有没有介入') || html.includes('机会型对冲 的腿级状态'),
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
        self.assertIn('"hasOverlayLabel":true', result.stdout)
        self.assertIn('"hasOpportunisticThresholds":true', result.stdout)
        self.assertIn('"hasOpportunityLeg":true', result.stdout)
        self.assertIn('"hasOpportunityReason":true', result.stdout)
        self.assertIn('"hasModeCopy":true', result.stdout)

    def test_strategy_view_surfaces_independent_overlay_config_and_state(self) -> None:
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
        hedge_overlay_mode: 'independent',
        hedge_overlay_runtime_supported: true,
        hedge_overlay_mode_ready: true,
        hedge_overlay_effective_enabled: true,
        hedge_independent_enabled: true,
        hedge_independent_long_entry_threshold: 0.66,
        hedge_independent_short_entry_threshold: 0.64,
        hedge_independent_long_scale_in_threshold: 0.70,
        hedge_independent_short_scale_in_threshold: 0.68,
        hedge_independent_long_min_hold_seconds: 300,
        hedge_independent_short_min_hold_seconds: 420,
        hedge_independent_rebalance_cooldown_seconds: 120,
        hedge_independent_trial_guard_enabled: true,
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
  hasIndependentLabel: html.includes('独立双书'),
  hasIndependentThresholds: html.includes('strategy_hedge_independent_long_entry_threshold / strategy_hedge_independent_short_entry_threshold'),
  hasIndependentBooks: html.includes('双书分 long 0.74 / short 0.68 | 目标 +0.03 / -0.01'),
  hasIndependentReasons: html.includes('long book 的双书分已经超过开仓阈值') && html.includes('long book 的试盘守护已经触发'),
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
        self.assertIn('"hasIndependentLabel":true', result.stdout)
        self.assertIn('"hasIndependentThresholds":true', result.stdout)
        self.assertIn('"hasIndependentBooks":true', result.stdout)
        self.assertIn('"hasIndependentReasons":true', result.stdout)

    def test_strategy_view_surfaces_overlay_rollout_stage_and_rollback_order(self) -> None:
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
  hasRolloutRow: html.includes('strategy_hedge_opportunistic_rollout_stage / strategy_hedge_independent_rollout_stage'),
  hasCurrentStage: html.includes('当前运行线 实盘 | 独立双书 受限'),
  hasBlockReason: html.includes('独立双书当前只放开到 dry-run，这条实盘运行线不会启用'),
  hasRollbackOrder: html.includes('先关闭 strategy_hedge_opportunistic_enabled'),
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
        self.assertIn('"hasRolloutRow":true', result.stdout)
        self.assertIn('"hasCurrentStage":true', result.stdout)
        self.assertIn('"hasBlockReason":true', result.stdout)
        self.assertIn('"hasRollbackOrder":true', result.stdout)

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
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(ai.status_code, 303)
        self.assertEqual(ai.headers["location"], "/login")
        self.assertEqual(ai_analysis.status_code, 303)
        self.assertEqual(ai_analysis.headers["location"], "/login")
        self.assertEqual(ai_config.status_code, 303)
        self.assertEqual(ai_config.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
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
                "strategyRuntime",
                "strategyAttribution",
                "latestDecision",
                "recentDecisions",
                "executionLatest",
                "trialReviewSummary",
                "trialReviewHistory",
            ],
        )

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
                "aiOverview",
                "aiRuntime",
                "aiLatest",
                "aiShadowLatest",
                "profileControlSummary",
                "aiRecent",
                "aiShadowRecent",
                "aiShadowEvaluations",
            ],
        )

    def test_ai_views_render_in_node_smoke_test(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderAIAnalysisView } from './aats/api/static/modules/views/ai-analysis-view.js';
import { renderAIConfigView } from './aats/api/static/modules/views/ai-config-view.js';
import { buildDecisionDrawer } from './aats/api/static/modules/detail-drawers.js';

const analysisHtml = renderAIAnalysisView({
  session: { role: 'admin' },
  blockerControl: {},
  aiOverview: {
    runtime: {
      configured_operating_mode: 'ai_decision_maker_with_profile_control',
      effective_operating_mode: 'ai_decision_maker_with_profile_control',
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
      decision_authority: 'final_decision_with_profile_control',
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

const configHtml = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'ai_decision_maker_with_profile_control',
    effective_operating_mode: 'ai_decision_maker_with_profile_control',
    manual_override_active: false,
    manual_override_default_freeze_seconds: 3600,
    shadow_mode_enabled: true,
    execution_suggestion_mode: 'shadow_translation',
    strategy_profile_auto_control_effective: false,
  },
  summary: {
    ai: {
      effective_operating_mode: 'ai_decision_maker_with_profile_control',
      configured_operating_mode: 'ai_decision_maker_with_profile_control',
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
});

const manualOnlyConfigHtml = renderAIConfigView({
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
  },
});

const drawer = buildDecisionDrawer({
  decision_id: 'dec-1',
  decision_context: { symbol: 'BTC-USDT-SWAP', current_position_qty: 0 },
  position_target: { position_intent: 'hold', current_position_qty: 0, target_position_qty: 0 },
  policy_decision: { execution_allowed: true },
  risk_decision: { approved: true },
  decision_outcome: {
    decision_source: 'baseline_fallback',
    decision_authority: 'final_decision_with_profile_control',
    decision_blocked_reasons: ['ai_confidence_below_threshold'],
  },
  ai_decision_audit: {
    configured_mode: 'ai_decision_maker_with_profile_control',
    assessment_operating_mode: 'ai_decision_maker_with_profile_control',
    decision_source: 'baseline_fallback',
    decision_authority: 'final_decision_with_profile_control',
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
  configHasRuntimeParams: configHtml.includes('运行参数概览'),
  configOmitsAdaptiveControls: !configHtml.includes('风险预算乘数') && !configHtml.includes('执行侵略性乘数'),
  configHasTimingControls: configHtml.includes('持有与冷却') && configHtml.includes('低边际保护'),
  configHasStrategyShadow: configHtml.includes('策略层 shadow'),
  configHasExecutionShadow: configHtml.includes('执行层 shadow'),
  configHasProfileControlModeButtons: configHtml.includes('手动切档') && configHtml.includes('自动切档'),
  configNoJumpButtons: !configHtml.includes('前往 AI 工作台') && !configHtml.includes('查看 AI 分析'),
  analysisHasAdaptiveControls: analysisHtml.includes('风险预算乘数') && analysisHtml.includes('自动切档闸门'),
  drawerExplainsFallback: drawer.body.includes('当前运行模式允许 AI 参与'),
  drawerUsesHumanDecisionSource: drawer.body.includes('本轮最终回退到基础策略'),
  manualOnlyProfileDefaultsToManual: /<button class="primary-button" data-action="set-profile-control-mode" data-value="manual"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyProfileAutoEnabled: /<button class="secondary-button" data-action="set-profile-control-mode" data-value="auto"/.test(manualOnlyConfigHtml),
  manualOnlyProfileButtonsUnlocked: /data-action="manual-activate-strategy-profile" data-value="trend_strict"/.test(manualOnlyConfigHtml) && !/data-action="manual-activate-strategy-profile" data-value="trend_strict"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyRuntimeCurrentModeLocked: /<button class="primary-button" data-action="select-ai-operating-mode" data-value="baseline_only"[^>]*disabled/.test(manualOnlyConfigHtml),
  manualOnlyRuntimeAvoidsLegacyButtons: !manualOnlyConfigHtml.includes('跟随配置') && !manualOnlyConfigHtml.includes('手动接管'),
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
        self.assertIn('"analysisHasRuntimeSummary":true', result.stdout)
        self.assertIn('"analysisHasDecisionChain":true', result.stdout)
        self.assertIn('"analysisUsesStrategyShadowName":true', result.stdout)
        self.assertIn('"analysisUsesExecutionShadowName":true', result.stdout)
        self.assertIn('"analysisNoTopNavButtons":true', result.stdout)
        self.assertIn('"configHasRuntimeModeCard":true', result.stdout)
        self.assertIn('"configHasAutoProfileControlCard":true', result.stdout)
        self.assertIn('"configHasRuntimeParams":true', result.stdout)
        self.assertIn('"configOmitsAdaptiveControls":true', result.stdout)
        self.assertIn('"configHasTimingControls":true', result.stdout)
        self.assertIn('"configHasStrategyShadow":true', result.stdout)
        self.assertIn('"configHasExecutionShadow":true', result.stdout)
        self.assertIn('"configHasProfileControlModeButtons":true', result.stdout)
        self.assertIn('"configNoJumpButtons":true', result.stdout)
        self.assertIn('"analysisHasAdaptiveControls":true', result.stdout)
        self.assertIn('"drawerExplainsFallback":true', result.stdout)
        self.assertIn('"drawerUsesHumanDecisionSource":true', result.stdout)
        self.assertIn('"manualOnlyProfileDefaultsToManual":true', result.stdout)
        self.assertIn('"manualOnlyProfileAutoEnabled":true', result.stdout)
        self.assertIn('"manualOnlyProfileButtonsUnlocked":true', result.stdout)
        self.assertIn('"manualOnlyRuntimeCurrentModeLocked":true', result.stdout)
        self.assertIn('"manualOnlyRuntimeAvoidsLegacyButtons":true', result.stdout)

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
      auto_parallel_enabled: false,
      automation_active_count: 0,
      automation_contracted_count: 0,
      automation_paused_count: 0,
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
  hasTradeCostCard: html.includes('trade_cost_spot_taker_fee_bps') && html.includes('trade_cost_derivatives_taker_fee_bps') && html.includes('trade_cost_delivery_settlement_fee_bps'),
  hasTradeCostBpsCopy: html.includes('8 = 0.08%') && html.includes('账户费率'),
  hasConfigCard: html.includes('smart_arbitrage_quote_budget_per_trade') && html.includes('smart_arbitrage_margin_short_execution_ready'),
  hasAdvancedConfig: html.includes('smart_arbitrage_max_concurrent_pairs') && html.includes('smart_arbitrage_cost_model_enabled') && html.includes('trade_costs.*'),
  hasHedgeLeverageConfig: html.includes('smart_arbitrage_hedge_target_leverage') && html.includes('对冲腿目标杠杆'),
  hasCostCard: html.includes('智能套利磨损模型') && html.includes('理论净优势') && html.includes('实际总磨损'),
  hasPairLabel: html.includes('BTC-USDT &lt;-&gt; BTC-USDT-SWAP'),
  hasThresholdCopy: html.includes('还没有达到入场阈值'),
  hidesGenericNoLegCopy: !html.includes('当前没有附带套利双腿执行信息。'),
  hasCostSourceCopy: html.includes('手续费按逐腿配置') && html.includes('spread 按逐腿配置'),
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
        self.assertIn('"hasTradeCostCard":true', result.stdout)
        self.assertIn('"hasTradeCostBpsCopy":true', result.stdout)
        self.assertIn('"hasConfigCard":true', result.stdout)
        self.assertIn('"hasAdvancedConfig":true', result.stdout)
        self.assertIn('"hasHedgeLeverageConfig":true', result.stdout)
        self.assertIn('"hasCostCard":true', result.stdout)
        self.assertIn('"hasPairLabel":true', result.stdout)
        self.assertIn('"hasThresholdCopy":true', result.stdout)
        self.assertIn('"hidesGenericNoLegCopy":true', result.stdout)
        self.assertIn('"hasCostSourceCopy":true', result.stdout)

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
      auto_parallel_enabled: true,
      automation_active_count: 1,
      automation_contracted_count: 0,
      automation_paused_count: 0,
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
  hasSpotOnlyCopy: html.includes('当前运行域不支持自动做空') && html.includes('long/共享开仓阈值'),
  hidesShortKeys: !html.includes('strategy_short_entry_allowed_regimes') && !html.includes('strategy_short_entry_*') && !html.includes('strategy_short_reversal_*'),
  hidesSmartArbitrageCards: !html.includes('智能套利配置') && !html.includes('智能套利磨损模型') && !html.includes('smart_arbitrage_quote_budget_per_trade'),
  keepsTradeCostCard: html.includes('trade_cost_spot_taker_fee_bps') && html.includes('trade_cost_derivatives_taker_fee_bps'),
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
        self.assertIn('"hasSpotOnlyCopy":true', result.stdout)
        self.assertIn('"hidesShortKeys":true', result.stdout)
        self.assertIn('"hidesSmartArbitrageCards":true', result.stdout)
        self.assertIn('"keepsTradeCostCard":true', result.stdout)

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
      auto_parallel_enabled: true,
      automation_active_count: 1,
      automation_contracted_count: 0,
      automation_paused_count: 0,
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
  hasNav: html.includes('href="#strategy-overview"') && html.includes('href="#strategy-reference"') && html.includes('href="#strategy-history"'),
  hasOutcomeSection: html.includes('本轮策略到底想做什么') && html.includes('当前候选与自动调度') && html.includes('试盘与自动运行状态'),
  hasCollapsedReference: html.includes('展开配置与成本参考') && html.includes('默认折叠，避免配置卡占满主工作区'),
  hasCollapsedHistory: html.includes('展开归因与历史记录') && html.includes('默认折叠，保留复盘能力但不抢主视线'),
  keepsConfigCards: html.includes('统一交易成本配置') && html.includes('方向策略做空能力') && html.includes('智能套利配置'),
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
        self.assertIn('"hasCollapsedReference":true', result.stdout)
        self.assertIn('"hasCollapsedHistory":true', result.stdout)
        self.assertIn('"keepsConfigCards":true', result.stdout)

    def test_strategy_view_uses_reason_copy_for_blocked_smart_arbitrage_intents_and_multi_pair_targets(self) -> None:
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

    def test_strategy_view_localizes_negative_basis_reason_copy_across_advisory_opening_and_blocked_states(self) -> None:
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

    def test_strategy_view_compacts_observe_only_smart_arbitrage_copy(self) -> None:
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
        self.assertIn('"thresholdCopyCount":1', result.stdout)
        self.assertIn('"hasObserveRoute":true', result.stdout)
        self.assertIn('"hasObserveTarget":true', result.stdout)
        self.assertIn('"hasNoLegPlanCopy":true', result.stdout)
        self.assertIn('"avoidsPendingThresholdCopy":true', result.stdout)

    def test_strategy_view_distinguishes_waiting_exit_vs_kill_switch_blocked_exit_and_short_card_states(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
