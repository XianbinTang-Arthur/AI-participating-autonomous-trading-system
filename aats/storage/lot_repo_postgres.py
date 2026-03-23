from __future__ import annotations

from decimal import Decimal

from sqlalchemy import asc, delete, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.storage.sqlalchemy_models import LotEventModel, PositionLotModel


class PostgresPositionLotRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def replace_scope(
        self,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        lots: list[dict],
    ) -> None:
        with self.session_factory() as session:
            self.replace_scope_in_session(
                session,
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
                lots=lots,
            )
            session.commit()

    def replace_scope_in_session(
        self,
        session: Session,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        lots: list[dict],
    ) -> None:
        session.execute(
            delete(PositionLotModel)
            .where(PositionLotModel.symbol == symbol)
            .where(PositionLotModel.product_type == product_type)
            .where(PositionLotModel.margin_mode == margin_mode)
        )
        for lot in lots:
            session.add(
                PositionLotModel(
                    lot_id=str(lot["lot_id"]),
                    symbol=symbol,
                    product_type=product_type,
                    margin_mode=margin_mode,
                    signed_quantity_open=Decimal(str(lot["signed_quantity_open"])),
                    entry_price=Decimal(str(lot["entry_price"])),
                    source_fill_id=str(lot["source_fill_id"]),
                    target_leverage=float(lot.get("target_leverage") or 1.0),
                    exposure_side=str(lot.get("exposure_side") or "flat"),
                    status=str(lot["status"]),
                    opened_at=lot["opened_at"],
                    closed_at=lot.get("closed_at"),
                    updated_at=lot["updated_at"],
                    metadata_json=dump_payload_exact(lot.get("metadata") or {}),
                )
            )

    def lots_for_scope(
        self,
        *,
        symbol: str | None = None,
        product_type: str,
        margin_mode: str,
        open_only: bool = False,
    ) -> list[dict]:
        query = (
            select(PositionLotModel)
            .where(PositionLotModel.product_type == product_type)
            .where(PositionLotModel.margin_mode == margin_mode)
        )
        if symbol is not None:
            query = query.where(PositionLotModel.symbol == symbol)
        if open_only:
            query = query.where(PositionLotModel.status == "OPEN")
        query = query.order_by(
            asc(PositionLotModel.symbol),
            asc(PositionLotModel.opened_at),
            asc(PositionLotModel.lot_id),
        )
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [_lot_row_to_dict(row) for row in rows]


class PostgresLotEventRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def replace_scope(
        self,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        events: list[dict],
    ) -> None:
        with self.session_factory() as session:
            self.replace_scope_in_session(
                session,
                symbol=symbol,
                product_type=product_type,
                margin_mode=margin_mode,
                events=events,
            )
            session.commit()

    def replace_scope_in_session(
        self,
        session: Session,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        events: list[dict],
    ) -> None:
        session.execute(
            delete(LotEventModel)
            .where(LotEventModel.symbol == symbol)
            .where(LotEventModel.product_type == product_type)
            .where(LotEventModel.margin_mode == margin_mode)
        )
        for event in events:
            session.add(
                LotEventModel(
                    event_id=str(event["event_id"]),
                    fill_id=str(event["fill_id"]),
                    lot_id=str(event["lot_id"]),
                    symbol=symbol,
                    product_type=product_type,
                    margin_mode=margin_mode,
                    event_type=str(event["event_type"]),
                    quantity=Decimal(str(event["quantity"])),
                    entry_price=Decimal(str(event["entry_price"])),
                    exit_price=None
                    if event.get("exit_price") is None
                    else Decimal(str(event["exit_price"])),
                    realized_pnl_delta=Decimal(str(event.get("realized_pnl_delta") or "0")),
                    created_at=event["created_at"],
                    payload=dump_payload_exact(event.get("payload") or {}),
                )
            )

    def events_for_fill(self, fill_id: str) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(LotEventModel)
                .where(LotEventModel.fill_id == fill_id)
                .order_by(asc(LotEventModel.created_at), asc(LotEventModel.event_id))
            ).all()
        return [_lot_event_row_to_dict(row) for row in rows]


def _lot_row_to_dict(row: PositionLotModel) -> dict:
    return {
        "lot_id": row.lot_id,
        "symbol": row.symbol,
        "product_type": row.product_type,
        "margin_mode": row.margin_mode,
        "signed_quantity_open": row.signed_quantity_open,
        "entry_price": row.entry_price,
        "source_fill_id": row.source_fill_id,
        "target_leverage": row.target_leverage,
        "exposure_side": row.exposure_side,
        "status": row.status,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "updated_at": row.updated_at,
        "metadata": dict(row.metadata_json),
    }


def _lot_event_row_to_dict(row: LotEventModel) -> dict:
    return {
        "event_id": row.event_id,
        "fill_id": row.fill_id,
        "lot_id": row.lot_id,
        "symbol": row.symbol,
        "product_type": row.product_type,
        "margin_mode": row.margin_mode,
        "event_type": row.event_type,
        "quantity": row.quantity,
        "entry_price": row.entry_price,
        "exit_price": row.exit_price,
        "realized_pnl_delta": row.realized_pnl_delta,
        "created_at": row.created_at,
        "payload": dict(row.payload),
    }
