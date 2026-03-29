from __future__ import annotations

import hashlib
from decimal import Decimal
from threading import Lock

from sqlalchemy.orm import Session

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderObligation
from aats.services.accounting import fill_fee_delta_in_quote, resolve_symbol_currencies, resolved_fee_currency
from aats.services.portfolio_service.decimals import to_decimal
from aats.storage.ledger_repo import (
    LedgerAccountRepository,
    LedgerEntryRepository,
    LedgerJournalRepository,
    SettlementRepository,
)
from aats.storage.reservation_repo import ReservationRepositoryV2


class Phase1LedgerMirrorService:
    _EPSILON = Decimal("1e-12")

    def __init__(
        self,
        *,
        reservation_repo: ReservationRepositoryV2,
        ledger_account_repo: LedgerAccountRepository | None = None,
        ledger_journal_repo: LedgerJournalRepository | None = None,
        ledger_entry_repo: LedgerEntryRepository | None = None,
        settlement_repo: SettlementRepository | None = None,
    ) -> None:
        self.reservation_repo = reservation_repo
        self.ledger_account_repo = ledger_account_repo
        self.ledger_journal_repo = ledger_journal_repo
        self.ledger_entry_repo = ledger_entry_repo
        self.settlement_repo = settlement_repo
        self._lock = Lock()
        self._sync_attempt_count = 0
        self._sync_success_count = 0
        self._sync_failure_count = 0
        self._last_sync_ts = None
        self._last_failure_ts = None
        self._last_reason = None
        self._last_order_id = None
        self._last_fill_id = None
        self._last_obligation_status = None
        self._last_error = None
        self._last_outcome = "idle"

    def sync_obligation(
        self,
        obligation: OrderObligation | None,
        *,
        reason: str,
        related_fill: FillEvent | None,
    ) -> None:
        if obligation is None:
            return
        with self._lock:
            self._sync_attempt_count += 1
        try:
            session_factory = getattr(self.reservation_repo, "session_factory", None)
            if session_factory is None:
                raise RuntimeError("reservation_repo_session_factory_missing")
            with session_factory() as session:
                reserve_account_id = self._reserve_account_id(obligation, session=session)
                shadow_row = self.reservation_repo.get_by_order_id_in_session(
                    session,
                    obligation.client_order_id,
                    for_update=True,
                )
                if shadow_row is None:
                    self.reservation_repo.create_reservation_in_session(
                        session,
                        reservation_id=obligation.obligation_id,
                        order_id=obligation.client_order_id,
                        reserve_account_id=reserve_account_id,
                        reserved_amount=obligation.reserved_amount,
                        state="ACTIVE",
                        created_at=obligation.created_at,
                    )
                    shadow_row = self.reservation_repo.get_by_order_id_in_session(
                        session,
                        obligation.client_order_id,
                        for_update=True,
                    )
                    if shadow_row is not None and obligation.reserved_amount > self._EPSILON:
                        self._post_reservation_hold(
                            obligation=obligation,
                            amount=obligation.reserved_amount,
                            session=session,
                        )
                if shadow_row is None:
                    session.commit()
                    self._record_success(obligation=obligation, reason=reason, related_fill=related_fill)
                    return

                current_consumed = Decimal(str(shadow_row["consumed_amount"]))
                current_released = Decimal(str(shadow_row["released_amount"]))
                delta_consumed = max(obligation.consumed_amount - current_consumed, Decimal("0"))
                delta_released = max(obligation.released_amount - current_released, Decimal("0"))

                if delta_consumed > self._EPSILON:
                    self.reservation_repo.consume_in_session(
                        session,
                        reservation_id=str(shadow_row["reservation_id"]),
                        amount=delta_consumed,
                        updated_at=obligation.last_update_ts or utc_now(),
                    )
                    if related_fill is not None:
                        self._post_fill_settlement(
                            obligation=obligation,
                            fill=related_fill,
                            amount=delta_consumed,
                            session=session,
                        )

                if delta_released > self._EPSILON:
                    self.reservation_repo.release_in_session(
                        session,
                        reservation_id=str(shadow_row["reservation_id"]),
                        amount=delta_released,
                        next_state=obligation.status,
                        updated_at=obligation.last_update_ts or utc_now(),
                    )
                    self._post_reservation_release(
                        obligation=obligation,
                        amount=delta_released,
                        session=session,
                    )

                refreshed_row = self.reservation_repo.get_by_order_id_in_session(
                    session,
                    obligation.client_order_id,
                    for_update=True,
                )
                if refreshed_row is not None and str(refreshed_row["state"]) != obligation.status:
                    self.reservation_repo.release_in_session(
                        session,
                        reservation_id=str(refreshed_row["reservation_id"]),
                        amount=Decimal("0"),
                        next_state=obligation.status,
                        updated_at=obligation.last_update_ts or utc_now(),
                    )
                session.commit()
            self._record_success(obligation=obligation, reason=reason, related_fill=related_fill)
        except Exception as exc:
            self._record_failure(obligation=obligation, reason=reason, related_fill=related_fill, error=str(exc))
            raise

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            status = "idle"
            if self._last_outcome == "failure":
                status = "degraded"
            elif self._sync_success_count > 0:
                status = "healthy"
            return {
                "configured": True,
                "status": status,
                "last_outcome": self._last_outcome,
                "sync_attempt_count": self._sync_attempt_count,
                "sync_success_count": self._sync_success_count,
                "sync_failure_count": self._sync_failure_count,
                "last_sync_ts": self._last_sync_ts,
                "last_failure_ts": self._last_failure_ts,
                "last_reason": self._last_reason,
                "last_synced_order_id": self._last_order_id,
                "last_synced_fill_id": self._last_fill_id,
                "last_obligation_status": self._last_obligation_status,
                "last_error": self._last_error,
            }

    def _record_success(
        self,
        *,
        obligation: OrderObligation,
        reason: str,
        related_fill: FillEvent | None,
    ) -> None:
        with self._lock:
            self._sync_success_count += 1
            self._last_sync_ts = obligation.last_update_ts or utc_now()
            self._last_reason = reason
            self._last_order_id = obligation.client_order_id
            self._last_fill_id = related_fill.fill_id if related_fill is not None else None
            self._last_obligation_status = obligation.status
            self._last_outcome = "success"

    def _record_failure(
        self,
        *,
        obligation: OrderObligation,
        reason: str,
        related_fill: FillEvent | None,
        error: str,
    ) -> None:
        with self._lock:
            self._sync_failure_count += 1
            self._last_failure_ts = utc_now()
            self._last_reason = reason
            self._last_order_id = obligation.client_order_id
            self._last_fill_id = related_fill.fill_id if related_fill is not None else None
            self._last_obligation_status = obligation.status
            self._last_error = error
            self._last_outcome = "failure"

    def _reserve_account_id(self, obligation: OrderObligation, *, session: Session | None = None) -> str:
        return self._ledger_account_id(
            account_type="cash_reserved",
            currency=obligation.reserve_currency,
            product_type=obligation.product_type,
            margin_mode=obligation.margin_mode,
            created_at=obligation.created_at,
            session=session,
        )

    def _post_reservation_hold(
        self,
        *,
        obligation: OrderObligation,
        amount: Decimal,
        session: Session | None = None,
    ) -> None:
        if amount <= self._EPSILON:
            return
        available_account_id = self._ledger_account_id(
            account_type="cash_available",
            currency=obligation.reserve_currency,
            product_type=obligation.product_type,
            margin_mode=obligation.margin_mode,
            created_at=obligation.created_at,
            session=session,
        )
        reserved_account_id = self._reserve_account_id(obligation, session=session)
        self._post_journal(
            journal_type="reservation_hold",
            source_type="reservation",
            source_id=self._stable_id(
                "src",
                obligation.obligation_id,
                "hold",
                format(obligation.reserved_amount, "f"),
            ),
            created_at=obligation.last_update_ts or obligation.created_at,
            metadata={
                "decision_id": obligation.decision_id,
                "intent_id": obligation.intent_id,
                "client_order_id": obligation.client_order_id,
                "reason": "reservation_hold",
            },
            entries=(
                ("reserved", reserved_account_id, "debit", amount),
                ("available", available_account_id, "credit", amount),
            ),
            currency=obligation.reserve_currency,
            session=session,
        )

    def _post_fill_settlement(
        self,
        *,
        obligation: OrderObligation,
        fill: FillEvent,
        amount: Decimal,
        session: Session | None = None,
    ) -> None:
        if amount <= self._EPSILON or self.settlement_repo is None:
            return
        reserved_account_id = self._reserve_account_id(obligation, session=session)
        external_account_id = self._ledger_account_id(
            account_type="external_clearing",
            currency=obligation.reserve_currency,
            product_type=obligation.product_type,
            margin_mode=obligation.margin_mode,
            created_at=fill.created_at,
            session=session,
        )
        settlement_id = self._stable_id("set", fill.fill_id)
        if session is not None:
            self.settlement_repo.create_settlement_in_session(
                session,
                settlement_id=settlement_id,
                fill_id=fill.fill_id,
                order_id=fill.client_order_id,
                state="PENDING",
                created_at=fill.created_at,
            )
        else:
            self.settlement_repo.create_settlement(
                settlement_id=settlement_id,
                fill_id=fill.fill_id,
                order_id=fill.client_order_id,
                state="PENDING",
                created_at=fill.created_at,
            )
        journal_id = self._post_journal(
            journal_type="fill_settlement",
            source_type="fill_settlement",
            source_id=fill.fill_id,
            created_at=fill.ingestion_timestamp,
            metadata={
                "decision_id": fill.decision_id,
                "intent_id": fill.intent_id,
                "client_order_id": fill.client_order_id,
                "fill_id": fill.fill_id,
                "reserve_currency": obligation.reserve_currency,
            },
            entries=(
                ("external", external_account_id, "debit", amount),
                ("reserved", reserved_account_id, "credit", amount),
            ),
            currency=obligation.reserve_currency,
            session=session,
        )
        if journal_id is not None:
            if session is not None:
                self.settlement_repo.attach_journal_in_session(
                    session,
                    settlement_id=settlement_id,
                    journal_id=journal_id,
                    posted_at=fill.ingestion_timestamp,
                )
            else:
                self.settlement_repo.attach_journal(
                    settlement_id=settlement_id,
                    journal_id=journal_id,
                    posted_at=fill.ingestion_timestamp,
                )
        self._post_reservation_backed_spot_fee_attribution(
            obligation=obligation,
            fill=fill,
            session=session,
        )

    def _post_reservation_release(
        self,
        *,
        obligation: OrderObligation,
        amount: Decimal,
        session: Session | None = None,
    ) -> None:
        if amount <= self._EPSILON:
            return
        available_account_id = self._ledger_account_id(
            account_type="cash_available",
            currency=obligation.reserve_currency,
            product_type=obligation.product_type,
            margin_mode=obligation.margin_mode,
            created_at=obligation.created_at,
            session=session,
        )
        reserved_account_id = self._reserve_account_id(obligation, session=session)
        self._post_journal(
            journal_type="reservation_release",
            source_type="reservation_release",
            source_id=self._stable_id(
                "src",
                obligation.obligation_id,
                "release",
                format(obligation.released_amount, "f"),
            ),
            created_at=obligation.last_update_ts or utc_now(),
            metadata={
                "decision_id": obligation.decision_id,
                "intent_id": obligation.intent_id,
                "client_order_id": obligation.client_order_id,
                "status": obligation.status,
            },
            entries=(
                ("available", available_account_id, "debit", amount),
                ("reserved", reserved_account_id, "credit", amount),
            ),
            currency=obligation.reserve_currency,
            session=session,
        )

    def _post_reservation_backed_spot_fee_attribution(
        self,
        *,
        obligation: OrderObligation,
        fill: FillEvent,
        session: Session | None = None,
    ) -> None:
        if obligation.product_type != "spot":
            return
        base_currency, quote_currency = resolve_symbol_currencies(fill.symbol)
        fee_currency = resolved_fee_currency(
            fill=fill,
            base_currency=base_currency,
            quote_currency=quote_currency,
        )
        if fee_currency is None or obligation.reserve_currency != fee_currency:
            return
        fee_amount = to_decimal(fill.fee_amount)
        if abs(fee_amount) <= self._EPSILON:
            return
        external_account_id = self._ledger_account_id(
            account_type="external_clearing",
            currency=obligation.reserve_currency,
            product_type=obligation.product_type,
            margin_mode=obligation.margin_mode,
            created_at=fill.created_at,
            session=session,
        )
        if obligation.reserve_currency == quote_currency:
            fee_delta_in_reserve = fill_fee_delta_in_quote(
                fill,
                base_currency=base_currency,
                quote_currency=quote_currency,
            )
        elif obligation.reserve_currency == base_currency:
            fee_delta_in_reserve = fee_amount
        else:
            return
        if abs(fee_delta_in_reserve) <= self._EPSILON:
            return
        amount = abs(fee_delta_in_reserve)
        if fee_delta_in_reserve > 0:
            journal_type = "fill_fee_expense"
            fee_account_type = "fee_expense"
            entries = (
                (
                    "fee_expense",
                    self._ledger_account_id(
                        account_type=fee_account_type,
                        currency=obligation.reserve_currency,
                        product_type=obligation.product_type,
                        margin_mode=obligation.margin_mode,
                        created_at=fill.created_at,
                        session=session,
                    ),
                    "debit",
                    amount,
                ),
                ("external", external_account_id, "credit", amount),
            )
            fee_direction = "expense"
        else:
            journal_type = "fill_fee_income"
            fee_account_type = "fee_income"
            entries = (
                ("external", external_account_id, "debit", amount),
                (
                    "fee_income",
                    self._ledger_account_id(
                        account_type=fee_account_type,
                        currency=obligation.reserve_currency,
                        product_type=obligation.product_type,
                        margin_mode=obligation.margin_mode,
                        created_at=fill.created_at,
                        session=session,
                    ),
                    "credit",
                    amount,
                ),
            )
            fee_direction = "income"
        self._post_journal(
            journal_type=journal_type,
            source_type="fill_fee_attribution",
            source_id=self._stable_id("src", fill.fill_id, "fee_attribution", obligation.reserve_currency),
            created_at=fill.ingestion_timestamp,
            metadata={
                "decision_id": fill.decision_id,
                "intent_id": fill.intent_id,
                "client_order_id": fill.client_order_id,
                "fill_id": fill.fill_id,
                "fee_currency": obligation.reserve_currency,
                "resolved_fee_currency": fee_currency,
                "fee_direction": fee_direction,
                "fee_delta_in_reserve": format(fee_delta_in_reserve, "f"),
            },
            entries=entries,
            currency=obligation.reserve_currency,
            session=session,
        )

    def _ledger_account_id(
        self,
        *,
        account_type: str,
        currency: str,
        product_type: str,
        margin_mode: str,
        created_at,
        session: Session | None = None,
    ) -> str:
        if self.ledger_account_repo is None:
            return self._stable_id("acct", account_type, currency, product_type, margin_mode)
        if session is not None:
            return self.ledger_account_repo.get_or_create_account_in_session(
                session,
                account_type=account_type,
                currency=currency,
                product_type=product_type,
                margin_mode=margin_mode,
                symbol=None,
                created_at=created_at,
            )
        return self.ledger_account_repo.get_or_create_account(
            account_type=account_type,
            currency=currency,
            product_type=product_type,
            margin_mode=margin_mode,
            symbol=None,
            created_at=created_at,
        )

    def _post_journal(
        self,
        *,
        journal_type: str,
        source_type: str,
        source_id: str,
        created_at,
        metadata: dict,
        entries: tuple[tuple[str, str, str, Decimal], ...],
        currency: str,
        session: Session | None = None,
    ) -> str | None:
        if self.ledger_journal_repo is None or self.ledger_entry_repo is None:
            return None
        journal_id = self._stable_id("jrnl", source_type, source_id)
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
            self.ledger_entry_repo.append_entries_in_session(
                session,
                entries=[
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
                    for label, account_id, direction, amount in entries
                ],
            )
            self.ledger_journal_repo.mark_posted_in_session(session, journal_id, created_at)
        else:
            self.ledger_journal_repo.create_journal(
                journal_id=journal_id,
                journal_type=journal_type,
                source_type=source_type,
                source_id=source_id,
                status="PENDING",
                created_at=created_at,
                metadata=metadata,
            )
            self.ledger_entry_repo.append_entries(
                entries=[
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
                    for label, account_id, direction, amount in entries
                ]
            )
            self.ledger_journal_repo.mark_posted(journal_id, created_at)
        return journal_id

    @staticmethod
    def stable_id(prefix: str, *parts: object) -> str:
        return Phase1LedgerMirrorService._stable_id(prefix, *parts)

    @staticmethod
    def _stable_id(prefix: str, *parts: object) -> str:
        raw = "|".join(str(part) for part in parts)
        return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"
