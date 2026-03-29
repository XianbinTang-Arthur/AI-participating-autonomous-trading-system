from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session


class LedgerAccountRepository(Protocol):
    def get_or_create_account(
        self,
        *,
        account_type: str,
        currency: str,
        product_type: str,
        margin_mode: str,
        symbol: str | None,
        created_at: datetime,
    ) -> str:
        ...

    def get_or_create_account_in_session(
        self,
        session: Session,
        *,
        account_type: str,
        currency: str,
        product_type: str,
        margin_mode: str,
        symbol: str | None,
        created_at: datetime,
    ) -> str:
        ...

    def get_account(self, account_id: str) -> dict | None:
        ...

    def list_accounts(
        self,
        *,
        account_type: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[dict]:
        ...

    def list_accounts_in_session(
        self,
        session: Session,
        *,
        account_type: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[dict]:
        ...


class LedgerJournalRepository(Protocol):
    def create_journal(
        self,
        *,
        journal_id: str,
        journal_type: str,
        source_type: str,
        source_id: str,
        status: str,
        created_at: datetime,
        metadata: dict,
    ) -> None:
        ...

    def create_journal_in_session(
        self,
        session: Session,
        *,
        journal_id: str,
        journal_type: str,
        source_type: str,
        source_id: str,
        status: str,
        created_at: datetime,
        metadata: dict,
    ) -> None:
        ...

    def mark_posted(self, journal_id: str, posted_at: datetime) -> None:
        ...

    def mark_posted_in_session(self, session: Session, journal_id: str, posted_at: datetime) -> None:
        ...

    def get_by_source(self, source_type: str, source_id: str) -> dict | None:
        ...

    def get_by_source_in_session(self, session: Session, source_type: str, source_id: str) -> dict | None:
        ...


class LedgerEntryRepository(Protocol):
    def append_entries(self, *, entries: list[dict]) -> None:
        ...

    def append_entries_in_session(self, session: Session, *, entries: list[dict]) -> None:
        ...

    def entries_for_journal(self, journal_id: str) -> list[dict]:
        ...

    def balance_by_account(self, account_id: str) -> Decimal:
        ...

    def balance_by_account_in_session(self, session: Session, account_id: str) -> Decimal:
        ...


class SettlementRepository(Protocol):
    def create_settlement(
        self,
        *,
        settlement_id: str,
        fill_id: str,
        order_id: str,
        state: str,
        created_at: datetime,
    ) -> None:
        ...

    def create_settlement_in_session(
        self,
        session: Session,
        *,
        settlement_id: str,
        fill_id: str,
        order_id: str,
        state: str,
        created_at: datetime,
    ) -> None:
        ...

    def attach_journal(self, *, settlement_id: str, journal_id: str, posted_at: datetime) -> None:
        ...

    def attach_journal_in_session(
        self,
        session: Session,
        *,
        settlement_id: str,
        journal_id: str,
        posted_at: datetime,
    ) -> None:
        ...

    def get_by_fill_id(self, fill_id: str) -> dict | None:
        ...

    def get_by_fill_id_in_session(self, session: Session, fill_id: str) -> dict | None:
        ...
