from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.ui import ui_router


class TestDashboardUI(unittest.TestCase):
    def test_dashboard_routes_serve_html_and_assets(self) -> None:
        app = FastAPI()
        app.include_router(ui_router)

        with TestClient(app) as client:
            root = client.get("/")
            ui = client.get("/ui")
            css = client.get("/ui/app.css")
            js = client.get("/ui/app.js")

        self.assertEqual(root.status_code, 200)
        self.assertEqual(ui.status_code, 200)
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertIn("AATS Flight Deck", root.text)
        self.assertIn("Operator Flight Deck", root.text)
        self.assertIn("Command Dock", root.text)
        self.assertIn("Overview", root.text)
        self.assertIn("Decisions", root.text)
        self.assertIn("Execution", root.text)
        self.assertIn("Diagnostics", root.text)
        self.assertIn("Validate Reconciliation", root.text)
        self.assertIn("/ui/app.css", ui.text)
        self.assertIn("refreshDashboard", js.text)
        self.assertIn("showDetail", js.text)
        self.assertIn("setActiveView", js.text)
        self.assertIn("Use a tab action or a direct lookup to inspect detail.", root.text)
        self.assertIn(".view-nav", css.text)
        self.assertIn(".status-ribbon", css.text)


if __name__ == "__main__":
    unittest.main()
