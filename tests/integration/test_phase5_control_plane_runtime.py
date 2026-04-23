from __future__ import annotations

import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent
from aats.storage.sqlalchemy_models import OrderStateModel
from tests.support.postgres import temporary_postgres_url


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestPhase5ControlPlaneRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_phase5_control_plane_reads_execution_and_ledger_truth(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
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
                        "database_url": database_url,
                        "database_auto_create_schema": True,
                        "database_single_runtime_guard_enabled": False,
                        "event_persistence_mode": "strict",
                        "execution_command_flow_enabled": True,
                        "portfolio_ledger_truth_enabled": True,
                        "recovery_reconciliation_execution_ledger_enabled": True,
                        "operator_control_plane_execution_ledger_enabled": True,
                        "operator_auth_enabled": False,
                    }
                )
            )
            try:
                seeded_snapshot = runtime.market_gateway.normalizer.normalize(
                    runtime.market_gateway._build_local_payload(runtime.settings.default_symbol)  # type: ignore[attr-defined]
                )
                runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = seeded_snapshot  # type: ignore[attr-defined]
                runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now()  # type: ignore[attr-defined]
                intent = OrderIntent(
                    intent_id="intent_phase5_control_1",
                    decision_id="decision_phase5_control_1",
                    symbol="BTC-USDT",
                    side="buy",
                    quantity=0.001,
                    execution_style="exchange",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=False,
                    close_only=False,
                    idempotency_key="phase5_control_1",
                    market_snapshot_ref="mkt_snap_phase5_control_1",
                    feature_snapshot_ref="feat_snap_phase5_control_1",
                    portfolio_snapshot_ref="port_snap_phase5_control_1",
                    health_snapshot_ref="health_snap_phase5_control_1",
                )
                runtime.audit_repo.upsert(
                    DecisionAuditRecord(
                        decision_id=intent.decision_id,
                        decision_context_ref="evt_phase5_control_1",
                    )
                )
                await runtime.order_manager.handle_order_intent(
                    {
                        "topic": topics.ORDER_INTENTS,
                        "key": intent.symbol,
                        "payload": build_envelope(
                            topic=topics.ORDER_INTENTS,
                            key=intent.symbol,
                            payload_model=intent,
                            source_component="test",
                        ).model_dump(mode="json"),
                    }
                )
                self.assertIsNotNone(runtime.execution_command_processor)
                await runtime.execution_command_processor.process_pending()
                with runtime.database_runtime.session_factory() as session:
                    session.execute(delete(OrderStateModel).where(OrderStateModel.client_order_id == "clphase5_control_1"))
                    session.commit()

                app = FastAPI()
                app.include_router(auth_router)
                app.include_router(router)
                app.state.runtime = runtime

                with TestClient(app) as client:
                    portfolio_latest = client.get("/portfolio/latest")
                    balances = client.get("/balances")
                    orders_recent = client.get("/orders/recent")
                    order_detail = client.get("/orders/clphase5_control_1")
                    fills_recent = client.get("/fills/recent")
                    system_runtime = client.get("/system/runtime")
                    halt_attempt = client.post("/system/halt", json={"reason": "phase5_should_block_anon"})

                self.assertEqual(portfolio_latest.status_code, 200)
                self.assertEqual(balances.status_code, 200)
                self.assertEqual(orders_recent.status_code, 200)
                self.assertEqual(order_detail.status_code, 200)
                self.assertEqual(fills_recent.status_code, 200)
                self.assertEqual(system_runtime.status_code, 200)
                self.assertEqual(halt_attempt.status_code, 403)

                portfolio_payload = portfolio_latest.json()
                balances_payload = balances.json()
                orders_payload = orders_recent.json()
                order_detail_payload = order_detail.json()
                fills_payload = fills_recent.json()
                runtime_payload = system_runtime.json()

                self.assertEqual(portfolio_payload["truth_source"], "ledger_backed_snapshot")
                self.assertEqual(balances_payload["truth_source"], "ledger_accounts")
                self.assertEqual(orders_payload["truth_source"], "execution_order_repo")
                self.assertEqual(fills_payload["truth_source"], "execution_fill_repo_v2")
                self.assertTrue(orders_payload["orders"])
                self.assertTrue(fills_payload["fills"])
                self.assertEqual(orders_payload["orders"][0]["truth_source"], "execution_order_repo")
                self.assertNotEqual(
                    order_detail_payload["stuck_submission_resolution"]["reason_code"],
                    "phase5_order_state_unavailable",
                )
                self.assertEqual(fills_payload["fills"][0]["truth_source"], "execution_fill_repo_v2")

                # Execution truth exposure: phase5 control-plane order/fill payloads must
                # surface execution_style + 4 snapshot refs (and raw_exchange on fill rows)
                # as flat top-level keys so callers don't need to dig through raw_payload.
                # Real truth shape along this paper/phase5 path:
                #   - Order raw_payload 最终是 OrderState.model_dump 覆盖写入的结果；
                #     OrderState 继承了 intent 的 4 个 snapshot_ref，所以 refs 在 order
                #     行上必须等于 intent 播种的值；而 OrderState 不携带 execution_style，
                #     所以 execution_style 在 paper 路径下合法为 None，契约是 "key 在顶层
                #     出现"，不是 "值一定非空"。
                #   - Paper 路径合成 FillEvent 不携带 refs / 不带交易所原始 payload，
                #     所以 4 refs 与 raw_exchange 都可能为 None（真实 truth），契约是
                #     "key 在顶层出现"。
                expected_refs = {
                    "market_snapshot_ref": "mkt_snap_phase5_control_1",
                    "feature_snapshot_ref": "feat_snap_phase5_control_1",
                    "portfolio_snapshot_ref": "port_snap_phase5_control_1",
                    "health_snapshot_ref": "health_snap_phase5_control_1",
                }

                first_order = orders_payload["orders"][0]
                self.assertIn("execution_style", first_order)
                for ref_key, ref_value in expected_refs.items():
                    self.assertEqual(first_order[ref_key], ref_value)

                detail_order = order_detail_payload["order"]
                self.assertIn("execution_style", detail_order)
                for ref_key, ref_value in expected_refs.items():
                    self.assertEqual(detail_order[ref_key], ref_value)

                first_fill = fills_payload["fills"][0]
                for ref_key in expected_refs:
                    self.assertIn(ref_key, first_fill)
                self.assertIn("raw_exchange", first_fill)
                self.assertTrue(
                    first_fill["raw_exchange"] is None
                    or isinstance(first_fill["raw_exchange"], dict)
                )
                self.assertTrue(runtime_payload["control_plane"]["phase5_enabled"])
                self.assertTrue(runtime_payload["control_plane"]["auth_hardened"])
                self.assertFalse(runtime_payload["control_plane"]["legacy_layer_authoritative"])
                self.assertEqual(runtime_payload["control_plane"]["truth_consistency_status"], "transitional")
                self.assertIn(
                    "phase5_control_plane_running_without_financial_convergence",
                    runtime_payload["control_plane"]["consistency_warning_codes"],
                )
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()


if __name__ == "__main__":
    unittest.main()
