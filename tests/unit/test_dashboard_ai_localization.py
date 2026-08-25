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
