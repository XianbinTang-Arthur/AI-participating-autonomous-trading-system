from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_json(script: str) -> object:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_ai_model_metadata_localizes_known_provider_state() -> None:
    payload = _run_node_json(
        r"""
import { renderAISections } from './aats/api/static/modules/views/ai-view.js';

const html = renderAISections({
  aiLatest: {
    assessment: {
      provider_name: 'baseline_fallback',
      model_name: 'baseline',
      output_valid: true,
      fallback_used: true,
    },
  },
}).aiLatest;

console.log(JSON.stringify({
  localized: html.includes('服务来源 AI 未被采纳，沿用基础策略'),
  rawProvider: html.includes('provider baseline_fallback'),
}));
"""
    )

    assert payload == {"localized": True, "rawProvider": False}


def test_ai_profile_operator_summary_localizes_registered_profile_ids() -> None:
    payload = _run_node_json(
        r"""
import { renderAIAnalysisView } from './aats/api/static/modules/views/ai-analysis-view.js';

const html = renderAIAnalysisView({
  profileControlSummary: {
    control_summary: { evidence: {}, adaptive_controls: {} },
    latest_selection_decision: {
      fast_track_applied: true,
      operator_summary: '系统已从 trend_normal 切换到 trend_aggressive。',
      gating_state: {},
    },
  },
});

console.log(JSON.stringify({
  localized: html.includes('系统已从 趋势标准 切换到 趋势激进。'),
  rawNormal: html.includes('trend_normal'),
  rawAggressive: html.includes('trend_aggressive'),
}));
"""
    )

    assert payload == {"localized": True, "rawNormal": False, "rawAggressive": False}


def test_enabled_live_copy_does_not_overstate_simulation_as_real_money() -> None:
    payload = _run_node_json(
        r"""
import { renderAIConfigView } from './aats/api/static/modules/views/ai-config-view.js';
import { renderAIAnalysisSectionCards } from './aats/api/static/modules/views/ai-view.js';

const configHtml = renderAIConfigView({
  session: { role: 'admin' },
  aiRuntime: {
    configured_operating_mode: 'ai_decision_maker',
    effective_operating_mode: 'ai_decision_maker',
    execution_suggestion_mode: 'enabled_live',
  },
  summary: {
    ai: {},
    runtime_profile: { current_runtime_payload: {} },
    strategy_profile: { activation: {}, active_revision: {} },
  },
});
const analysisHtml = renderAIAnalysisSectionCards({
  aiOverview: {
    latest_execution_suggestion: {
      configured_mode: 'enabled_live',
      status: 'applied',
      translation_present: true,
      live_limit_price: 123.45,
      latest_translation: {
        applied_to_live_execution: true,
        applied_live_fields: ['order_type'],
        translation_preview: {
          execution_style: 'maker_bias',
          order_type: 'limit',
          time_in_force: 'GTC',
          limit_offset_bps: 2,
        },
      },
    },
  },
}).aiExecutionSuggestion;

console.log(JSON.stringify({
  configUsesEnvironmentNeutralTruth: configHtml.includes('已接入当前执行链')
    && configHtml.includes('模拟盘或实盘由运行模式和执行后端另行决定'),
  analysisUsesExecutionTruth: analysisHtml.includes('已应用到当前执行链')
    && analysisHtml.includes('执行应用')
    && analysisHtml.includes('已受限应用')
    && analysisHtml.includes('下单限价'),
  noFalseRealMoneyClaim: !configHtml.includes('已进入受限实盘')
    && !configHtml.includes('参与真实执行')
    && !analysisHtml.includes('已进入受限实盘')
    && !analysisHtml.includes('实盘应用')
    && !analysisHtml.includes('实盘限价'),
}));
"""
    )

    assert payload == {
        "configUsesEnvironmentNeutralTruth": True,
        "analysisUsesExecutionTruth": True,
        "noFalseRealMoneyClaim": True,
    }
