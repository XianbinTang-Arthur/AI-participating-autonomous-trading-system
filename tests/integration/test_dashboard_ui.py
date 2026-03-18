from __future__ import annotations

import unittest
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
            root = client.get("/")
            home = client.get("/ui/home")
            overview = client.get("/ui")
            overview_alias = client.get("/ui/overview")
            strategy = client.get("/ui/strategy")
            execution = client.get("/ui/execution")
            risk = client.get("/ui/risk")
            ai = client.get("/ui/ai")
            settings = client.get("/ui/settings")
            css = client.get("/ui/app.css")
            js = client.get("/ui/app.js")
            store_js = client.get("/ui/modules/store.js")
            trade_display_js = client.get("/ui/modules/trade-display.js")
            strategy_js = client.get("/ui/modules/views/strategy-view.js")
            ai_view_js = client.get("/ui/modules/views/ai-view.js")
            admin_js = client.get("/ui/modules/views/admin-view.js")
            terms_js = client.get("/ui/modules/terms.js")
            login = client.get("/login", follow_redirects=False)
            login_js = client.get("/ui/login.js")

        for response in [
            root,
            home,
            overview,
            overview_alias,
            strategy,
            execution,
            risk,
            ai,
            settings,
            css,
            js,
            store_js,
            trade_display_js,
            strategy_js,
            ai_view_js,
            admin_js,
            terms_js,
            login_js,
        ]:
            self.assertEqual(response.status_code, 200)

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/ui")

        self.assertEqual(root.headers["cache-control"], "no-store")
        self.assertEqual(css.headers["cache-control"], "no-store")
        self.assertEqual(js.headers["cache-control"], "no-store")
        self.assertEqual(store_js.headers["cache-control"], "no-store")
        self.assertEqual(login_js.headers["cache-control"], "no-store")

        self.assertIn("text/html; charset=utf-8", root.headers["content-type"])
        self.assertIn("application/javascript; charset=utf-8", js.headers["content-type"])

        self.assertIn('href="/ui"', root.text)
        self.assertIn("/ui/overview", root.text)
        self.assertIn("/ui/strategy", root.text)
        self.assertIn("/ui/execution", root.text)
        self.assertIn("/ui/risk", root.text)
        self.assertIn("/ui/ai", root.text)
        self.assertIn("/ui/settings", root.text)

        self.assertIn("主页", root.text)
        self.assertIn("控制动作", root.text)
        self.assertIn("交易总览", overview_alias.text)
        self.assertIn("AI 工作台", ai.text)
        self.assertIn("AI 当前有效模式、接管门禁和 shadow 回放", ai.text)
        self.assertIn("账户与权限", settings.text)
        self.assertIn("策略档位、账户与权限", settings.text)

        self.assertIn('window.location.replace("/login")', js.text)
        self.assertIn("evaluate-strategy-profile", js.text)
        self.assertIn("evaluate-ai-shadow", js.text)
        self.assertIn("renderAISections", js.text)
        self.assertIn("AUTO_REFRESH_MS", store_js.text)
        self.assertIn("recentAIShadowEvaluations", store_js.text)
        self.assertIn("recentReconciliations", store_js.text)
        self.assertIn("home:", store_js.text)
        self.assertIn("baseline.regime", strategy_js.text)
        self.assertIn("AI 运行状态", ai_view_js.text)
        self.assertIn("evaluate-ai-shadow", ai_view_js.text)
        self.assertIn("strategyProfiles", admin_js.text)
        self.assertIn("AI 调参建议", admin_js.text)
        self.assertIn("operator_rejected_strategy_profile_recommendation", terms_js.text)
        self.assertIn("未登录", terms_js.text)
        self.assertIn("/auth/login", login_js.text)
        self.assertIn(".workspace-nav", css.text)
        self.assertIn(".workspace-link", css.text)
        self.assertIn(".utility-bar", css.text)
        self.assertIn(".status-ribbon", css.text)
        self.assertIn(".login-shell", css.text)

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
            strategy = client.get("/ui/strategy", follow_redirects=False)
            ai = client.get("/ui/ai", follow_redirects=False)
            login = client.get("/login")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(strategy.status_code, 303)
        self.assertEqual(strategy.headers["location"], "/login")
        self.assertEqual(ai.status_code, 303)
        self.assertEqual(ai.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn("login", login.text.lower())


if __name__ == "__main__":
    unittest.main()
