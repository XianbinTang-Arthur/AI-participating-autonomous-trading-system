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
        self.assertIn("Operator Console", root.text)
        self.assertIn("Inspector", root.text)
        self.assertIn("Validate Reconciliation", root.text)
        self.assertIn("Top-Level Control Surface", root.text)
        self.assertIn("data-page=\"system\"", root.text)
        self.assertIn("data-page=\"trading\"", root.text)
        self.assertIn("secondaryNav", root.text)
        self.assertIn("/ui/app.css", ui.text)
        self.assertIn("refreshDashboard", js.text)
        self.assertIn("setActivePage", js.text)
        self.assertIn("Select a decision to inspect the full chain.", js.text)
        self.assertIn(".primary-nav", css.text)
        self.assertIn(".shell", css.text)


if __name__ == "__main__":
    unittest.main()
