from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.portfolio import SleevePnLRecord
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.sqlalchemy_models import SleevePnLRecordModel


class PostgresSleevePnLRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_record(self, record: SleevePnLRecord) -> SleevePnLRecord:
        with self.session_factory() as session:
            self.save_record_in_session(session, record)
            session.commit()
        return record

    def save_record_in_session(self, session: Session, record: SleevePnLRecord) -> SleevePnLRecord:
        row = session.get(SleevePnLRecordModel, record.record_id)
        payload = dump_payload_exact(record)
        if row is None:
            row = SleevePnLRecordModel(
                record_id=record.record_id,
                strategy_sleeve_id=record.strategy_sleeve_id,
                strategy_family=record.strategy_family,
                allocation_id=record.allocation_id,
                strategy_bundle_id=record.strategy_bundle_id,
                strategy_leg_role=record.strategy_leg_role,
                symbol=record.symbol,
                event_type=record.event_type,
                fill_id=record.fill_id,
                funding_fee_id=record.funding_fee_id,
                fee_currency=record.fee_currency,
                realized_pnl=record.realized_pnl,
                fee_amount=record.fee_amount,
                funding_fee_amount=record.funding_fee_amount,
                inventory_move_qty=record.inventory_move_qty,
                attribution_type=record.attribution_type,
                product_type=record.product_type,
                margin_mode=record.margin_mode,
                event_timestamp=record.event_timestamp,
                created_at=record.created_at,
                payload=payload,
            )
            session.add(row)
        else:
            row.strategy_sleeve_id = record.strategy_sleeve_id
            row.strategy_family = record.strategy_family
            row.allocation_id = record.allocation_id
            row.strategy_bundle_id = record.strategy_bundle_id
            row.strategy_leg_role = record.strategy_leg_role
            row.symbol = record.symbol
            row.event_type = record.event_type
            row.fill_id = record.fill_id
            row.funding_fee_id = record.funding_fee_id
            row.fee_currency = record.fee_currency
            row.realized_pnl = record.realized_pnl
            row.fee_amount = record.fee_amount
            row.funding_fee_amount = record.funding_fee_amount
            row.inventory_move_qty = record.inventory_move_qty
            row.attribution_type = record.attribution_type
            row.product_type = record.product_type
            row.margin_mode = record.margin_mode
            row.event_timestamp = record.event_timestamp
            row.created_at = record.created_at
            row.payload = payload
        return record

    def get_record(self, record_id: str) -> SleevePnLRecord | None:
        with self.session_factory() as session:
            row = session.get(SleevePnLRecordModel, record_id)
        return None if row is None else SleevePnLRecord.model_validate(row.payload)

    def records(self) -> list[SleevePnLRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(SleevePnLRecordModel).order_by(
                    SleevePnLRecordModel.created_at,
                    SleevePnLRecordModel.record_id,
                )
            ).all()
        return [SleevePnLRecord.model_validate(row.payload) for row in rows]

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[SleevePnLRecord]:
        query = (
            select(SleevePnLRecordModel)
            .where(SleevePnLRecordModel.product_type == scope.product_type)
            .where(SleevePnLRecordModel.margin_mode == scope.margin_mode)
        )
        if scope.allowed_symbols:
            query = query.where(
                (SleevePnLRecordModel.symbol.is_(None))
                | (SleevePnLRecordModel.symbol.in_(tuple(scope.allowed_symbols)))
            )
        if since is not None:
            query = query.where(SleevePnLRecordModel.created_at >= since)
        query = query.order_by(
            desc(SleevePnLRecordModel.event_timestamp),
            desc(SleevePnLRecordModel.created_at),
            desc(SleevePnLRecordModel.record_id),
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [SleevePnLRecord.model_validate(row.payload) for row in reversed(rows)]

    def replace_scope(
        self,
        *,
        scope: RuntimeStateScope,
        records: list[SleevePnLRecord],
    ) -> None:
        with self.session_factory() as session:
            self.replace_scope_in_session(session, scope=scope, records=records)
            session.commit()

    def replace_scope_in_session(
        self,
        session: Session,
        *,
        scope: RuntimeStateScope,
        records: list[SleevePnLRecord],
    ) -> None:
        delete_query = (
            delete(SleevePnLRecordModel)
            .where(SleevePnLRecordModel.product_type == scope.product_type)
            .where(SleevePnLRecordModel.margin_mode == scope.margin_mode)
        )
        if scope.allowed_symbols:
            delete_query = delete_query.where(
                (SleevePnLRecordModel.symbol.is_(None))
                | (SleevePnLRecordModel.symbol.in_(tuple(scope.allowed_symbols)))
            )
        session.execute(delete_query)
        for record in records:
            self.save_record_in_session(session, record)
