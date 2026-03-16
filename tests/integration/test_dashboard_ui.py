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
            login = client.get("/login", follow_redirects=False)
            login_js = client.get("/ui/login.js")

        self.assertEqual(root.status_code, 200)
        self.assertEqual(ui.status_code, 200)
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/ui")
        self.assertEqual(login_js.status_code, 200)
        self.assertEqual(root.headers["cache-control"], "no-store")
        self.assertEqual(css.headers["cache-control"], "no-store")
        self.assertEqual(js.headers["cache-control"], "no-store")
        self.assertEqual(login_js.headers["cache-control"], "no-store")
        self.assertIn("AATS Operator Console", root.text)
        self.assertIn("Signed-In Operator", root.text)
        self.assertIn("Logout", root.text)
        self.assertIn("Runtime Profiles", root.text)
        self.assertIn("/ui/app.css", ui.text)
        self.assertIn("refreshDashboard", js.text)
        self.assertIn("/auth/session", js.text)
        self.assertIn("/runtime-profiles", js.text)
        self.assertIn("runtime_profile_control_enabled", js.text)
        self.assertIn("logoutOperator", js.text)
        self.assertIn("/auth/login", login_js.text)
        self.assertIn(".workspace-nav", css.text)
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
            login = client.get("/login")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn("Operator Login", login.text)
        self.assertIn("Sign In", login.text)


if __name__ == "__main__":
    unittest.main()
