from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from sqlalchemy import asc, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import new_id
from aats.storage.sqlalchemy_models import LedgerAccountModel, LedgerEntryModel, LedgerJournalModel, SettlementModel


class PostgresLedgerAccountRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

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
        with self.session_factory() as session:
            account_id = self.get_or_create_account_in_session(
                session,
                account_type=account_type,
                currency=currency,
                product_type=product_type,
                margin_mode=margin_mode,
                symbol=symbol,
                created_at=created_at,
            )
            session.commit()
            return account_id

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
        _acquire_transaction_advisory_lock(
            session,
            "ledger_account",
            account_type,
            currency,
            product_type,
            margin_mode,
            symbol or "<none>",
        )
        row = _find_account(
            session=session,
            account_type=account_type,
            currency=currency,
            product_type=product_type,
            margin_mode=margin_mode,
            symbol=symbol,
        )
        if row is None:
            row = LedgerAccountModel(
                account_id=new_id("lacc"),
                account_type=account_type,
                currency=currency,
                product_type=product_type,
                margin_mode=margin_mode,
                symbol=symbol,
                created_at=created_at,
            )
            session.add(row)
            session.flush()
        return row.account_id

    def get_account(self, account_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(LedgerAccountModel, account_id)
        return _ledger_account_row_to_dict(row) if row is not None else None

    def list_accounts(
        self,
        *,
        account_type: str | None = None,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[dict]:
        query = select(LedgerAccountModel)
        if account_type is not None:
            query = query.where(LedgerAccountModel.account_type == account_type)
        if product_type is not None:
            query = query.where(LedgerAccountModel.product_type == product_type)
        if margin_mode is not None:
            query = query.where(LedgerAccountModel.margin_mode == margin_mode)
        query = query.order_by(
            asc(LedgerAccountModel.currency),
            asc(LedgerAccountModel.account_type),
            asc(LedgerAccountModel.account_id),
        )
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [_ledger_account_row_to_dict(row) for row in rows]


class PostgresLedgerJournalRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

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
        with self.session_factory() as session:
            self.create_journal_in_session(
                session,
                journal_id=journal_id,
                journal_type=journal_type,
                source_type=source_type,
                source_id=source_id,
                status=status,
                created_at=created_at,
                metadata=metadata,
            )
            session.commit()

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
        session.execute(
            insert(LedgerJournalModel)
            .values(
                journal_id=journal_id,
                journal_type=journal_type,
                source_type=source_type,
                source_id=source_id,
                status=status,
                created_at=created_at,
                posted_at=None,
                metadata_json=metadata,
            )
            .on_conflict_do_nothing()
        )

    def mark_posted(self, journal_id: str, posted_at: datetime) -> None:
        with self.session_factory() as session:
            self.mark_posted_in_session(session, journal_id, posted_at)
            session.commit()

    def mark_posted_in_session(self, session: Session, journal_id: str, posted_at: datetime) -> None:
        row = session.get(LedgerJournalModel, journal_id)
        if row is None:
            return
        row.status = "POSTED"
        row.posted_at = posted_at

    def get_by_source(self, source_type: str, source_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(LedgerJournalModel)
                .where(LedgerJournalModel.source_type == source_type)
                .where(LedgerJournalModel.source_id == source_id)
                .limit(1)
            )
        return _ledger_journal_row_to_dict(row) if row is not None else None

    def get_by_source_in_session(self, session: Session, source_type: str, source_id: str) -> dict | None:
        row = session.scalar(
            select(LedgerJournalModel)
            .where(LedgerJournalModel.source_type == source_type)
            .where(LedgerJournalModel.source_id == source_id)
            .limit(1)
        )
        return _ledger_journal_row_to_dict(row) if row is not None else None


class PostgresLedgerEntryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def append_entries(self, *, entries: list[dict]) -> None:
        if not entries:
            return
        with self.session_factory() as session:
            self.append_entries_in_session(session, entries=entries)
            session.commit()

    def append_entries_in_session(self, session: Session, *, entries: list[dict]) -> None:
        if not entries:
            return
        normalized_entries = [
            {
                "entry_id": str(entry["entry_id"]),
                "journal_id": str(entry["journal_id"]),
                "account_id": str(entry["account_id"]),
                "direction": str(entry["direction"]),
                "amount": Decimal(str(entry["amount"])),
                "currency": str(entry["currency"]),
                "effective_at": entry["effective_at"],
                "source_type": str(entry["source_type"]),
                "source_id": str(entry["source_id"]),
                "created_at": entry["created_at"],
            }
            for entry in entries
        ]
        session.execute(insert(LedgerEntryModel).values(normalized_entries).on_conflict_do_nothing())

    def entries_for_journal(self, journal_id: str) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(LedgerEntryModel)
                .where(LedgerEntryModel.journal_id == journal_id)
                .order_by(LedgerEntryModel.created_at, LedgerEntryModel.entry_id)
            ).all()
        return [_ledger_entry_row_to_dict(row) for row in rows]

    def balance_by_account(self, account_id: str) -> Decimal:
        with self.session_factory() as session:
            rows = session.scalars(select(LedgerEntryModel).where(LedgerEntryModel.account_id == account_id)).all()
        balance = Decimal("0")
        for row in rows:
            balance += row.amount if row.direction == "debit" else -row.amount
        return balance


class PostgresSettlementRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create_settlement(
        self,
        *,
        settlement_id: str,
        fill_id: str,
        order_id: str,
        state: str,
        created_at: datetime,
    ) -> None:
        with self.session_factory() as session:
            self.create_settlement_in_session(
                session,
                settlement_id=settlement_id,
                fill_id=fill_id,
                order_id=order_id,
                state=state,
                created_at=created_at,
            )
            session.commit()

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
        session.execute(
            insert(SettlementModel)
            .values(
                settlement_id=settlement_id,
                fill_id=fill_id,
                order_id=order_id,
                journal_id=None,
                state=state,
                created_at=created_at,
                posted_at=None,
            )
            .on_conflict_do_nothing()
        )

    def attach_journal(self, *, settlement_id: str, journal_id: str, posted_at: datetime) -> None:
        with self.session_factory() as session:
            self.attach_journal_in_session(
                session,
                settlement_id=settlement_id,
                journal_id=journal_id,
                posted_at=posted_at,
            )
            session.commit()

    def attach_journal_in_session(
        self,
        session: Session,
        *,
        settlement_id: str,
        journal_id: str,
        posted_at: datetime,
    ) -> None:
        row = session.get(SettlementModel, settlement_id)
        if row is None:
            raise KeyError(f"settlement_not_found:{settlement_id}")
        row.journal_id = journal_id
        row.state = "POSTED"
        row.posted_at = posted_at

    def get_by_fill_id(self, fill_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(select(SettlementModel).where(SettlementModel.fill_id == fill_id).limit(1))
        return _settlement_row_to_dict(row) if row is not None else None

    def get_by_fill_id_in_session(self, session: Session, fill_id: str) -> dict | None:
        row = session.scalar(select(SettlementModel).where(SettlementModel.fill_id == fill_id).limit(1))
        return _settlement_row_to_dict(row) if row is not None else None


def _find_account(
    *,
    session: Session,
    account_type: str,
    currency: str,
    product_type: str,
    margin_mode: str,
    symbol: str | None,
) -> LedgerAccountModel | None:
    query = (
        select(LedgerAccountModel)
        .where(LedgerAccountModel.account_type == account_type)
        .where(LedgerAccountModel.currency == currency)
        .where(LedgerAccountModel.product_type == product_type)
        .where(LedgerAccountModel.margin_mode == margin_mode)
    )
    if symbol is None:
        query = query.where(LedgerAccountModel.symbol.is_(None))
    else:
        query = query.where(LedgerAccountModel.symbol == symbol)
    return session.scalar(query.limit(1))


def _acquire_transaction_advisory_lock(session: Session, *parts: str) -> None:
    lock_key = _stable_lock_key(*parts)
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _stable_lock_key(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:8], byteorder="big", signed=False)
    if value >= 2**63:
        value -= 2**64
    return value


def _ledger_account_row_to_dict(row: LedgerAccountModel) -> dict:
    return {
        "account_id": row.account_id,
        "account_type": row.account_type,
        "currency": row.currency,
        "product_type": row.product_type,
        "margin_mode": row.margin_mode,
        "symbol": row.symbol,
        "created_at": row.created_at,
    }


def _ledger_journal_row_to_dict(row: LedgerJournalModel) -> dict:
    return {
        "journal_id": row.journal_id,
        "journal_type": row.journal_type,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "status": row.status,
        "created_at": row.created_at,
        "posted_at": row.posted_at,
        "metadata": dict(row.metadata_json),
    }


def _ledger_entry_row_to_dict(row: LedgerEntryModel) -> dict:
    return {
        "entry_id": row.entry_id,
        "journal_id": row.journal_id,
        "account_id": row.account_id,
        "direction": row.direction,
        "amount": row.amount,
        "currency": row.currency,
        "effective_at": row.effective_at,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "created_at": row.created_at,
    }


def _settlement_row_to_dict(row: SettlementModel) -> dict:
    return {
        "settlement_id": row.settlement_id,
        "fill_id": row.fill_id,
        "order_id": row.order_id,
        "journal_id": row.journal_id,
        "state": row.state,
        "created_at": row.created_at,
        "posted_at": row.posted_at,
    }
