from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.storage.session import create_database_runtime


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for Postgres integration tests")
class TestPhase1ShadowPostgres(unittest.IsolatedAsyncioTestCase):
    async def test_phase1_shadow_routes_and_review_history_work_against_postgres_schema(self) -> None:
        admin_engine, schema_name, scoped_url = self._schema_database_url()
        runtime = None
        try:
            runtime = await build_runtime(
                AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "paper_live",
                        "market_data_backend": "demo",
                        "execution_backend": "paper",
                        "account_backend": "disabled",
                        "account_read_enabled": False,
                        "storage_mode": "postgres",
                        "database_url": scoped_url,
                        "database_auto_create_schema": False,
                        "database_single_runtime_guard_enabled": False,
                        "operator_unsafe_write_without_auth": True,
                        "event_persistence_mode": "strict",
                    }
                )
            )
            await runtime.market_gateway.run_local_publisher(
                symbol=runtime.settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )
            runtime.execution_repo.save_order_state(
                OrderState(
                    decision_id="decision_pg_shadow",
                    intent_id="intent_pg_shadow",
                    symbol=runtime.settings.default_symbol,
                    client_order_id="cl_pg_shadow_1",
                    venue="PAPER",
                    status="CREATED",
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
            app = FastAPI()
            app.include_router(auth_router)
            app.include_router(router)
            app.state.runtime = runtime

            with TestClient(app) as client:
                shadow_before = client.get("/system/shadow")
                blocker_control = client.get("/system/blocker-control").json()
                action = client.post(
                    "/system/blocker-actions/acknowledge-phase1-shadow",
                    json={
                        "panel_version": blocker_control["panel_version"],
                        "blocker": "phase1_shadow_lagging",
                        "reason": "postgres_phase1_shadow_review",
                    },
                )
                shadow_after = client.get("/system/shadow")
                history = client.get("/system/shadow/history?limit=10")

            self.assertEqual(shadow_before.status_code, 200)
            self.assertEqual(action.status_code, 200)
            self.assertEqual(shadow_after.status_code, 200)
            self.assertEqual(history.status_code, 200)

            before_payload = shadow_before.json()
            after_payload = shadow_after.json()
            history_payload = history.json()

            self.assertEqual(before_payload["status"], "lagging")
            self.assertTrue(before_payload["review_recommended"])
            self.assertEqual(action.json()["action_id"], "acknowledge-phase1-shadow")
            self.assertEqual(after_payload["latest_review_action"]["action"], "phase1_shadow_review")
            self.assertEqual(history_payload["history"][0]["entry_type"], "review")
            self.assertEqual(history_payload["history"][0]["details"]["lag"]["order_backlog"], 1)
        finally:
            if runtime is not None:
                runtime.database_runtime.dispose() if runtime.database_runtime is not None else None
            self._drop_schema(admin_engine, schema_name)

    @staticmethod
    def _schema_database_url() -> tuple[object, str, str]:
        base_url = make_url(os.environ["AATS_DATABASE_URL"])
        schema_name = f"aats_test_{uuid.uuid4().hex[:12]}"
        admin_engine = create_engine(base_url.render_as_string(hide_password=False), future=True)
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        query = dict(base_url.query)
        existing_options = query.get("options")
        search_path_option = f"-csearch_path={schema_name}"
        query["options"] = f"{existing_options} {search_path_option}".strip() if existing_options else search_path_option
        scoped_url = base_url.set(query=query).render_as_string(hide_password=False)
        runtime = create_database_runtime(scoped_url)
        try:
            TestPhase1ShadowPostgres._apply_migrations(runtime)
        finally:
            runtime.dispose()
        return admin_engine, schema_name, scoped_url

    @staticmethod
    def _apply_migrations(runtime) -> None:
        migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
        with runtime.engine.begin() as connection:
            raw_connection = connection.connection
            with raw_connection.cursor() as cursor:
                for migration_path in sorted(migrations_dir.glob("*.sql")):
                    cursor.execute(migration_path.read_text(encoding="utf-8"))

    @staticmethod
    def _drop_schema(admin_engine, schema_name: str) -> None:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()
