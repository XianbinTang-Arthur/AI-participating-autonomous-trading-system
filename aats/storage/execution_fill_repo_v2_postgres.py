from __future__ import annotations

from datetime import datetime

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import FillEvent
from aats.storage.sqlalchemy_models import ExecutionFillModelV2


class PostgresExecutionFillRepositoryV2:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_fill(
        self,
        *,
        fill: FillEvent,
        order_id: str,
        source: str,
        raw_payload: dict,
    ) -> bool:
        with self.session_factory() as session:
            saved = self.save_fill_in_session(
                session,
                fill=fill,
                order_id=order_id,
                source=source,
                raw_payload=raw_payload,
            )
            session.commit()
            return saved

    def save_fill_in_session(
        self,
        session: Session,
        *,
        fill: FillEvent,
        order_id: str,
        source: str,
        raw_payload: dict,
    ) -> bool:
        raw_payload_dict = dict(raw_payload or {})
        venue_fill_id = raw_payload_dict.get("venue_fill_id")
        top_level_raw_exchange = (
            raw_payload_dict.get("raw_exchange")
            if isinstance(raw_payload_dict.get("raw_exchange"), dict)
            else {}
        )
        nested_fill_event = (
            raw_payload_dict.get("fill_event")
            if isinstance(raw_payload_dict.get("fill_event"), dict)
            else {}
        )
        nested_raw_exchange = (
            nested_fill_event.get("raw_exchange")
            if isinstance(nested_fill_event.get("raw_exchange"), dict)
            else {}
        )
        fill_raw_exchange = fill.raw_exchange if isinstance(fill.raw_exchange, dict) else {}
        fee_rate = (
            str(fill_raw_exchange.get("feeRate") or "").strip()
            or str(top_level_raw_exchange.get("feeRate") or "").strip()
            or str(nested_raw_exchange.get("feeRate") or "").strip()
            or None
        )
        exec_type = (
            str(fill_raw_exchange.get("execType") or "").strip()
            or str(top_level_raw_exchange.get("execType") or "").strip()
            or str(nested_raw_exchange.get("execType") or "").strip()
            or None
        )
        inserted_fill_id = session.scalar(
            insert(ExecutionFillModelV2)
            .values(
                fill_id=fill.fill_id,
                venue_fill_id=None if venue_fill_id is None else str(venue_fill_id),
                order_id=order_id,
                execution_attempt_id=fill.execution_attempt_id,
                venue_order_id=fill.exchange_order_id,
                client_order_id=fill.client_order_id,
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                symbol=fill.symbol,
                side=fill.side,
                fill_qty=fill.fill_qty,
                fill_price=fill.fill_price,
                fee_amount=fill.fee_amount,
                fee_currency=fill.fee_currency,
                reduce_only=fill.reduce_only,
                close_only=fill.close_only,
                td_mode=fill.td_mode,
                position_mode=fill.position_mode,
                pos_side=fill.pos_side,
                reduce_only_reason=fill.reduce_only_reason,
                close_only_reason=fill.close_only_reason,
                instrument_family=fill.instrument_family,
                settle_currency=fill.settle_currency,
                strategy_family=fill.strategy_family,
                strategy_sleeve_id=fill.strategy_sleeve_id,
                allocation_id=fill.allocation_id,
                strategy_bundle_id=fill.strategy_bundle_id,
                strategy_leg_role=fill.strategy_leg_role,
                liquidity_role=fill.liquidity_role,
                fee_rate=fee_rate,
                exec_type=exec_type,
                exchange_ts=fill.exchange_timestamp,
                ingestion_ts=fill.ingestion_timestamp,
                source_system=source,
                raw_payload=dump_payload_exact(raw_payload_dict or fill),
                created_at=fill.created_at,
            )
            .returning(ExecutionFillModelV2.fill_id)
            .on_conflict_do_nothing()
        )
        if inserted_fill_id is not None:
            return True
        duplicate = session.get(ExecutionFillModelV2, fill.fill_id)
        if duplicate is None and venue_fill_id is not None:
            duplicate = session.scalar(
                select(ExecutionFillModelV2)
                .where(ExecutionFillModelV2.source_system == source)
                .where(ExecutionFillModelV2.venue_fill_id == str(venue_fill_id))
                .limit(1)
            )
        if duplicate is not None:
            return False
        raise RuntimeError("execution_fill_insert_conflict_without_existing_row")

    def get_fill(self, fill_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(ExecutionFillModelV2, fill_id)
        return _fill_row_to_dict(row) if row is not None else None

    def get_fill_by_dedupe_key(self, source: str, venue_fill_id: str | None) -> dict | None:
        if venue_fill_id is None:
            return None
        with self.session_factory() as session:
            row = session.scalar(
                select(ExecutionFillModelV2)
                .where(ExecutionFillModelV2.source_system == source)
                .where(ExecutionFillModelV2.venue_fill_id == venue_fill_id)
                .limit(1)
            )
        return _fill_row_to_dict(row) if row is not None else None

    def fills_for_order(self, order_id: str) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ExecutionFillModelV2)
                .where(ExecutionFillModelV2.order_id == order_id)
                .order_by(
                    asc(ExecutionFillModelV2.exchange_ts),
                    asc(ExecutionFillModelV2.ingestion_ts),
                    asc(ExecutionFillModelV2.fill_id),
                )
            ).all()
        return [_fill_row_to_dict(row) for row in rows]

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        query = select(ExecutionFillModelV2)
        if since is not None:
            query = query.where(ExecutionFillModelV2.ingestion_ts >= since)
        if limit is not None:
            if limit <= 0:
                return []
            query = query.order_by(
                desc(ExecutionFillModelV2.exchange_ts),
                desc(ExecutionFillModelV2.ingestion_ts),
                desc(ExecutionFillModelV2.fill_id),
            )
            query = query.limit(limit)
        else:
            query = query.order_by(
                asc(ExecutionFillModelV2.exchange_ts),
                asc(ExecutionFillModelV2.ingestion_ts),
                asc(ExecutionFillModelV2.fill_id),
            )
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        if limit is not None:
            rows = list(reversed(rows))
        return [_fill_row_to_dict(row) for row in rows]

    def recent_fills(self, *, limit: int, offset: int = 0) -> list[dict]:
        normalized_limit = max(int(limit), 0)
        if normalized_limit <= 0:
            return []
        query = (
            select(ExecutionFillModelV2)
            .order_by(
                ExecutionFillModelV2.ingestion_ts.desc(),
                ExecutionFillModelV2.exchange_ts.desc(),
                ExecutionFillModelV2.fill_id.desc(),
            )
            .offset(max(int(offset), 0))
            .limit(normalized_limit)
        )
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [_fill_row_to_dict(row) for row in rows]

    def recent_fills_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        normalized_limit = None if limit is None else max(int(limit), 0)
        if normalized_limit == 0:
            return []
        query = (
            self._fills_for_scope_query(
                product_type=product_type,
                margin_mode=margin_mode,
                symbols=symbols,
            )
            .order_by(
                ExecutionFillModelV2.ingestion_ts.desc(),
                ExecutionFillModelV2.exchange_ts.desc(),
                ExecutionFillModelV2.fill_id.desc(),
            )
            .offset(max(int(offset), 0))
        )
        if normalized_limit is not None:
            query = query.limit(normalized_limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [_fill_row_to_dict(row) for row in rows]

    def count_fills(self) -> int:
        with self.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(ExecutionFillModelV2)) or 0)

    def count_fills_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
    ) -> int:
        query = select(func.count()).select_from(
            self._fills_for_scope_query(
                product_type=product_type,
                margin_mode=margin_mode,
                symbols=symbols,
            ).subquery()
        )
        with self.session_factory() as session:
            return int(session.scalar(query) or 0)

    def _fills_for_scope_query(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
    ):
        payload_product_type = ExecutionFillModelV2.raw_payload["product_type"].as_string()
        payload_margin_mode = ExecutionFillModelV2.raw_payload["margin_mode"].as_string()
        query = select(ExecutionFillModelV2).where(
            or_(payload_product_type == product_type, payload_product_type.is_(None)),
            or_(payload_margin_mode == margin_mode, payload_margin_mode.is_(None)),
        )
        scoped_symbols = tuple(symbol for symbol in symbols if symbol)
        if scoped_symbols:
            query = query.where(ExecutionFillModelV2.symbol.in_(scoped_symbols))
        return query


