from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.portfolio import FundingFeeRecord
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.sqlalchemy_models import FundingFeeRecordModel


class PostgresFundingFeeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_record(self, record: FundingFeeRecord) -> FundingFeeRecord:
        with self.session_factory() as session:
            self.save_record_in_session(session, record)
            session.commit()
        return record

    def save_record_in_session(self, session: Session, record: FundingFeeRecord) -> FundingFeeRecord:
        row = session.get(FundingFeeRecordModel, record.bill_id)
        payload = dump_payload_exact(record)
        if row is None:
            row = FundingFeeRecordModel(
                bill_id=record.bill_id,
                symbol=record.symbol,
                currency=record.currency,
                amount=record.amount,
                balance_after=record.balance_after,
                bill_type=record.bill_type,
                sub_type=record.sub_type,
                type_label=record.type_label,
                sub_type_label=record.sub_type_label,
                semantic_group=record.semantic_group,
                funding_direction=record.funding_direction,
                bill_ts=record.bill_ts,
                ledger_posting_state=record.ledger_posting_state,
                ledger_journal_id=record.ledger_journal_id,
                ledger_posted_at=record.ledger_posted_at,
                product_type=record.product_type,
                margin_mode=record.margin_mode,
                created_at=record.created_at,
                payload=payload,
            )
            session.add(row)
        else:
            row.symbol = record.symbol
            row.currency = record.currency
            row.amount = record.amount
            row.balance_after = record.balance_after
            row.bill_type = record.bill_type
            row.sub_type = record.sub_type
            row.type_label = record.type_label
            row.sub_type_label = record.sub_type_label
            row.semantic_group = record.semantic_group
            row.funding_direction = record.funding_direction
            row.bill_ts = record.bill_ts
            row.ledger_posting_state = record.ledger_posting_state
            row.ledger_journal_id = record.ledger_journal_id
            row.ledger_posted_at = record.ledger_posted_at
            row.product_type = record.product_type
            row.margin_mode = record.margin_mode
            row.created_at = record.created_at
            row.payload = payload
        return record

    def get_record(self, bill_id: str) -> FundingFeeRecord | None:
        with self.session_factory() as session:
            row = session.get(FundingFeeRecordModel, bill_id)
        return None if row is None else FundingFeeRecord.model_validate(row.payload)

    def get_record_in_session(self, session: Session, bill_id: str) -> FundingFeeRecord | None:
        row = session.get(FundingFeeRecordModel, bill_id)
        return None if row is None else FundingFeeRecord.model_validate(row.payload)

    def records(self) -> list[FundingFeeRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FundingFeeRecordModel).order_by(
                    FundingFeeRecordModel.bill_ts,
                    FundingFeeRecordModel.created_at,
                    FundingFeeRecordModel.bill_id,
                )
            ).all()
        return [FundingFeeRecord.model_validate(row.payload) for row in rows]

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FundingFeeRecord]:
        query = (
            select(FundingFeeRecordModel)
            .where(FundingFeeRecordModel.product_type == scope.product_type)
            .where(FundingFeeRecordModel.margin_mode == scope.margin_mode)
        )
        if scope.allowed_symbols:
            query = query.where(
                (FundingFeeRecordModel.symbol.is_(None))
                | (FundingFeeRecordModel.symbol.in_(tuple(scope.allowed_symbols)))
            )
        if since is not None:
            query = query.where(FundingFeeRecordModel.created_at >= since)
        query = query.order_by(
            desc(FundingFeeRecordModel.bill_ts),
            desc(FundingFeeRecordModel.created_at),
            desc(FundingFeeRecordModel.bill_id),
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [FundingFeeRecord.model_validate(row.payload) for row in reversed(rows)]
