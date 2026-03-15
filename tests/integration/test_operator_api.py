from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance


class FakeOperatorAccountService:
    SNAPSHOT: ExchangeAccountSnapshot | None = None

    def __init__(self, *, settings, client) -> None:
        self.settings = settings
        self.client = client
        self._snapshot = self.SNAPSHOT

    async def refresh(self, *, force: bool = False):
        return self._snapshot

    def latest_snapshot(self):
        return self._snapshot

    def instrument_metadata(self, symbol: str):
        return None

    def open_order_count(self, symbol: str | None = None) -> int:
        return len(self._snapshot.open_orders) if self._snapshot is not None else 0

    def recent_fills(self, symbol: str | None = None):
        return list(self._snapshot.fills) if self._snapshot is not None else []

    def status(self):
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": self._snapshot is not None,
            "fresh": self._snapshot is not None,
            "last_update_ts": self._snapshot.fetched_at if self._snapshot is not None else None,
            "last_error": None,
            "ready": self._snapshot is not None,
            "detail": "fake_operator_account",
            "blockers": [] if self._snapshot is not None else ["account_snapshot_missing"],
        }


class TestOperatorAPI(unittest.IsolatedAsyncioTestCase):
    async def test_system_status_and_mode_endpoints_are_operator_readable(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            health = client.get("/system/health")
            mode = client.get("/system/mode")
            runtime_response = client.get("/system/runtime")
            blockers = client.get("/system/blockers")
            metrics = client.get("/system/metrics")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(mode.status_code, 200)
        self.assertEqual(runtime_response.status_code, 200)
        self.assertEqual(blockers.status_code, 200)
        self.assertEqual(metrics.status_code, 200)

        health_payload = health.json()
        mode_payload = mode.json()
        runtime_payload = runtime_response.json()
        blockers_payload = blockers.json()
        metrics_payload = metrics.json()

        self.assertIn("overall_status", health_payload)
        self.assertIn("subsystems", health_payload)
        self.assertIn("storage", health_payload["subsystems"])
        self.assertIn("audit_replay", health_payload["subsystems"])
        self.assertEqual(mode_payload["config_profile"], "local_demo")
        self.assertEqual(mode_payload["market_data_backend"], "demo")
        self.assertEqual(mode_payload["execution_backend"], "paper")
        self.assertEqual(mode_payload["ai_operating_mode"], "baseline_only")
        self.assertFalse(mode_payload["execution_blocked"])
        self.assertTrue(mode_payload["submit_blocked"])
        self.assertIn("local_demo_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIn("paper_execution_has_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIsNone(mode_payload["blocked_reason"])
        self.assertEqual(runtime_payload["symbols"], ["BTC-USDT"])
        self.assertEqual(runtime_payload["enabled_timeframes"], ["15m"])
        self.assertGreaterEqual(runtime_payload["uptime_seconds"], 0.0)
        self.assertIn("baseline_takeover", runtime_payload)
        self.assertIn("decision_cycle_count", metrics_payload)
        self.assertIn("recent_execution_errors", metrics_payload)
        self.assertIn("exposure_summary", metrics_payload)
        self.assertIsInstance(blockers_payload["blockers"], list)
        self.assertTrue(any(item["submit_only"] for item in blockers_payload["blockers"]))
        self.assertIn(health_payload["runtime_state"], {"healthy", "degraded", "blocked", "halted"})

    async def test_operator_visibility_endpoints_cover_decision_execution_reconciliation_and_audit(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            latest_decision = client.get("/decision/latest").json()
            decision_id = latest_decision["decision_id"]
            recent_decisions = client.get("/decision/recent").json()
            decision_detail = client.get(f"/decision/{decision_id}").json()
            risk_latest = client.get("/risk/latest").json()
            policy_latest = client.get("/policy/latest").json()
            portfolio_latest = client.get("/portfolio/latest").json()
            portfolio_history = client.get("/portfolio/history?limit=5").json()
            balances = client.get("/balances").json()
            positions = client.get("/positions").json()
            orders_recent = client.get("/orders/recent").json()
            latest_order_id = orders_recent["orders"][0]["client_order_id"]
            order_detail = client.get(f"/orders/{latest_order_id}").json()
            fills_recent = client.get("/fills/recent").json()
            latest_fill_id = fills_recent["fills"][0]["fill_id"]
            fill_detail = client.get(f"/fills/{latest_fill_id}").json()
            execution_latest = client.get("/execution/latest").json()
            reconciliation_latest = client.get("/reconciliation/latest").json()
            reconciliation_recent = client.get("/reconciliation/recent").json()
            reconciliation_mismatches = client.get("/reconciliation/mismatches").json()
            latest_reconciliation_id = reconciliation_latest["reconciliation"]["reconciliation_id"]
            reconciliation_detail = client.get(f"/reconciliation/{latest_reconciliation_id}").json()
            audit_latest = client.get("/audit/latest").json()
            audit_detail = client.get(f"/audit/{decision_id}").json()
            replay_status_before = client.get("/replay/status").json()
            replay_validation = client.post(f"/replay/validate/{decision_id}").json()
            replay_status_after = client.get("/replay/status").json()

        self.assertIsNotNone(decision_id)
        self.assertEqual(decision_detail["decision_id"], decision_id)
        self.assertTrue(recent_decisions["decisions"])
        self.assertEqual(risk_latest["decision_id"], decision_id)
        self.assertEqual(policy_latest["decision_id"], decision_id)
        self.assertIsNotNone(portfolio_latest["portfolio"])
        self.assertTrue(portfolio_history["snapshots"])
        self.assertIn("local_balances", balances)
        self.assertIn("local_positions", positions)
        self.assertTrue(orders_recent["orders"])
        self.assertEqual(order_detail["order"]["client_order_id"], latest_order_id)
        self.assertTrue(fills_recent["fills"])
        self.assertEqual(fill_detail["fill"]["fill_id"], latest_fill_id)
        self.assertIsNotNone(execution_latest["latest_order"])
        self.assertIsNotNone(reconciliation_latest["reconciliation"])
        self.assertIn("mismatch_categories", reconciliation_latest["mismatch_summary"])
        self.assertTrue(reconciliation_recent["reconciliations"])
        self.assertIsInstance(reconciliation_mismatches["mismatches"], list)
        self.assertEqual(
            reconciliation_detail["reconciliation"]["reconciliation_id"],
            latest_reconciliation_id,
        )
        self.assertIsNotNone(audit_latest["audit"])
        self.assertEqual(audit_detail["audit"]["decision_id"], decision_id)
        self.assertIsNone(replay_status_before["last_validation"])
        self.assertEqual(replay_validation["decision_id"], decision_id)
        self.assertTrue(replay_status_after["recent_validations"])

    async def test_halt_resume_and_stale_market_blocker_are_visible(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            health_after_halt = client.get("/system/health")
            resumed = client.post("/system/resume", json={"reason": "operator_test_resume"})

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(health_after_halt.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(halted.json()["halted"])
        blockers = [item["blocker"] for item in health_after_halt.json()["blockers"]]
        self.assertIn("kill_switch_active", blockers)
        self.assertFalse(resumed.json()["halted"])

        latest_snapshot = runtime.market_gateway.latest_snapshot(runtime.settings.default_symbol)
        self.assertIsNotNone(latest_snapshot)
        runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = latest_snapshot.model_copy(
            update={"snapshot_ts": utc_now() - timedelta(seconds=120)}
        )
        with TestClient(app) as client:
            stale_health = client.get("/system/health").json()
        stale_blockers = [item["blocker"] for item in stale_health["blockers"]]
        self.assertIn("market_data_stale", stale_blockers)

    async def test_system_health_reports_reconciliation_staleness_consistently(self) -> None:
        runtime = await self._runtime()
        latest_report = runtime.reconciliation_repo.latest()
        self.assertIsNotNone(latest_report)
        runtime.reconciliation_repo.save_report(
            latest_report.model_copy(update={"as_of_ts": utc_now() - timedelta(seconds=601)})
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            health = client.get("/system/health").json()

        blockers = [item["blocker"] for item in health["blockers"]]
        self.assertIn("reconciliation_stale", blockers)
        self.assertFalse(health["subsystems"]["reconciliation"]["fresh"])
        self.assertFalse(health["subsystems"]["reconciliation"]["ready"])
        self.assertIn("reconciliation_stale", health["subsystems"]["reconciliation"]["blockers"])
        self.assertFalse(health["freshness"]["reconciliation_fresh"])

    async def test_operator_auth_enforces_read_write_split_and_reconciliation_validate(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_read_api_key="read-key",
            operator_write_api_key="write-key",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            unauthorized = client.get("/system/health")
            read_allowed = client.get("/system/health", headers={"X-AATS-API-Key": "read-key"})
            read_denied_write = client.post(
                "/system/halt",
                json={"reason": "should_fail"},
                headers={"X-AATS-API-Key": "read-key"},
            )
            write_allowed = client.post(
                "/system/halt",
                json={"reason": "authorized_halt"},
                headers={"X-AATS-API-Key": "write-key"},
            )
            reconciliation_validate = client.post(
                "/reconciliation/validate",
                json={"reason": "startup_check"},
                headers={"X-AATS-API-Key": "write-key"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(read_allowed.status_code, 200)
        self.assertEqual(read_denied_write.status_code, 403)
        self.assertEqual(write_allowed.status_code, 200)
        self.assertEqual(reconciliation_validate.status_code, 200)
        self.assertEqual(reconciliation_validate.json()["validation"]["trigger"], "startup_check")

    async def test_session_login_enforces_viewer_and_operator_roles(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_viewer_username="viewer",
            operator_viewer_password="viewer-pass",
            operator_operator_username="operator",
            operator_operator_password="operator-pass",
        )
        app = self._app(runtime)
        with TestClient(app) as viewer_client:
            login = viewer_client.post("/auth/login", json={"username": "viewer", "password": "viewer-pass"})
            health = viewer_client.get("/system/health")
            halt_denied = viewer_client.post("/system/halt", json={"reason": "viewer_should_fail"})
            logout = viewer_client.post("/auth/logout")

        with TestClient(app) as operator_client:
            login_operator = operator_client.post("/auth/login", json={"username": "operator", "password": "operator-pass"})
            halt_allowed = operator_client.post("/system/halt", json={"reason": "operator_should_work"})
            session = operator_client.get("/auth/session")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(halt_denied.status_code, 403)
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(login_operator.status_code, 200)
        self.assertEqual(halt_allowed.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json()["authenticated"])
        self.assertEqual(session.json()["role"], "operator")

    async def test_session_login_rejects_invalid_credentials(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_admin_username="admin",
            operator_admin_password="correct-pass",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "admin", "password": "wrong-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_operator_write_is_denied_without_auth_by_default(self) -> None:
        runtime = await self._runtime(operator_unsafe_write_without_auth=False)
        app = self._app(runtime)
        with TestClient(app) as client:
            denied = client.post("/system/halt", json={"reason": "unauthenticated_write"})

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_write_auth_required")

    async def test_operator_histories_are_persisted_for_blockers_and_replay(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            client.get("/system/health")
            blockers = client.get("/system/blockers").json()
            decision_id = client.get("/decision/latest").json()["decision_id"]
            replay_validation = client.post(f"/replay/validate/{decision_id}").json()
            replay_recent = client.get("/replay/recent-validations").json()

        self.assertTrue(blockers["recent_history"])
        self.assertEqual(replay_validation["decision_id"], decision_id)
        self.assertTrue(replay_recent["validations"])

    async def test_system_recovery_and_rebaseline_endpoints_expose_operator_recovery_flow(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            recovery_before = client.get("/system/recovery").json()
            rebaseline = client.post("/system/rebaseline", json={"reason": "accept_exchange_state"}).json()
            recovery_after = client.get("/system/recovery").json()

        self.assertIn("recovery_state", recovery_before["recovery"])
        self.assertEqual(rebaseline["status"], "rebaseline_completed")
        self.assertEqual(recovery_after["recovery"]["recovery_state"], "rebaseline_completed")
        self.assertTrue(recovery_after["recovery"]["resume_eligible"])
        self.assertIsNotNone(recovery_after["recovery"]["last_rebaseline_action"])

    async def _runtime(self, **overrides):
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "operator_unsafe_write_without_auth": True,
                **overrides,
            }
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )
        return runtime

    @staticmethod
    def _app(runtime) -> FastAPI:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(router)
        app.state.runtime = runtime
        return app


if __name__ == "__main__":
    unittest.main()
