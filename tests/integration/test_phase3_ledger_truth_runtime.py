from __future__ import annotations

import os
import unittest
from decimal import Decimal

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent
from tests.support.postgres import temporary_postgres_url


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestPhase3LedgerTruthRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_uses_ledger_truth_for_portfolio_snapshot_when_enabled(self) -> None:
        runtime = None
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
                        "portfolio_ledger_truth_enabled": True,
                    }
                )
            )
            seeded_snapshot = runtime.market_gateway.normalizer.normalize(
                runtime.market_gateway._build_local_payload(runtime.settings.default_symbol)  # type: ignore[attr-defined]
            )
            runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = seeded_snapshot  # type: ignore[attr-defined]
            runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now()  # type: ignore[attr-defined]
            intent = OrderIntent(
                intent_id="intent_phase3_runtime_1",
                decision_id="decision_phase3_runtime_1",
                symbol="BTC-USDT",
                side="buy",
                quantity=Decimal("0.001"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase3_runtime_1",
            )
            runtime.audit_repo.upsert(
                DecisionAuditRecord(
                    decision_id=intent.decision_id,
                    decision_context_ref="evt_phase3_runtime_1",
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

            snapshot = runtime.portfolio_repo.latest()
            fills = runtime.execution_repo.fills()
            self.assertEqual(len(fills), 1)
            fill = fills[0]
            expected_usdt = Decimal("10000") - (fill.fill_qty * fill.fill_price) - fill.fee_amount
            outcome = runtime.fill_outcome_repo.get_outcome(fills[0].fill_id)
            self.assertIsNotNone(snapshot)
            self.assertIsNotNone(outcome)
            self.assertEqual(snapshot.source_fill_id, fill.fill_id)
            self.assertEqual(snapshot.balances["BTC"], Decimal("0.001"))
            self.assertEqual(snapshot.balances["USDT"], expected_usdt)
            self.assertEqual(runtime.portfolio_service.state.balances["BTC"], Decimal("0.001"))
            self.assertEqual(
                runtime.portfolio_service.state.balances["USDT"].quantize(Decimal("0.00000001")),
                expected_usdt.quantize(Decimal("0.00000001")),
            )
            available_accounts = runtime.ledger_account_repo.list_accounts(
                account_type="cash_available",
                product_type="spot",
                margin_mode="cash",
            )
            balances = {
                row["currency"]: runtime.ledger_entry_repo.balance_by_account(str(row["account_id"]))
                for row in available_accounts
            }
            self.assertEqual(balances["BTC"], Decimal("0.001"))
            self.assertEqual(
                balances["USDT"].quantize(Decimal("0.00000001")),
                expected_usdt.quantize(Decimal("0.00000001")),
            )
        if runtime is not None and runtime.database_runtime is not None:
            runtime.database_runtime.dispose()
