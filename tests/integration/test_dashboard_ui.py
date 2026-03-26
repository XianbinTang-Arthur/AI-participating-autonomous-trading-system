from __future__ import annotations

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
        self.assertIn("refreshBackgroundPanels", js_text)
        self.assertIn("backgroundGenerations", js_text)
        self.assertIn("void refreshBackgroundPanels(refreshingView, backgroundGeneration)", js_text)
        self.assertIn('if ((state.backgroundGenerations[view] || 0) !== generation) return;', js_text)
        self.assertNotIn('ai: "/ui/ai"', js_text)

        store_text = responses["store_js"].text
        self.assertIn('["profileControlSummary", "/reports/profile-control-summary"]', store_text)
        self.assertIn('["trialReviewSummary", "/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4"]', store_text)
        self.assertIn('["trialReviewHistory", "/reports/trial-review-history?limit=5&offset=0"]', store_text)
        self.assertIn("aiAnalysis", store_text)
        self.assertIn("viewBackgroundSpecs", store_text)
        self.assertIn('["aiRecent", `/ai/recent?limit=${limits.recentAIAssessments}&offset=0`]', store_text)
        self.assertIn('["aiShadowRecent", `/ai/shadow/recent?limit=${limits.recentAIShadowDecisions}&offset=0`]', store_text)
        self.assertIn('["aiShadowEvaluations", `/ai/shadow/evaluations?limit=${limits.recentAIShadowEvaluations}&offset=0`]', store_text)
        self.assertIn('["guardedLivePreflight", "/system/guarded-live-preflight"]', store_text)
        self.assertIn('["guardedLiveRunPacket", "/reports/guarded-live-run-packet"]', store_text)
        self.assertIn('["strategyRuntime", "/strategy/runtime"]', store_text)
        self.assertIn('["strategyAttribution", "/reports/strategy-attribution?limit=200"]', store_text)
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
        self.assertIn("AI 策略模式", ai_config_text)
        self.assertIn("AI 换档控制", ai_config_text)
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
        self.assertIn("renderSmartArbitrageConfigCard", strategy_text)
        self.assertIn("renderAllocatorBudgetSnapshotTable", strategy_text)
        self.assertIn("renderAllocatorConflictResolutionTable", strategy_text)
        self.assertIn("renderAllocatorNettingDecisionTable", strategy_text)
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
        self.assertNotIn("继续保持暂停", risk_text)

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
  configHasRuntimeModeCard: configHtml.includes('AI 策略模式'),
  configHasAutoProfileControlCard: configHtml.includes('AI 换档控制'),
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
        basis_entry_bps: 18,
        basis_exit_bps: 6,
        estimated_cost_bps: 10,
        quote_budget_per_trade: 200,
        max_pair_notional: 2000,
        negative_basis_mode: 'advisory_only',
        inventory_reservation_enabled: false,
        margin_short_enabled: false,
        margin_short_execution_ready: false,
        margin_short_spot_margin_mode: 'cross',
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
    family_enablement: { smart_arbitrage: { enabled: true } },
  },
  strategyAttribution: {
    summary: {},
    profitability_by_strategy_sleeve: [],
    sleeve_inventory_summary: [],
  },
  trialReviewSummary: { summary: {}, sections: {} },
});

console.log(JSON.stringify({
  hasConfigCard: html.includes('smart_arbitrage_quote_budget_per_trade') && html.includes('smart_arbitrage_margin_short_execution_ready'),
  hasPairLabel: html.includes('BTC-USDT &lt;-&gt; BTC-USDT-SWAP'),
  hasThresholdCopy: html.includes('还没有达到入场阈值'),
  hidesGenericNoLegCopy: !html.includes('当前没有附带套利双腿执行信息。'),
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
        self.assertIn('"hasConfigCard":true', result.stdout)
        self.assertIn('"hasPairLabel":true', result.stdout)
        self.assertIn('"hasThresholdCopy":true', result.stdout)
        self.assertIn('"hidesGenericNoLegCopy":true', result.stdout)

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


if __name__ == "__main__":
    unittest.main()
