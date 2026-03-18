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
            overview = client.get("/ui")
            overview_alias = client.get("/ui/overview")
            strategy = client.get("/ui/strategy")
            execution = client.get("/ui/execution")
            risk = client.get("/ui/risk")
            settings = client.get("/ui/settings")
            css = client.get("/ui/app.css")
            js = client.get("/ui/app.js")
            module_js = client.get("/ui/modules/store.js")
            trade_display_js = client.get("/ui/modules/trade-display.js")
            strategy_js = client.get("/ui/modules/views/strategy-view.js")
            login = client.get("/login", follow_redirects=False)
            login_js = client.get("/ui/login.js")

        self.assertEqual(root.status_code, 200)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview_alias.status_code, 200)
        self.assertEqual(strategy.status_code, 200)
        self.assertEqual(execution.status_code, 200)
        self.assertEqual(risk.status_code, 200)
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertEqual(module_js.status_code, 200)
        self.assertEqual(trade_display_js.status_code, 200)
        self.assertEqual(strategy_js.status_code, 200)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/ui")
        self.assertEqual(login_js.status_code, 200)

        self.assertEqual(root.headers["cache-control"], "no-store")
        self.assertEqual(css.headers["cache-control"], "no-store")
        self.assertEqual(js.headers["cache-control"], "no-store")
        self.assertEqual(module_js.headers["cache-control"], "no-store")
        self.assertEqual(trade_display_js.headers["cache-control"], "no-store")
        self.assertEqual(login_js.headers["cache-control"], "no-store")

        self.assertIn("text/html; charset=utf-8", root.headers["content-type"])
        self.assertIn("application/javascript; charset=utf-8", js.headers["content-type"])

        self.assertIn("自动交易监控台", root.text)
        self.assertIn("登录会话", root.text)
        self.assertIn("交易总览", root.text)
        self.assertIn("/ui/strategy", root.text)
        self.assertIn("/ui/execution", root.text)
        self.assertIn("/ui/risk", root.text)
        self.assertIn("/ui/settings", root.text)

        self.assertIn("策略判断", strategy.text)
        self.assertIn("委托与成交", execution.text)
        self.assertIn("风险与恢复", risk.text)
        self.assertIn("账户与权限", settings.text)

        self.assertIn("window.refreshDashboard = refreshDashboard;", js.text)
        self.assertIn('import { fetchPanels, requestJson }', js.text)
        self.assertIn('from "./modules/trade-display.js"', js.text)
        self.assertIn('window.location.replace("/login")', js.text)
        self.assertIn("hasResolvedAuthContext", js.text)
        self.assertIn("正在确认当前账号权限", js.text)
        self.assertIn("load-more-orders", js.text)
        self.assertIn("load-more-fills", js.text)
        self.assertIn("load-more-decisions", js.text)
        self.assertIn("load-more-reconciliations", js.text)
        self.assertIn("load-more-blocker-history", js.text)
        self.assertIn("load-more-replay-validations", js.text)
        self.assertIn("patchRenderedSections", js.text)
        self.assertIn("AUTO_REFRESH_MS", module_js.text)
        self.assertIn("PAGE_LOAD_STEP", module_js.text)
        self.assertIn("recentReconciliations", module_js.text)
        self.assertIn("blockerHistory", module_js.text)
        self.assertIn("replayValidations", module_js.text)
        self.assertIn("现货委托", trade_display_js.text)
        self.assertIn("合约委托", trade_display_js.text)
        self.assertIn("现货成交", trade_display_js.text)
        self.assertIn("合约成交", trade_display_js.text)
        self.assertIn("继续观望", strategy_js.text)
        self.assertIn("baseline.regime", strategy_js.text)
        self.assertIn("/auth/login", login_js.text)
        self.assertIn(".workspace-nav", css.text)
        self.assertIn(".workspace-link", css.text)
        self.assertIn(".status-ribbon", css.text)
        self.assertIn(".login-shell", css.text)
        self.assertIn("每 15 秒刷新一次", root.text)

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
            login = client.get("/login")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(strategy.status_code, 303)
        self.assertEqual(strategy.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn("登录交易控制台", login.text)
        self.assertIn("登录", login.text)


if __name__ == "__main__":
    unittest.main()
