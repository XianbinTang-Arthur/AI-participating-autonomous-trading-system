from __future__ import annotations

import os
import unittest

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError as SAOperationalError

from aats.schemas.common import utc_now
from aats.storage.command_outbox_repo_postgres import PostgresCommandOutboxRepositoryV2
from aats.storage.execution_command_repo_postgres import PostgresExecutionCommandRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.inbox_repo_postgres import PostgresExternalInboxRepository
from aats.storage.ledger_repo_postgres import (
    PostgresLedgerAccountRepository,
    PostgresLedgerEntryRepository,
    PostgresLedgerJournalRepository,
    PostgresSettlementRepository,
)
from aats.storage.reservation_repo_postgres import PostgresReservationRepository
from tests.support.postgres import temporary_postgres_runtime


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestPhase1StorageScaffold(unittest.TestCase):
    def test_phase1_schema_tables_exist_and_repos_instantiate(self) -> None:
        try:
            with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):

                tables = set(inspect(runtime.engine).get_table_names())
                self.assertTrue(
                    {
                        "execution_orders",
                        "execution_order_state_history",
                        "execution_commands",
                        "execution_fills",
                        "ledger_accounts",
                        "ledger_journals",
                        "ledger_entries",
                        "reservations",
                        "settlements",
                        "external_event_inbox",
                        "command_outbox",
                    }.issubset(tables)
                )

                PostgresExecutionOrderRepository(runtime.session_factory)
                PostgresExecutionOrderHistoryRepository(runtime.session_factory)
                PostgresExecutionCommandRepository(runtime.session_factory)
                PostgresExecutionFillRepositoryV2(runtime.session_factory)
                PostgresReservationRepository(runtime.session_factory)
                PostgresLedgerAccountRepository(runtime.session_factory)
                PostgresLedgerJournalRepository(runtime.session_factory)
                PostgresLedgerEntryRepository(runtime.session_factory)
                PostgresSettlementRepository(runtime.session_factory)
                PostgresExternalInboxRepository(runtime.session_factory)
                PostgresCommandOutboxRepositoryV2(runtime.session_factory)
        except SAOperationalError:
            self.skipTest("Postgres 不可达")

    def test_external_inbox_duplicate_dedupe_key_is_handled_idempotently(self) -> None:
        try:
            with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                repo = PostgresExternalInboxRepository(runtime.session_factory)

                first = repo.save_incoming(
                    inbox_id="inbox_idem_1",
                    source_system="okx_webhook",
                    dedupe_key="okx:bill:1",
                    payload={"bill_id": "1"},
                    received_at=utc_now(),
                )
                second = repo.save_incoming(
                    inbox_id="inbox_idem_2",
                    source_system="okx_webhook",
                    dedupe_key="okx:bill:1",
                    payload={"bill_id": "1"},
                    received_at=utc_now(),
                )

                self.assertTrue(first)
                self.assertFalse(second)
                rows = repo.unprocessed(limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["inbox_id"], "inbox_idem_1")
        except SAOperationalError:
            self.skipTest("Postgres 不可达")
