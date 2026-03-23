from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.storage.ledger_repo import LedgerAccountRepository, LedgerEntryRepository, LedgerJournalRepository
from aats.storage.reservation_repo import ReservationRepositoryV2


@dataclass(slots=True, frozen=True)
class FillSettlementProjection:
    base_currency: str | None
    quote_currency: str | None
    starting_quantity: Decimal
    ending_quantity: Decimal
    realized_pnl_delta: Decimal
    fee_delta: Decimal


class LedgerSettlementPostingService:
    _EPSILON = Decimal("1e-12")

    def __init__(
        self,
        *,
        ledger_account_repo: LedgerAccountRepository,
        ledger_journal_repo: LedgerJournalRepository,
        ledger_entry_repo: LedgerEntryRepository,
        reservation_repo: ReservationRepositoryV2 | None = None,
    ) -> None:
        self.ledger_account_repo = ledger_account_repo
        self.ledger_journal_repo = ledger_journal_repo
        self.ledger_entry_repo = ledger_entry_repo
        self.reservation_repo = reservation_repo

    def ensure_initial_balance(
        self,
        *,
        currency: str,
        amount: Decimal,
        product_type: str,
        margin_mode: str,
    ) -> None:
        if amount <= self._EPSILON:
            return
        source_id = self._stable_id("bootstrap", product_type, margin_mode, currency)
        session_factory = getattr(self.ledger_account_repo, "session_factory", None)
        if session_factory is None:
            raise RuntimeError("ledger_account_repo_session_factory_missing")
        with session_factory() as session:
            if self.ledger_journal_repo.get_by_source_in_session(session, "opening_balance", source_id) is not None:
                session.commit()
                return
            available_account_id = self._account_id(
                account_type="cash_available",
                currency=currency,
                product_type=product_type,
                margin_mode=margin_mode,
                session=session,
            )
            opening_account_id = self._account_id(
                account_type="opening_balance",
                currency=currency,
                product_type=product_type,
                margin_mode=margin_mode,
                session=session,
            )
            now = utc_now()
            self._post_journal(
                journal_type="opening_balance",
                source_type="opening_balance",
                source_id=source_id,
                created_at=now,
                metadata={"currency": currency, "amount": format(amount, "f")},
                entries=(
                    ("available", available_account_id, "debit", amount, currency),
                    ("opening", opening_account_id, "credit", amount, currency),
                ),
                session=session,
            )
            session.commit()

    def post_fill_effects(
        self,
        *,
        fill: FillEvent,
        projection: FillSettlementProjection,
    ) -> None:
        session_factory = getattr(self.ledger_account_repo, "session_factory", None)
        if session_factory is None:
            raise RuntimeError("ledger_account_repo_session_factory_missing")
        with session_factory() as session:
            product_type = fill.product_type
            margin_mode = fill.margin_mode
            base_currency = projection.base_currency
            quote_currency = projection.quote_currency
            fee_currency = str(fill.fee_currency or quote_currency or "")
            fill_qty = Decimal(str(fill.fill_qty))
            fill_price = Decimal(str(fill.fill_price))
            fee_amount = Decimal(str(fill.fee_amount))
            notional = fill_qty * fill_price
            reservation_row = (
                self.reservation_repo.get_by_order_id_in_session(session, fill.client_order_id)
                if self.reservation_repo is not None
                else None
            )

            if product_type != "derivatives" and reservation_row is None:
                if fill.side == "buy" and quote_currency:
                    source_id = self._stable_id("fill_quote_spend", fill.fill_id, quote_currency)
                    if self.ledger_journal_repo.get_by_source_in_session(session, "fill_quote_spend", source_id) is None:
                        self._post_journal(
                            journal_type="fill_quote_spend",
                            source_type="fill_quote_spend",
                            source_id=source_id,
                            created_at=fill.ingestion_timestamp,
                            metadata=self._fill_metadata(fill),
                            entries=(
                                (
                                    "external_quote",
                                    self._account_id(
                                        account_type="external_clearing",
                                        currency=quote_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "debit",
                                    notional,
                                    quote_currency,
                                ),
                                (
                                    "available_quote",
                                    self._account_id(
                                        account_type="cash_available",
                                        currency=quote_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "credit",
                                    notional,
                                    quote_currency,
                                ),
                            ),
                            session=session,
                        )
                if fill.side == "sell" and base_currency:
                    source_id = self._stable_id("fill_asset_delivery", fill.fill_id, base_currency)
                    if self.ledger_journal_repo.get_by_source_in_session(session, "fill_asset_delivery", source_id) is None:
                        self._post_journal(
                            journal_type="fill_asset_delivery",
                            source_type="fill_asset_delivery",
                            source_id=source_id,
                            created_at=fill.ingestion_timestamp,
                            metadata=self._fill_metadata(fill),
                            entries=(
                                (
                                    "external_base",
                                    self._account_id(
                                        account_type="external_clearing",
                                        currency=base_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "debit",
                                    fill_qty,
                                    base_currency,
                                ),
                                (
                                    "available_base",
                                    self._account_id(
                                        account_type="cash_available",
                                        currency=base_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "credit",
                                    fill_qty,
                                    base_currency,
                                ),
                            ),
                            session=session,
                        )

            if product_type != "derivatives":
                if fill.side == "buy" and base_currency:
                    source_id = self._stable_id("fill_asset_receipt", fill.fill_id, base_currency)
                    if self.ledger_journal_repo.get_by_source_in_session(session, "fill_asset_receipt", source_id) is None:
                        self._post_journal(
                            journal_type="fill_asset_receipt",
                            source_type="fill_asset_receipt",
                            source_id=source_id,
                            created_at=fill.ingestion_timestamp,
                            metadata=self._fill_metadata(fill),
                            entries=(
                                (
                                    "available_base",
                                    self._account_id(
                                        account_type="cash_available",
                                        currency=base_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "debit",
                                    fill_qty,
                                    base_currency,
                                ),
                                (
                                    "external_base",
                                    self._account_id(
                                        account_type="external_clearing",
                                        currency=base_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "credit",
                                    fill_qty,
                                    base_currency,
                                ),
                            ),
                            session=session,
                        )
                if fill.side == "sell" and quote_currency:
                    source_id = self._stable_id("fill_quote_proceeds", fill.fill_id, quote_currency)
                    if self.ledger_journal_repo.get_by_source_in_session(session, "fill_quote_proceeds", source_id) is None:
                        self._post_journal(
                            journal_type="fill_quote_proceeds",
                            source_type="fill_quote_proceeds",
                            source_id=source_id,
                            created_at=fill.ingestion_timestamp,
                            metadata=self._fill_metadata(fill),
                            entries=(
                                (
                                    "available_quote",
                                    self._account_id(
                                        account_type="cash_available",
                                        currency=quote_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "debit",
                                    notional,
                                    quote_currency,
                                ),
                                (
                                    "external_quote",
                                    self._account_id(
                                        account_type="external_clearing",
                                        currency=quote_currency,
                                        product_type=product_type,
                                        margin_mode=margin_mode,
                                        session=session,
                                    ),
                                    "credit",
                                    notional,
                                    quote_currency,
                                ),
                            ),
                            session=session,
                        )

            if abs(projection.realized_pnl_delta) > self._EPSILON and quote_currency:
                source_id = self._stable_id("fill_realized_pnl", fill.fill_id, quote_currency)
                if self.ledger_journal_repo.get_by_source_in_session(session, "fill_realized_pnl", source_id) is None:
                    pnl_amount = abs(projection.realized_pnl_delta)
                    if projection.realized_pnl_delta > 0:
                        entries = (
                            (
                                "available_quote",
                                self._account_id(
                                    account_type="cash_available",
                                    currency=quote_currency,
                                    product_type=product_type,
                                    margin_mode=margin_mode,
                                    session=session,
                                ),
                                "debit",
                                pnl_amount,
                                quote_currency,
                            ),
                            (
                                "realized_pnl",
                                self._account_id(
                                    account_type="realized_pnl",
                                    currency=quote_currency,
                                    product_type=product_type,
                                    margin_mode=margin_mode,
                                    session=session,
                                ),
                                "credit",
                                pnl_amount,
                                quote_currency,
                            ),
                        )
                    else:
                        entries = (
                            (
                                "realized_pnl",
                                self._account_id(
                                    account_type="realized_pnl",
                                    currency=quote_currency,
                                    product_type=product_type,
                                    margin_mode=margin_mode,
                                    session=session,
                                ),
                                "debit",
                                pnl_amount,
                                quote_currency,
                            ),
                            (
                                "available_quote",
                                self._account_id(
                                    account_type="cash_available",
                                    currency=quote_currency,
                                    product_type=product_type,
                                    margin_mode=margin_mode,
                                    session=session,
                                ),
                                "credit",
                                pnl_amount,
                                quote_currency,
                            ),
                        )
                    self._post_journal(
                        journal_type="fill_realized_pnl",
                        source_type="fill_realized_pnl",
                        source_id=source_id,
                        created_at=fill.ingestion_timestamp,
                        metadata=self._fill_metadata(fill),
                        entries=entries,
                        session=session,
                    )

            fee_covered_by_reservation = (
                reservation_row is not None
                and product_type == "spot"
                and fill.side == "buy"
                and fee_currency == quote_currency
            )
            if fee_amount > self._EPSILON and fee_currency and not fee_covered_by_reservation:
                fee_source_id = self._stable_id("fill_fee", fill.fill_id, fee_currency)
                if self.ledger_journal_repo.get_by_source_in_session(session, "fill_fee", fee_source_id) is None:
                    self._post_journal(
                        journal_type="fill_fee",
                        source_type="fill_fee",
                        source_id=fee_source_id,
                        created_at=fill.ingestion_timestamp,
                        metadata=self._fill_metadata(fill),
                        entries=(
                            (
                                "fee_expense",
                                self._account_id(
                                    account_type="fee_expense",
                                    currency=fee_currency,
                                    product_type=product_type,
                                    margin_mode=margin_mode,
                                    session=session,
                                ),
                                "debit",
                                fee_amount,
                                fee_currency,
                            ),
                            (
                                "available_fee_currency",
                                self._account_id(
                                    account_type="cash_available",
                                    currency=fee_currency,
                                    product_type=product_type,
                                    margin_mode=margin_mode,
                                    session=session,
                                ),
                                "credit",
                                fee_amount,
                                fee_currency,
                            ),
                        ),
                        session=session,
                    )
            session.commit()

    def available_balances(
        self,
        *,
        product_type: str,
        margin_mode: str,
    ) -> dict[str, Decimal]:
        balances: dict[str, Decimal] = {}
        for account in self.ledger_account_repo.list_accounts(
            account_type="cash_available",
            product_type=product_type,
            margin_mode=margin_mode,
        ):
            currency = str(account["currency"])
            balances[currency] = balances.get(currency, Decimal("0")) + self.ledger_entry_repo.balance_by_account(
                str(account["account_id"])
            )
        return {currency: amount for currency, amount in balances.items() if abs(amount) > self._EPSILON}

    def _account_id(
        self,
        *,
        account_type: str,
        currency: str,
        product_type: str,
        margin_mode: str,
        session: Session | None = None,
    ) -> str:
        if session is not None:
            return self.ledger_account_repo.get_or_create_account_in_session(
                session,
                account_type=account_type,
                currency=currency,
                product_type=product_type,
                margin_mode=margin_mode,
                symbol=None,
                created_at=utc_now(),
            )
        return self.ledger_account_repo.get_or_create_account(
            account_type=account_type,
            currency=currency,
            product_type=product_type,
            margin_mode=margin_mode,
            symbol=None,
            created_at=utc_now(),
        )

    def _post_journal(
        self,
        *,
        journal_type: str,
        source_type: str,
        source_id: str,
        created_at,
        metadata: dict,
        entries: tuple[tuple[str, str, str, Decimal, str], ...],
        session: Session | None = None,
    ) -> None:
        journal_id = self._stable_id("jrnl", source_type, source_id)
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
        if session is not None:
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
            return
        self.ledger_journal_repo.create_journal(
            journal_id=journal_id,
            journal_type=journal_type,
            source_type=source_type,
            source_id=source_id,
            status="PENDING",
            created_at=created_at,
            metadata=metadata,
        )
        self.ledger_entry_repo.append_entries(entries=normalized_entries)
        self.ledger_journal_repo.mark_posted(journal_id, created_at)

    @staticmethod
    def _fill_metadata(fill: FillEvent) -> dict[str, str]:
        return {
            "decision_id": str(fill.decision_id),
            "intent_id": str(fill.intent_id),
            "client_order_id": str(fill.client_order_id),
            "fill_id": str(fill.fill_id),
            "symbol": str(fill.symbol),
        }

    @staticmethod
    def _stable_id(*parts: str) -> str:
        raw = "|".join(parts).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:48]
