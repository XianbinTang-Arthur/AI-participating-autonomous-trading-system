from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.schemas.exchange import AccountBaselineSnapshot, ExchangeAccountSnapshot, ExchangeBalance, ExchangeOpenOrder
from aats.events import topics
from aats.schemas.operator import ExecutionErrorSummary
from aats.schemas.operator import OperatorUserRecord
from aats.services.operator.passwords import hash_password


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
            blocker_history = client.get("/system/blocker-history?limit=5")
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
        blocker_history_payload = blocker_history.json()
        metrics_payload = metrics.json()

        self.assertIn("overall_status", health_payload)
        self.assertIn("subsystems", health_payload)
        self.assertIn("storage", health_payload["subsystems"])
        self.assertIn("audit_replay", health_payload["subsystems"])
        self.assertEqual(mode_payload["config_profile"], "local_demo")
        self.assertEqual(mode_payload["market_data_backend"], "demo")
        self.assertEqual(mode_payload["execution_backend"], "paper")
        self.assertEqual(mode_payload["ai_operating_mode"], "baseline_only")
        self.assertEqual(mode_payload["runtime_profile"]["name"], "paper_local")
        self.assertEqual(mode_payload["environment_capabilities"]["execution_adapter_kind"], "paper")
        self.assertFalse(mode_payload["policy_profile"]["exchange_submission_allowed_in_principle"])
        self.assertFalse(mode_payload["recovery_policy"]["operator_rebaseline_supported"])
        self.assertFalse(mode_payload["execution_blocked"])
        self.assertTrue(mode_payload["submit_blocked"])
        self.assertIn("local_demo_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIn("paper_execution_has_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIsNone(mode_payload["blocked_reason"])
        self.assertEqual(runtime_payload["symbols"], ["BTC-USDT"])
        self.assertEqual(runtime_payload["enabled_timeframes"], ["15m"])
        self.assertGreaterEqual(runtime_payload["uptime_seconds"], 0.0)
        self.assertEqual(runtime_payload["runtime_profile"]["name"], "paper_local")
        self.assertEqual(runtime_payload["environment_capabilities"]["execution_route"], "paper_local")
        self.assertIn("baseline_takeover", runtime_payload)
        self.assertIn("decision_cycle_count", metrics_payload)
        self.assertIn("recent_execution_errors", metrics_payload)
        self.assertIn("exposure_summary", metrics_payload)
        self.assertIsInstance(blockers_payload["blockers"], list)
        self.assertTrue(any(item["submit_only"] for item in blockers_payload["blockers"]))
        self.assertIn("history", blocker_history_payload)
        self.assertIn("total_available", blocker_history_payload)
        self.assertIn("has_more", blocker_history_payload)
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
            risk_recent = client.get("/risk/recent?limit=2").json()
            policy_latest = client.get("/policy/latest").json()
            policy_recent = client.get("/policy/recent?limit=2").json()
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
            replay_recent = client.get("/replay/recent-validations?limit=5").json()

        self.assertIsNotNone(decision_id)
        self.assertEqual(decision_detail["decision_id"], decision_id)
        self.assertTrue(recent_decisions["decisions"])
        self.assertIn("total_available", recent_decisions)
        self.assertIn("has_more", recent_decisions)
        self.assertEqual(risk_latest["decision_id"], decision_id)
        self.assertIn("total_available", risk_recent)
        self.assertIn("has_more", risk_recent)
        self.assertEqual(policy_latest["decision_id"], decision_id)
        self.assertIn("total_available", policy_recent)
        self.assertIn("has_more", policy_recent)
        self.assertIsNotNone(portfolio_latest["portfolio"])
        self.assertTrue(portfolio_history["snapshots"])
        self.assertIn("local_balances", balances)
        self.assertIn("local_positions", positions)
        self.assertTrue(orders_recent["orders"])
        self.assertIn("total_available", orders_recent)
        self.assertIn("has_more", orders_recent)
        self.assertEqual(order_detail["order"]["client_order_id"], latest_order_id)
        self.assertTrue(fills_recent["fills"])
        self.assertIn("total_available", fills_recent)
        self.assertIn("has_more", fills_recent)
        self.assertEqual(fill_detail["fill"]["fill_id"], latest_fill_id)
        self.assertIsNotNone(execution_latest["latest_order"])
        self.assertIsNotNone(reconciliation_latest["reconciliation"])
        self.assertIn("mismatch_categories", reconciliation_latest["mismatch_summary"])
        self.assertTrue(reconciliation_recent["reconciliations"])
        self.assertIn("total_available", reconciliation_recent)
        self.assertIn("has_more", reconciliation_recent)
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
        self.assertTrue(replay_recent["validations"])
        self.assertIn("total_available", replay_recent)
        self.assertIn("has_more", replay_recent)

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

    async def test_manual_halt_marks_recovery_as_resume_blocked(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            recovery = client.get("/system/recovery")

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.json()["recovery"]["recovery_state"], "resume_blocked")

    async def test_resume_ignores_kill_switch_as_the_only_blocker(self) -> None:
        runtime = await self._runtime()
        query = runtime  # keep runtime in scope for intent clarity
        app = self._app(query)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            resumed = client.post("/system/resume", json={"reason": "operator_test_resume"})

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["halted"])
        self.assertIn(resumed.json()["status"], {"resumed", "already_resumed"})

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

    async def test_memory_storage_auth_surface_is_not_reported_as_database_backed(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
        )
        runtime.operator_repo.save_user(
            OperatorUserRecord(
                username="admin",
                password_hash=hash_password("secret"),
                role="admin",
            )
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            providers = client.get("/auth/providers")
            login = client.post("/auth/login", json={"username": "admin", "password": "secret"})
            session = client.get("/auth/session")

        self.assertEqual(providers.status_code, 200)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertFalse(providers.json()["database_backed"])
        self.assertFalse(providers.json()["runtime_profile_control_enabled"])
        self.assertFalse(session.json()["database_backed"])

    async def test_session_login_enforces_viewer_and_operator_roles(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("viewer", "viewer-pass"), ("operator", "operator-pass")],
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
            operator_users=[("admin", "correct-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "admin", "password": "wrong-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_disabled_database_operator_account_cannot_log_in(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "correct-pass")],
        )
        runtime.operator_repo.save_user(
            OperatorUserRecord(
                username="disabled-user",
                password_hash=hash_password("disabled-pass"),
                role="operator",
                enabled=False,
            )
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "disabled-user", "password": "disabled-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_admin_can_manage_operator_users_and_audit_login_and_crud_actions(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            users = client.get("/auth/users")
            created = client.post(
                "/auth/users",
                json={
                    "username": "viewer2",
                    "password": "viewer-pass",
                    "role": "viewer",
                    "enabled": True,
                },
            )
            updated = client.patch(
                "/auth/users/viewer2",
                json={"role": "operator", "enabled": False},
            )
            deleted = client.delete("/auth/users/viewer2")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(users.status_code, 200)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(deleted.status_code, 200)

        stored_admin = runtime.operator_repo.get_by_username("admin")
        self.assertIsNotNone(stored_admin)
        self.assertIsNotNone(stored_admin.last_login_at)

        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        login_action = next(item for item in reversed(actions) if item["action"] == "login")
        create_action = next(item for item in reversed(actions) if item["action"] == "user_create")
        update_action = next(item for item in reversed(actions) if item["action"] == "user_update")
        delete_action = next(item for item in reversed(actions) if item["action"] == "user_delete")

        self.assertEqual(login_action["actor_identity"], "admin")
        self.assertEqual(login_action["actor_role"], "admin")
        self.assertEqual(create_action["details"]["target_username"], "viewer2")
        self.assertEqual(update_action["details"]["target_username"], "viewer2")
        self.assertEqual(delete_action["details"]["target_username"], "viewer2")

    async def test_runtime_profile_routes_report_env_switch_mode(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            listing = client.get("/runtime-profiles")
            created = client.post("/runtime-profiles/drafts", json={"profile_label": "derivatives primary"})

        self.assertEqual(login.status_code, 200)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["profile_source"], "env_fallback")
        self.assertFalse(listing.json()["management_enabled"])
        self.assertEqual(created.status_code, 409)
        self.assertEqual(created.json()["detail"], "runtime_profile_control_disabled")

    async def test_runtime_profile_stage_routes_are_disabled_in_env_switch_mode(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_runtime_profile",
                intent_id="intent_runtime_profile",
                symbol="BTC-USDT",
                client_order_id="order_runtime_profile",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            canceled = client.post("/runtime-profiles/pending/cancel")
            restart = client.post("/runtime-profiles/restart")

        self.assertEqual(canceled.status_code, 409)
        self.assertEqual(canceled.json()["detail"], "runtime_profile_control_disabled")
        self.assertEqual(restart.status_code, 409)
        self.assertEqual(restart.json()["detail"], "runtime_profile_control_disabled")

    async def test_operator_user_management_requires_admin_and_preserves_last_admin(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass"), ("operator", "operator-pass")],
            operator_write_api_key="write-key",
        )
        app = self._app(runtime)

        with TestClient(app) as operator_client:
            login = operator_client.post("/auth/login", json={"username": "operator", "password": "operator-pass"})
            denied = operator_client.get("/auth/users")

        with TestClient(app) as admin_client:
            admin_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self_disable = admin_client.patch("/auth/users/admin", json={"enabled": False})
            self_delete = admin_client.delete("/auth/users/admin")

        with TestClient(app) as api_key_client:
            write_allowed = api_key_client.get("/auth/users", headers={"X-AATS-API-Key": "write-key"})
            last_admin_disable = api_key_client.patch(
                "/auth/users/admin",
                json={"enabled": False},
                headers={"X-AATS-API-Key": "write-key"},
            )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_admin_access_required")
        self.assertEqual(self_disable.status_code, 409)
        self.assertEqual(self_disable.json()["detail"], "operator_self_disable_forbidden")
        self.assertEqual(self_delete.status_code, 409)
        self.assertEqual(self_delete.json()["detail"], "operator_self_delete_forbidden")
        self.assertEqual(write_allowed.status_code, 200)
        self.assertEqual(last_admin_disable.status_code, 409)
        self.assertEqual(last_admin_disable.json()["detail"], "operator_last_admin_required")

    async def test_session_is_revoked_immediately_when_database_user_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass"), ("operator", "operator-pass")],
            operator_write_api_key="write-key",
        )
        operator_user = runtime.operator_repo.get_by_username("operator")
        self.assertIsNotNone(operator_user)
        runtime.operator_repo.save_user(operator_user.model_copy(update={"role": "admin"}))

        app = self._app(runtime)
        with TestClient(app) as session_client:
            login = session_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self.assertEqual(login.status_code, 200)
            before_disable = session_client.get("/auth/whoami")
            with TestClient(app) as api_key_client:
                disabled = api_key_client.patch(
                    "/auth/users/admin",
                    json={"enabled": False},
                    headers={"X-AATS-API-Key": "write-key"},
                )
            after_disable = session_client.get("/auth/whoami")

        self.assertEqual(before_disable.status_code, 200)
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(after_disable.status_code, 401)
        self.assertEqual(after_disable.json()["detail"], "operator_auth_required")

    async def test_operator_write_is_denied_without_auth_by_default(self) -> None:
        runtime = await self._runtime(operator_unsafe_write_without_auth=False)
        app = self._app(runtime)
        with TestClient(app) as client:
            denied = client.post("/system/halt", json={"reason": "unauthenticated_write"})

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_write_auth_required")

    async def test_unauthenticated_session_remains_anonymous_when_browser_auth_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=False,
            operator_users=[("admin", "solo-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            session = client.get("/auth/session")

        self.assertEqual(session.status_code, 200)
        payload = session.json()
        self.assertFalse(payload["authenticated"])
        self.assertIsNone(payload["identity"])
        self.assertEqual(payload["role"], "anonymous")
        self.assertEqual(payload["auth_source"], "anonymous")

    async def test_auth_providers_no_longer_expose_runtime_bootstrap_state(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            providers = client.get("/auth/providers")

        self.assertEqual(providers.status_code, 200)
        payload = providers.json()
        self.assertEqual(payload["configured_roles"], [])
        self.assertFalse(payload["runtime_profile_control_enabled"])
        self.assertNotIn("bootstrap_pending", payload)

    async def test_unauthenticated_session_is_anonymous_without_stored_operator_users(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=False,
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            session = client.get("/auth/session")

        self.assertEqual(session.status_code, 200)
        payload = session.json()
        self.assertIsNone(payload["identity"])
        self.assertEqual(payload["role"], "anonymous")
        self.assertEqual(payload["auth_source"], "anonymous")

    async def test_sqlite_backed_operator_account_persists_and_allows_login(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = (Path(temp_dir) / "aats_auth.db").resolve().as_posix()
            seed_settings = AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "postgres",
                    "database_url": f"sqlite+pysqlite:///{database_path}",
                    "database_auto_create_schema": True,
                    "event_persistence_mode": "strict",
                    "enabled_decision_timeframes": ("15m",),
                    "operator_auth_enabled": False,
                }
            )
            runtime = await build_runtime(seed_settings)
            runtime.operator_repo.save_user(
                OperatorUserRecord(
                    username="admin",
                    password_hash=hash_password("correct-pass"),
                    role="admin",
                )
            )
            await runtime.market_gateway.run_local_publisher(
                symbol=seed_settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )
            self.assertEqual(runtime.operator_repo.count(), 1)
            if runtime.database_runtime is not None:
                runtime.database_runtime.dispose()

            settings = seed_settings.model_copy(
                update={
                    "operator_auth_enabled": True,
                    "operator_session_secret": "session-secret",
                }
            )
            recovered_runtime = await build_runtime(settings)
            app = self._app(recovered_runtime)
            with TestClient(app) as client:
                providers = client.get("/auth/providers")
                login = client.post("/auth/login", json={"username": "admin", "password": "correct-pass"})

            self.assertEqual(providers.status_code, 200)
            self.assertEqual(providers.json()["stored_user_count"], 1)
            self.assertFalse(providers.json()["runtime_profile_control_enabled"])
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.json()["identity"], "admin")
            if recovered_runtime.database_runtime is not None:
                recovered_runtime.database_runtime.dispose()

    async def test_database_backed_session_auth_requires_preexisting_admin_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = (Path(temp_dir) / "aats_auth.db").resolve().as_posix()
            settings = AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "postgres",
                    "database_url": f"sqlite+pysqlite:///{database_path}",
                    "database_auto_create_schema": True,
                    "event_persistence_mode": "strict",
                    "enabled_decision_timeframes": ("15m",),
                    "operator_auth_enabled": True,
                    "operator_session_secret": "session-secret",
                }
            )

            with self.assertRaisesRegex(ValueError, "operator_session_auth_requires_enabled_admin_user"):
                await build_runtime(settings)

    async def test_mode_hot_swap_is_rejected_and_cancel_is_operator_audited(self) -> None:
        runtime = await self._runtime(
            operator_users=[("admin", "solo-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            mode_change = client.post("/system/mode", json={"mode": "guarded_live"})
            latest_order_id = client.get("/orders/latest").json()["order"]["client_order_id"]
            cancel = client.post(
                f"/orders/{latest_order_id}/cancel",
                json={"reason": "ui_cancel_test"},
            )

        self.assertEqual(mode_change.status_code, 409)
        self.assertEqual(
            mode_change.json()["detail"],
            "runtime_mode_hot_swap_not_supported_restart_required",
        )
        self.assertEqual(cancel.status_code, 200)
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        cancel_action = next(item for item in reversed(actions) if item["action"] == "cancel_order")
        self.assertIsNone(cancel_action["actor_identity"])
        self.assertEqual(cancel_action["actor_role"], "anonymous")
        self.assertEqual(cancel_action["auth_source"], "anonymous")
        self.assertEqual(cancel_action["reason"], "ui_cancel_test")
        self.assertEqual(cancel_action["order_id"], latest_order_id)

    async def test_stuck_submission_can_be_resolved_after_restart_when_exchange_confirms_absence(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
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
            account_mode="cross",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        stale_ts = utc_now() - timedelta(minutes=10)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_stuck",
                intent_id="intent_restart_stuck",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_stuck",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=stale_ts,
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_stuck/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )
            detail = client.get("/orders/cl_restart_stuck")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["order"]["status"], "FAILED")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["stuck_submission_resolution"]["eligible"])
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        resolution_action = next(item for item in reversed(actions) if item["action"] == "resolve_stuck_submission")
        self.assertEqual(resolution_action["reason"], "ui_resolve_stuck_submission")
        self.assertEqual(resolution_action["order_id"], "cl_restart_stuck")

    async def test_stuck_submission_resolution_updates_audit_record_when_decision_audit_exists(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
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
        stale_ts = utc_now() - timedelta(minutes=10)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_stuck_audit",
                intent_id="intent_restart_stuck_audit",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_stuck_audit",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=stale_ts,
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id="decision_restart_stuck_audit",
                decision_context_ref="evt_decision_context_seed",
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_stuck_audit/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )

        self.assertEqual(response.status_code, 200)
        audit = runtime.audit_repo.get("decision_restart_stuck_audit")
        self.assertIsNotNone(audit)
        self.assertEqual(len(audit.order_state_refs), 1)
        order_update = runtime.event_store.get(audit.order_state_refs[0])
        self.assertIsNotNone(order_update)
        self.assertEqual(order_update.payload["status"], "FAILED")

    async def test_stuck_submission_resolution_is_rejected_when_exchange_still_has_order(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
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
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT",
                    client_order_id="cl_restart_live",
                    exchange_order_id="ord_live",
                    side="buy",
                    order_type="market",
                    status="live",
                    quantity=0.1,
                )
            ],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_live",
                intent_id="intent_restart_live",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_live",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=10),
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_live/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "stuck_submission_resolution_blocked:exchange_order_still_open",
        )

    async def test_orders_recent_returns_latest_orders_first(self) -> None:
        runtime = await self._runtime()
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_old_recent",
                intent_id="intent_old_recent",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_old_recent",
                venue="PAPER",
                status="FAILED",
                submission_mode="paper_local",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=5),
                requested_qty=1.0,
                filled_qty=0.0,
                remaining_qty=1.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_new_recent",
                intent_id="intent_new_recent",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_new_recent",
                venue="PAPER",
                status="SUBMITTING",
                submission_mode="paper_local",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=1.0,
                filled_qty=0.0,
                remaining_qty=1.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recent = client.get("/orders/recent?limit=1")
            full_list = client.get("/orders/recent?limit=200").json()
            order_ids = [item["client_order_id"] for item in full_list["orders"]]
            old_index = order_ids.index("order_old_recent")
            next_page = client.get(f"/orders/recent?limit=1&offset={old_index}")

        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.json()["orders"][0]["client_order_id"], "order_new_recent")
        self.assertEqual(recent.json()["offset"], 0)
        self.assertEqual(recent.json()["limit"], 1)
        self.assertTrue(recent.json()["has_more"])
        self.assertLess(order_ids.index("order_new_recent"), order_ids.index("order_old_recent"))
        self.assertEqual(next_page.status_code, 200)
        self.assertEqual(next_page.json()["orders"][0]["client_order_id"], "order_old_recent")
        self.assertEqual(next_page.json()["offset"], old_index)

    async def test_execution_latest_uses_normalized_recovery_view(self) -> None:
        runtime = await self._runtime()
        runtime.kill_switch.halt(reason="operator_test_halt")
        runtime.recovery_status = runtime.recovery_status.model_copy(update={"recovery_state": "normal_operation"})
        app = self._app(runtime)

        with TestClient(app) as client:
            execution = client.get("/execution/latest")
            recovery = client.get("/system/recovery")

        self.assertEqual(execution.status_code, 200)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(
            execution.json()["recovery"]["recovery_state"],
            recovery.json()["recovery"]["recovery_state"],
        )
        self.assertEqual(execution.json()["recovery"]["recovery_state"], "resume_blocked")

    async def test_execution_errors_hide_stale_failures_from_previous_runtime(self) -> None:
        runtime = await self._runtime()
        stale_error = ExecutionErrorSummary(
            subsystem="execution_engine",
            severity="error",
            message="stale_failure",
            decision_id="decision_old",
            order_id="order_old",
            status="FAILED",
            observed_at=utc_now() - timedelta(hours=2),
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key=runtime.settings.default_symbol,
                payload_model=stale_error,
                source_component="execution_engine",
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            execution_errors = client.get("/execution/errors")

        self.assertEqual(execution_errors.status_code, 200)
        self.assertEqual(execution_errors.json()["errors"], [])

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
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
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

    async def test_recovery_view_uses_latest_account_baseline_for_current_scope(self) -> None:
        runtime = await self._runtime()
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["BTC-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["ETH-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery").json()

        self.assertEqual(recovery["latest_account_baseline"]["allowed_symbols"], ["BTC-USDT"])

    async def test_rebaseline_uses_previous_baseline_from_current_scope(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
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

        initial_baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(initial_baseline)
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["ETH-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post("/system/rebaseline", json={"reason": "accept_exchange_state"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["baseline"]["previous_baseline_ref"],
            initial_baseline.event_id,
        )

    async def test_system_rebaseline_is_rejected_for_paper_local_profile(self) -> None:
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

        app = self._app(runtime)
        with TestClient(app) as client:
            response = client.post("/system/rebaseline", json={"reason": "paper_profile_rebaseline"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "rebaseline_not_supported_for_runtime_profile")

    async def _runtime(self, operator_users: list[tuple[str, str]] | None = None, **overrides):
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
        for username, password in operator_users or []:
            role = "admin" if username == "admin" else "operator" if username == "operator" else "viewer"
            runtime.operator_repo.save_user(
                OperatorUserRecord(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
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
