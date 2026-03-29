from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from aats.bootstrap.logging import get_logger, log_event
from aats.schemas.common import utc_now
from aats.schemas.portfolio import FundingFeeRecord
from aats.services.execution_engine.okx_bills import describe_okx_bill, okx_bill_semantic_group


@dataclass(slots=True, frozen=True)
class FundingFeeSyncResult:
    scanned_count: int
    funding_fee_count: int
    posted_count: int
    latest_bill_id: str | None = None


@dataclass(slots=True)
class LedgerFundingFeeSyncService:
    funding_fee_repo: Any
    ledger_account_repo: Any
    ledger_journal_repo: Any
    ledger_entry_repo: Any
    logger: Any = field(init=False)

    _EPSILON = Decimal("1e-12")

    def __post_init__(self) -> None:
        self.logger = get_logger("aats.funding_fee_sync")

    def sync_recent_bills(
        self,
        *,
        rows: list[dict[str, Any]],
        product_type: str,
        margin_mode: str,
    ) -> FundingFeeSyncResult:
        funding_rows = [
            dict(row)
            for row in rows
            if okx_bill_semantic_group(
                bill_type=str(row.get("type") or ""),
                sub_type=str(row.get("subType") or row.get("sub_type") or ""),
            )
            == "funding_fee"
        ]
        if not funding_rows:
            return FundingFeeSyncResult(scanned_count=len(rows), funding_fee_count=0, posted_count=0)

        session_factory = getattr(self.ledger_account_repo, "session_factory", None)
        if (
            session_factory is not None
            and session_factory is getattr(self.funding_fee_repo, "session_factory", None)
            and hasattr(self.funding_fee_repo, "save_record_in_session")
            and hasattr(self.funding_fee_repo, "get_record_in_session")
        ):
            with session_factory() as session:
                posted_count, latest_bill_id = self._sync_in_session(
                    session=session,
                    rows=funding_rows,
                    product_type=product_type,
                    margin_mode=margin_mode,
                )
                session.commit()
            return FundingFeeSyncResult(
                scanned_count=len(rows),
                funding_fee_count=len(funding_rows),
                posted_count=posted_count,
                latest_bill_id=latest_bill_id,
            )

        posted_count = 0
        latest_bill_id = None
        for row in funding_rows:
            record = self._build_record(row=row, product_type=product_type, margin_mode=margin_mode)
            existing = self.funding_fee_repo.get_record(record.bill_id)
            self._assert_record_compatible(existing=existing, candidate=record)
            journal = self.ledger_journal_repo.get_by_source("exchange_funding_fee_bill", record.bill_id)
            if journal is None:
                self._post_record(record=record)
                posted_count += 1
            else:
                record = record.model_copy(
                    update={
                        "ledger_posting_state": "POSTED",
                        "ledger_journal_id": journal.get("journal_id"),
                        "ledger_posted_at": journal.get("posted_at"),
                    }
                )
            self.funding_fee_repo.save_record(record)
            latest_bill_id = record.bill_id
        return FundingFeeSyncResult(
            scanned_count=len(rows),
            funding_fee_count=len(funding_rows),
            posted_count=posted_count,
            latest_bill_id=latest_bill_id,
        )

    def _sync_in_session(
        self,
        *,
        session: Session,
        rows: list[dict[str, Any]],
        product_type: str,
        margin_mode: str,
    ) -> tuple[int, str | None]:
        posted_count = 0
        latest_bill_id = None
        for row in rows:
            record = self._build_record(row=row, product_type=product_type, margin_mode=margin_mode)
            existing = self.funding_fee_repo.get_record_in_session(session, record.bill_id)
            self._assert_record_compatible(existing=existing, candidate=record)
            journal = self.ledger_journal_repo.get_by_source_in_session(session, "exchange_funding_fee_bill", record.bill_id)
            if journal is None:
                record = self._post_record_in_session(session=session, record=record)
                posted_count += 1
            else:
                record = record.model_copy(
                    update={
                        "ledger_posting_state": "POSTED",
                        "ledger_journal_id": journal.get("journal_id"),
                        "ledger_posted_at": journal.get("posted_at"),
                    }
                )
            self.funding_fee_repo.save_record_in_session(session, record)
            latest_bill_id = record.bill_id
        return posted_count, latest_bill_id

    def _post_record(self, *, record: FundingFeeRecord) -> FundingFeeRecord:
        session_factory = getattr(self.ledger_account_repo, "session_factory", None)
        if session_factory is None:
            raise RuntimeError("ledger_account_repo_session_factory_missing")
        with session_factory() as session:
            record = self._post_record_in_session(session=session, record=record)
            session.commit()
        return record

    def _post_record_in_session(self, *, session: Session, record: FundingFeeRecord) -> FundingFeeRecord:
        if abs(record.amount) <= self._EPSILON:
            return record.model_copy(
                update={
                    "ledger_posting_state": "POSTED",
                    "ledger_posted_at": record.bill_ts or record.created_at,
                }
            )
        journal_type = "funding_fee_income" if record.amount > 0 else "funding_fee_expense"
        journal_id = self._stable_id("jrnl", "exchange_funding_fee_bill", record.bill_id)
        amount = abs(record.amount)
        available_account_id = self._account_id(
            session=session,
            account_type="cash_available",
            currency=record.currency,
            product_type=record.product_type,
            margin_mode=record.margin_mode,
        )
        offset_account_type = "funding_fee_income" if record.amount > 0 else "funding_fee_expense"
        offset_account_id = self._account_id(
            session=session,
            account_type=offset_account_type,
            currency=record.currency,
            product_type=record.product_type,
            margin_mode=record.margin_mode,
        )
        if record.amount > 0:
            entries = (
                ("available", available_account_id, "debit", amount, record.currency),
                ("income", offset_account_id, "credit", amount, record.currency),
            )
        else:
            entries = (
                ("expense", offset_account_id, "debit", amount, record.currency),
                ("available", available_account_id, "credit", amount, record.currency),
            )
        self._post_journal(
            session=session,
            journal_id=journal_id,
            journal_type=journal_type,
            source_type="exchange_funding_fee_bill",
            source_id=record.bill_id,
            created_at=record.bill_ts or record.created_at,
            metadata={
                "bill_id": record.bill_id,
                "symbol": record.symbol,
                "currency": record.currency,
                "amount": format(record.amount, "f"),
                "bill_type": record.bill_type,
                "sub_type": record.sub_type,
                "funding_direction": record.funding_direction,
            },
            entries=entries,
        )
        log_event(
            self.logger,
            "funding_fee_posted",
            bill_id=record.bill_id,
            symbol=record.symbol,
            currency=record.currency,
            amount=format(record.amount, "f"),
            product_type=record.product_type,
            margin_mode=record.margin_mode,
        )
        return record.model_copy(
            update={
                "ledger_posting_state": "POSTED",
                "ledger_journal_id": journal_id,
                "ledger_posted_at": record.bill_ts or record.created_at,
            }
        )

    def _post_journal(
        self,
        *,
        session: Session,
        journal_id: str,
        journal_type: str,
        source_type: str,
        source_id: str,
        created_at: datetime,
        metadata: dict[str, Any],
        entries: tuple[tuple[str, str, str, Decimal, str], ...],
    ) -> None:
        normalized_entries = [
            {
                "entry_id": self._stable_id("ent", journal_id, label),
                "journal_id": journal_id,
                "account_id": account_id,
                "direction": direction,
                "amount": amount,
                "currency": currency,
                "effective_at": created_at,
                "source_type": source_type,
                "source_id": source_id,
                "created_at": created_at,
            }
            for label, account_id, direction, amount, currency in entries
        ]
        self.ledger_journal_repo.create_journal_in_session(
            session,
            journal_id=journal_id,
            journal_type=journal_type,
            source_type=source_type,
            source_id=source_id,
            status="PENDING",
            created_at=created_at,
            metadata=metadata,
        )
        self.ledger_entry_repo.append_entries_in_session(session, entries=normalized_entries)
        self.ledger_journal_repo.mark_posted_in_session(session, journal_id, created_at)

    def _account_id(
        self,
        *,
        session: Session,
        account_type: str,
        currency: str,
        product_type: str,
        margin_mode: str,
    ) -> str:
        return self.ledger_account_repo.get_or_create_account_in_session(
            session,
            account_type=account_type,
            currency=currency,
            product_type=product_type,
            margin_mode=margin_mode,
            symbol=None,
            created_at=utc_now(),
        )

    def _build_record(
        self,
        *,
        row: dict[str, Any],
        product_type: str,
        margin_mode: str,
    ) -> FundingFeeRecord:
        bill_type = str(row.get("type") or "")
        sub_type = str(row.get("subType") or row.get("sub_type") or "")
        description = describe_okx_bill(
            bill_type=bill_type,
            sub_type=sub_type,
            currency=str(row.get("ccy") or ""),
        )
        amount = self._bill_amount(row)
        direction = "income" if amount > self._EPSILON else "expense" if amount < -self._EPSILON else "neutral"
        return FundingFeeRecord(
            bill_id=self._bill_id(row),
            symbol=self._text_value(row, "instId"),
            currency=self._text_value(row, "ccy") or "UNKNOWN",
            amount=amount,
            balance_after=self._decimal_value(row, "bal"),
            bill_type=bill_type,
            sub_type=sub_type,
            type_label=str(description["type_label"]),
            sub_type_label=str(description["sub_type_label"]),
            semantic_group=str(description["semantic_group"]),
            funding_direction=direction,
            bill_ts=self._bill_timestamp(row),
            product_type=product_type,
            margin_mode=margin_mode,
            raw_payload=dict(row),
        )

    def _assert_record_compatible(
        self,
        *,
        existing: FundingFeeRecord | None,
        candidate: FundingFeeRecord,
    ) -> None:
        if existing is None or existing.ledger_posting_state != "POSTED":
            return
        immutable_fields = (
            "symbol",
            "currency",
            "amount",
            "bill_type",
            "sub_type",
            "product_type",
            "margin_mode",
        )
        mismatches = [
            field
            for field in immutable_fields
            if getattr(existing, field) != getattr(candidate, field)
        ]
        if mismatches:
            mismatch_text = ",".join(mismatches)
            raise RuntimeError(f"funding_fee_record_conflict:{candidate.bill_id}:{mismatch_text}")

    @staticmethod
    def _bill_id(row: dict[str, Any]) -> str:
        bill_id = str(row.get("billId") or "").strip()
        if bill_id:
            return bill_id
        raw = "|".join(
            [
                str(row.get("instId") or ""),
                str(row.get("ccy") or ""),
                str(row.get("type") or ""),
                str(row.get("subType") or row.get("sub_type") or ""),
                str(row.get("ts") or row.get("billTs") or row.get("fillTime") or ""),
                str(row.get("balChg") or row.get("sz") or row.get("amount") or row.get("amt") or row.get("pnl") or "0"),
            ]
        ).encode("utf-8")
        return f"ff_{hashlib.sha256(raw).hexdigest()[:40]}"

    @staticmethod
    def _bill_timestamp(row: dict[str, Any]) -> datetime | None:
        for key in ("ts", "billTs", "fillTime"):
            timestamp = row.get(key)
            if timestamp in {None, ""}:
                continue
            return datetime.fromtimestamp(int(str(timestamp)) / 1000.0, tz=timezone.utc)
        return None

    @staticmethod
    def _bill_amount(row: dict[str, Any]) -> Decimal:
        for key in ("balChg", "sz", "amount", "amt", "pnl"):
            value = row.get(key)
            if value not in {None, ""}:
                return Decimal(str(value))
        return Decimal("0")

    @staticmethod
    def _decimal_value(row: dict[str, Any], *keys: str) -> Decimal | None:
        for key in keys:
            value = row.get(key)
            if value in {None, ""}:
                continue
            return Decimal(str(value))
        return None

    @staticmethod
    def _text_value(row: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = row.get(key)
            if value in {None, ""}:
                continue
            return str(value)
        return None

    @staticmethod
    def _stable_id(*parts: str) -> str:
        raw = "|".join(parts).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:48]