def _fill_row_to_dict(row: ExecutionFillModelV2) -> dict:
    return {
        "fill_id": row.fill_id,
        "venue_fill_id": row.venue_fill_id,
        "order_id": row.order_id,
        "execution_attempt_id": row.execution_attempt_id,
        "venue_order_id": row.venue_order_id,
        "client_order_id": row.client_order_id,
        "decision_id": row.decision_id,
        "intent_id": row.intent_id,
        "symbol": row.symbol,
        "side": row.side,
        "fill_qty": row.fill_qty,
        "fill_price": row.fill_price,
        "fee_amount": row.fee_amount,
        "fee_currency": row.fee_currency,
        "reduce_only": row.reduce_only,
        "close_only": row.close_only,
        "td_mode": row.td_mode,
        "position_mode": row.position_mode,
        "pos_side": row.pos_side,
        "reduce_only_reason": row.reduce_only_reason,
        "close_only_reason": row.close_only_reason,
        "instrument_family": row.instrument_family,
        "settle_currency": row.settle_currency,
        "strategy_family": row.strategy_family,
        "strategy_sleeve_id": row.strategy_sleeve_id,
        "allocation_id": row.allocation_id,
        "strategy_bundle_id": row.strategy_bundle_id,
        "strategy_leg_role": row.strategy_leg_role,
        "liquidity_role": row.liquidity_role,
        "fee_rate": row.fee_rate,
        "exec_type": row.exec_type,
        "exchange_ts": row.exchange_ts,
        "ingestion_ts": row.ingestion_ts,
        "source_system": row.source_system,
        "raw_payload": dict(row.raw_payload),
        "created_at": row.created_at,
    }
