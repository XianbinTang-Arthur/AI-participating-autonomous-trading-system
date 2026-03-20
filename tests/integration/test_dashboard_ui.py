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
                "home": client.get("/ui/home"),
                "overview": client.get("/ui"),
                "overview_alias": client.get("/ui/overview"),
                "strategy": client.get("/ui/strategy"),
                "execution": client.get("/ui/execution"),
                "risk": client.get("/ui/risk"),
                "ai": client.get("/ui/ai"),
                "ai_config": client.get("/ui/ai-config"),
                "settings": client.get("/ui/settings"),
                "css": client.get("/ui/app.css"),
                "js": client.get("/ui/app.js"),
                "store_js": client.get("/ui/modules/store.js"),
                "home_view_js": client.get("/ui/modules/views/home-view.js"),
                "strategy_js": client.get("/ui/modules/views/strategy-view.js"),
                "ai_view_js": client.get("/ui/modules/views/ai-view.js"),
                "ai_config_js": client.get("/ui/modules/views/ai-config-view.js"),
                "admin_js": client.get("/ui/modules/views/admin-view.js"),
                "terms_js": client.get("/ui/modules/terms.js"),
                "login_js": client.get("/ui/login.js"),
            }
            login = client.get("/login", follow_redirects=False)

        for response in responses.values():
            self.assertEqual(response.status_code, 200)

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/ui")

        self.assertEqual(responses["root"].headers["cache-control"], "no-store")
        self.assertEqual(responses["home"].headers["cache-control"], "no-store")
        self.assertEqual(responses["overview_alias"].headers["cache-control"], "no-store")
        self.assertEqual(responses["css"].headers["cache-control"], "public, max-age=120")
        self.assertIn(".primary-button.is-pending", responses["css"].text)
        self.assertEqual(responses["js"].headers["cache-control"], "public, max-age=120")
        self.assertEqual(responses["store_js"].headers["cache-control"], "public, max-age=120")
        self.assertEqual(responses["login_js"].headers["cache-control"], "public, max-age=120")

        self.assertIn("text/html; charset=utf-8", responses["root"].headers["content-type"])
        self.assertIn("application/javascript; charset=utf-8", responses["js"].headers["content-type"])

        root_text = responses["root"].text
        self.assertIn("/ui/ai", root_text)
        self.assertIn("/ui/ai-config", root_text)
        self.assertIn("/ui/settings", root_text)
        self.assertIn('data-view="aiConfig"', root_text)
        self.assertIn('data-view="admin"', root_text)

        js_text = responses["js"].text
        self.assertIn("const VIEW_ROUTES", js_text)
        self.assertIn("const VIEW_META", js_text)
        self.assertIn("renderAISections", js_text)
        self.assertIn("renderAIConfigView", js_text)
        self.assertIn("AI 配置", js_text)
        self.assertIn("账户与权限工作区", js_text)
        self.assertIn('hidePageHead: true', js_text)

        store_text = responses["store_js"].text
        self.assertIn("AUTO_REFRESH_MS", store_text)
        self.assertIn("readyViews", store_text)
        self.assertIn("blockerControl", store_text)
        self.assertIn("aiConfigModel", store_text)
        self.assertIn('risk: [', store_text)
        self.assertIn('["replayStatus", "/replay/status"]', store_text)
        self.assertNotIn('risk: [\n      ["blockers", "/system/blockers"]', store_text)

        ai_text = responses["ai_view_js"].text
        self.assertIn("AI 决策链路", ai_text)
        self.assertIn("AI 复核处置", ai_text)
        self.assertIn("executionSuggestionRows", ai_text)
        self.assertIn("function readableShadowMeta", ai_text)

        risk_text = responses["risk"].text
        self.assertIn("风险与恢复", risk_text)
        self.assertIn("trigger-blocker-action", responses["js"].text)

        ai_config_text = responses["ai_config_js"].text
        self.assertIn("运行参数概览", ai_config_text)
        self.assertIn("策略档位切换", ai_config_text)
        self.assertIn("档位概览", ai_config_text)
        self.assertIn("管理员手动切换", ai_config_text)
        self.assertIn("最近一次自动切换结论", ai_config_text)
        self.assertIn("影子评估状态", ai_config_text)
        self.assertNotIn("立即评估并生成建议", ai_config_text)
        self.assertNotIn("评估并允许自动切换", ai_config_text)
        self.assertNotIn("回滚到上一稳定策略档位", ai_config_text)
        self.assertNotIn("执行建议能力", ai_config_text)
        self.assertNotIn("autoRollbackPolicyForm", ai_config_text)
        self.assertNotIn("activationPolicyForm", ai_config_text)
        self.assertNotIn("evaluate-strategy-profile", ai_config_text)

        admin_text = responses["admin_js"].text
        self.assertIn("账户与权限工作区", admin_text)
        self.assertIn("控制台账号", admin_text)
        self.assertIn("AI 配置页现在只保留策略档位状态、自动切换结论和管理员手动切换入口", admin_text)
        self.assertNotIn("回滚和激活策略已经单独迁移到 AI 配置页", admin_text)
        self.assertIn("operatorCreateForm", admin_text)
        self.assertNotIn("autoRollbackPolicyForm", admin_text)
        self.assertNotIn("activationPolicyForm", admin_text)

        self.assertIn("operator_rejected_strategy_profile_recommendation", responses["terms_js"].text)
        self.assertIn("/auth/login", responses["login_js"].text)
        self.assertIn("baseline.regime", responses["strategy_js"].text)
        self.assertIn("首要问题", responses["home_view_js"].text)

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
            ai_config = client.get("/ui/ai-config", follow_redirects=False)
            login = client.get("/login")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(ai.status_code, 303)
        self.assertEqual(ai.headers["location"], "/login")
        self.assertEqual(ai_config.status_code, 303)
        self.assertEqual(ai_config.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn("login", login.text.lower())

    def test_ai_and_risk_views_render_in_node_smoke_test(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { renderAIView } from './aats/api/static/modules/views/ai-view.js';
import { renderRiskView } from './aats/api/static/modules/views/risk-view.js';

const aiHtml = renderAIView({
  aiOverview: {
    runtime: {
      configured_operating_mode: 'baseline_only',
      effective_operating_mode: 'baseline_only',
      provider_ready: true,
      shadow_mode_enabled: true,
    },
    shadow_summary: {},
  },
  aiRuntime: {
    configured_operating_mode: 'baseline_only',
    effective_operating_mode: 'baseline_only',
    provider_ready: true,
    shadow_mode_enabled: true,
  },
  aiLatest: {},
  blockerControl: {},
  errors: {},
});

const riskHtml = renderRiskView({
  health: { runtime_state: 'halted' },
  systemRecovery: {
    recovery: {
      halted: true,
      resume_eligible: false,
      safe_to_trade: false,
      review_required: true,
      resume_blocked_reasons: ['ai_degraded_requires_manual_review'],
    },
  },
  blockerControl: {
    blockers: [],
    secondary_blockers: [],
    next_step_summary: '请先完成 AI 复核。',
  },
  metrics: {},
  portfolio: { portfolio: {} },
  accountState: {},
  reconciliationLatest: {},
  replayStatus: {},
  uiHints: {},
});

console.log(JSON.stringify({
  aiHasHero: aiHtml.includes('AI 状态概览'),
  riskHasHero: riskHtml.includes('风险与恢复'),
  riskHasBlockerPanel: riskHtml.includes('第一优先级阻断处置'),
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
        self.assertIn('"aiHasHero":true', result.stdout)
        self.assertIn('"riskHasHero":true', result.stdout)
        self.assertIn('"riskHasBlockerPanel":true', result.stdout)


if __name__ == "__main__":
    unittest.main()
