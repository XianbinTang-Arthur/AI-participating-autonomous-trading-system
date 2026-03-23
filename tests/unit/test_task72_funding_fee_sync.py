from __future__ import annotations

import os
import unittest
from decimal import Decimal

from sqlalchemy import func, select

from aats.services.ledger.funding_fee_sync import LedgerFundingFeeSyncService
from aats.services.ledger.settlement_posting import LedgerSettlementPostingService
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.funding_fee_repo_postgres import PostgresFundingFeeRepository
from aats.storage.ledger_repo_postgres import (
    PostgresLedgerAccountRepository,
    PostgresLedgerEntryRepository,
    PostgresLedgerJournalRepository,
)
from aats.storage.sqlalchemy_models import FundingFeeRecordModel, LedgerEntryModel, LedgerJournalModel
from tests.support.postgres import temporary_postgres_runtime


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask72FundingFeeSync(unittest.TestCase):
    def test_sync_recent_bills_persists_records_and_posts_ledger_effects(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            funding_fee_repo = PostgresFundingFeeRepository(runtime.session_factory)
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            settlement_service = LedgerSettlementPostingService(
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
            )
            settlement_service.ensure_initial_balance(
                currency="USDT",
                amount=Decimal("1000"),
                product_type="derivatives",
                margin_mode="cross",
            )
            service = LedgerFundingFeeSyncService(
                funding_fee_repo=funding_fee_repo,
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
            )

            result = service.sync_recent_bills(
                rows=[
                    {
                        "billId": "bill_fee_expense_1",
                        "type": "8",
                        "subType": "173",
                        "ccy": "USDT",
                        "instId": "BTC-USDT-SWAP",
                        "balChg": "-4.00",
                        "bal": "996.00",
                        "ts": "1700000001000",
                    },
                    {
                        "billId": "bill_fee_income_1",
                        "type": "8",
                        "subType": "174",
                        "ccy": "USDT",
                        "instId": "BTC-USDT-SWAP",
                        "balChg": "1.50",
                        "bal": "997.50",
                        "ts": "1700000002000",
                    },
                ],
                product_type="derivatives",
                margin_mode="cross",
            )

            scope = RuntimeStateScope(
                product_type="derivatives",
                margin_mode="cross",
                allowed_symbols=("BTC-USDT-SWAP",),
                default_symbol="BTC-USDT-SWAP",
            )
            records = funding_fee_repo.records_for_scope(scope=scope)
            self.assertEqual(result.funding_fee_count, 2)
            self.assertEqual(result.posted_count, 2)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].ledger_posting_state, "POSTED")
            self.assertEqual(records[0].funding_direction, "expense")
            self.assertEqual(records[1].funding_direction, "income")
            self.assertEqual(
                settlement_service.available_balances(product_type="derivatives", margin_mode="cross")["USDT"],
                Decimal("997.50"),
            )
            self.assertIsNotNone(
                ledger_journal_repo.get_by_source("exchange_funding_fee_bill", "bill_fee_expense_1")
            )
            self.assertIsNotNone(
                ledger_journal_repo.get_by_source("exchange_funding_fee_bill", "bill_fee_income_1")
            )

    def test_sync_recent_bills_is_idempotent_for_duplicate_bill_ids(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            funding_fee_repo = PostgresFundingFeeRepository(runtime.session_factory)
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            service = LedgerFundingFeeSyncService(
                funding_fee_repo=funding_fee_repo,
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
            )
            rows = [
                {
                    "billId": "bill_fee_dup_1",
                    "type": "8",
                    "subType": "173",
                    "ccy": "USDT",
                    "instId": "BTC-USDT-SWAP",
                    "balChg": "-1.00",
                    "bal": "999.00",
                    "ts": "1700000001000",
                }
            ]

            first = service.sync_recent_bills(rows=rows, product_type="derivatives", margin_mode="cross")
            second = service.sync_recent_bills(rows=rows, product_type="derivatives", margin_mode="cross")

            self.assertEqual(first.posted_count, 1)
            self.assertEqual(second.posted_count, 0)
            with runtime.session_factory() as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(FundingFeeRecordModel)), 1)
                self.assertEqual(session.scalar(select(func.count()).select_from(LedgerJournalModel)), 1)
                self.assertEqual(session.scalar(select(func.count()).select_from(LedgerEntryModel)), 2)

    def test_sync_recent_bills_rejects_conflicting_posted_record(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            funding_fee_repo = PostgresFundingFeeRepository(runtime.session_factory)
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            service = LedgerFundingFeeSyncService(
                funding_fee_repo=funding_fee_repo,
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
            )

            service.sync_recent_bills(
                rows=[
                    {
                        "billId": "bill_fee_conflict_1",
                        "type": "8",
                        "subType": "173",
                        "ccy": "USDT",
                        "instId": "BTC-USDT-SWAP",
                        "balChg": "-1.00",
                        "bal": "999.00",
                        "ts": "1700000001000",
                    }
                ],
                product_type="derivatives",
                margin_mode="cross",
            )

            with self.assertRaisesRegex(RuntimeError, "funding_fee_record_conflict"):
                service.sync_recent_bills(
                    rows=[
                        {
                            "billId": "bill_fee_conflict_1",
                            "type": "8",
                            "subType": "173",
                            "ccy": "USDT",
                            "instId": "BTC-USDT-SWAP",
                            "balChg": "-2.00",
                            "bal": "998.00",
                            "ts": "1700000001000",
                        }
                    ],
                    product_type="derivatives",
                    margin_mode="cross",
                )
