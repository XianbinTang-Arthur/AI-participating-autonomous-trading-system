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
            ui = client.get("/ui")
            css = client.get("/ui/app.css")
            js = client.get("/ui/app.js")
            module_js = client.get("/ui/modules/store.js")
            login = client.get("/login", follow_redirects=False)
            login_js = client.get("/ui/login.js")

        self.assertEqual(root.status_code, 200)
        self.assertEqual(ui.status_code, 200)
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertEqual(module_js.status_code, 200)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/ui")
        self.assertEqual(login_js.status_code, 200)
        self.assertEqual(root.headers["cache-control"], "no-store")
        self.assertEqual(css.headers["cache-control"], "no-store")
        self.assertEqual(js.headers["cache-control"], "no-store")
        self.assertEqual(module_js.headers["cache-control"], "no-store")
        self.assertEqual(login_js.headers["cache-control"], "no-store")
        self.assertIn("text/html; charset=utf-8", root.headers["content-type"])
        self.assertIn("application/javascript; charset=utf-8", js.headers["content-type"])
        self.assertIn("AATS 交易控制终端", root.text)
        self.assertIn("当前会话", root.text)
        self.assertIn("退出登录", root.text)
        self.assertIn("风险与对账", root.text)
        self.assertIn("/ui/app.css", ui.text)
        self.assertIn("window.refreshDashboard = refreshDashboard;", js.text)
        self.assertIn('import { fetchPanels, requestJson }', js.text)
        self.assertIn("AUTO_REFRESH_MS", module_js.text)
        self.assertIn("/auth/login", login_js.text)
        self.assertIn(".workspace-nav", css.text)
        self.assertIn(".status-ribbon", css.text)
        self.assertIn(".login-shell", css.text)
        self.assertIn("每 15 秒自动刷新一次", root.text)

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
            login = client.get("/login")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn("控制台登录", login.text)
        self.assertIn("登录", login.text)


if __name__ == "__main__":
    unittest.main()
