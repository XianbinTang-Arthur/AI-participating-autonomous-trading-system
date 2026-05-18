from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import asc, func, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import OrderIntent
from aats.storage.execution_order_labels import execution_order_storage_label
from aats.storage.sqlalchemy_models import ExecutionOrderModel, ExecutionOrderStateHistoryModel


_TERMINAL_ORDER_STATES = ("FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED")


def _is_missing_payload_value(value: Any) -> bool:
    return value is None or value == ""


def _looks_like_order_state_payload(payload: dict[str, Any]) -> bool:
    required_keys = {
        "decision_id",
        "intent_id",
        "symbol",
        "client_order_id",
        "status",
        "requested_qty",
        "remaining_qty",
    }
    return required_keys.issubset(payload.keys()) and (
        "filled_qty" in payload or "exchange_status" in payload
    )


def _order_state_payload_from_raw_payload(raw_payload: dict[str, Any]) -> dict[str, Any] | None:
    nested = raw_payload.get("order_state")
    if isinstance(nested, dict):
        return nested
    if _looks_like_order_state_payload(raw_payload):
        return raw_payload
    return None


def _payload_bool(payload: dict[str, Any], key: str, current: bool) -> bool:
    value = payload.get(key)
    if _is_missing_payload_value(value):
        return current
    return bool(value)


def _payload_text(payload: dict[str, Any], key: str, current: str | None) -> str | None:
    value = payload.get(key)
    if _is_missing_payload_value(value):
        return current
    return str(value)


class PostgresExecutionOrderRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create_order(
        self,
        *,
        order_id: str,
        intent: OrderIntent,
        initial_state: str,
        created_at: datetime,
        raw_payload: dict,
    ) -> None:
        with self.session_factory() as session:
            self.create_order_in_session(
                session,
                order_id=order_id,
                intent=intent,
                initial_state=initial_state,
                created_at=created_at,
                raw_payload=raw_payload,
            )
            session.commit()

    def create_order_in_session(
        self,
        session: Session,
        *,
        order_id: str,
        intent: OrderIntent,
        initial_state: str,
        created_at: datetime,
        raw_payload: dict,
    ) -> None:
        existing = session.get(ExecutionOrderModel, order_id)
        if existing is None:
            existing = session.scalar(select(ExecutionOrderModel).where(ExecutionOrderModel.intent_id == intent.intent_id).limit(1))
        if existing is not None:
            return
        raw_payload_dict = dict(raw_payload or {})
        nested_intent = raw_payload_dict.get("intent") if isinstance(raw_payload_dict.get("intent"), dict) else {}
        payload = dump_payload_exact(raw_payload_dict or intent)
        execution_style = (
            str(intent.execution_style).strip()
            or str(raw_payload_dict.get("execution_style") or "").strip()
            or str(nested_intent.get("execution_style") or "").strip()
            or None
        )
        session.add(
            ExecutionOrderModel(
                order_id=order_id,
                intent_id=intent.intent_id,
                decision_id=intent.decision_id,
                execution_attempt_id=intent.execution_attempt_id,
                client_order_id=str(raw_payload.get("client_order_id") or order_id),
                venue_order_id=raw_payload.get("venue_order_id"),
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                requested_qty=intent.quantity,
                limit_price=intent.limit_price,
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                product_type=intent.product_type,
                margin_mode=intent.margin_mode,
                execution_action=intent.execution_action,
                position_intent=intent.position_intent,
                execution_style=(
                    execution_order_storage_label(execution_style, fallback="unknown")
                    if execution_style
                    else None
                ),
                state=initial_state,
                state_version=1,
                source_system=execution_order_storage_label(
                    raw_payload.get("source_system"),
                    fallback="aats",
                ),
                last_exchange_ts=None,
                created_at=created_at,
                updated_at=created_at,
                raw_payload=payload,
            )
        )

    def get_order(self, order_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.get(ExecutionOrderModel, order_id)
        return _order_row_to_dict(row) if row is not None else None

    def get_order_by_intent(self, intent_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(select(ExecutionOrderModel).where(ExecutionOrderModel.intent_id == intent_id).limit(1))
        return _order_row_to_dict(row) if row is not None else None

    def get_order_by_intent_in_session(self, session: Session, intent_id: str) -> dict | None:
        row = session.scalar(select(ExecutionOrderModel).where(ExecutionOrderModel.intent_id == intent_id).limit(1))
        return _order_row_to_dict(row) if row is not None else None

    def get_order_by_client_order_id(self, client_order_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(select(ExecutionOrderModel).where(ExecutionOrderModel.client_order_id == client_order_id).limit(1))
        return _order_row_to_dict(row) if row is not None else None

    def get_order_by_client_order_id_in_session(
        self,
        session: Session,
        client_order_id: str,
        *,
        for_update: bool = False,
    ) -> dict | None:
        query = select(ExecutionOrderModel).where(ExecutionOrderModel.client_order_id == client_order_id).limit(1)
        if for_update:
            query = query.with_for_update()
        row = session.scalar(query)
        return _order_row_to_dict(row) if row is not None else None

    def update_order_state(
        self,
        *,
        order_id: str,
        expected_state_version: int,
        next_state: str,
        venue_order_id: str | None,
        last_exchange_ts: datetime | None,
        updated_at: datetime,
        raw_payload: dict,
    ) -> None:
        with self.session_factory() as session:
            self.update_order_state_in_session(
                session,
                order_id=order_id,
                expected_state_version=expected_state_version,
                next_state=next_state,
                venue_order_id=venue_order_id,
                last_exchange_ts=last_exchange_ts,
                updated_at=updated_at,
                raw_payload=raw_payload,
            )
            session.commit()

    def update_order_state_in_session(
        self,
        session: Session,
        *,
        order_id: str,
        expected_state_version: int,
        next_state: str,
        venue_order_id: str | None,
        last_exchange_ts: datetime | None,
        updated_at: datetime,
        raw_payload: dict,
    ) -> None:
        row = session.get(ExecutionOrderModel, order_id)
        if row is None:
            raise KeyError(f"execution_order_not_found:{order_id}")
        if row.state_version != expected_state_version:
            raise ValueError(
                f"execution_order_version_conflict order_id={order_id} expected={expected_state_version} actual={row.state_version}"
            )
        row.state = next_state
        row.state_version += 1
        row.venue_order_id = venue_order_id or row.venue_order_id
        row.last_exchange_ts = last_exchange_ts
        row.updated_at = updated_at
        raw_payload_dict = dict(raw_payload or {}) if isinstance(raw_payload, dict) else {}
        order_payload = _order_state_payload_from_raw_payload(raw_payload_dict)
        if isinstance(order_payload, dict):
            row.execution_attempt_id = (
                str(order_payload.get("execution_attempt_id"))
                if order_payload.get("execution_attempt_id") not in {None, ""}
                else row.execution_attempt_id
            )
            row.reduce_only = _payload_bool(order_payload, "reduce_only", row.reduce_only)
            row.close_only = _payload_bool(order_payload, "close_only", row.close_only)
            row.td_mode = _payload_text(order_payload, "td_mode", row.td_mode)
            row.position_mode = _payload_text(order_payload, "position_mode", row.position_mode)
            row.pos_side = _payload_text(order_payload, "pos_side", row.pos_side)
            row.reduce_only_reason = _payload_text(order_payload, "reduce_only_reason", row.reduce_only_reason)
            row.close_only_reason = _payload_text(order_payload, "close_only_reason", row.close_only_reason)
            row.instrument_family = _payload_text(order_payload, "instrument_family", row.instrument_family)
            row.settle_currency = _payload_text(order_payload, "settle_currency", row.settle_currency)
            row.strategy_family = _payload_text(order_payload, "strategy_family", row.strategy_family)
            row.strategy_sleeve_id = _payload_text(order_payload, "strategy_sleeve_id", row.strategy_sleeve_id)
            row.allocation_id = _payload_text(order_payload, "allocation_id", row.allocation_id)
            row.strategy_bundle_id = _payload_text(order_payload, "strategy_bundle_id", row.strategy_bundle_id)
            row.strategy_leg_role = _payload_text(order_payload, "strategy_leg_role", row.strategy_leg_role)
        row.raw_payload = dump_payload_exact(raw_payload_dict)

    def open_orders(self) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ExecutionOrderModel)
                .where(~ExecutionOrderModel.state.in_(_TERMINAL_ORDER_STATES))
                .order_by(asc(ExecutionOrderModel.created_at), asc(ExecutionOrderModel.order_id))
            ).all()
        return [_order_row_to_dict(row) for row in rows]

    def list_orders(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        query = (
            select(ExecutionOrderModel)
            .order_by(
                ExecutionOrderModel.updated_at.desc(),
                ExecutionOrderModel.created_at.desc(),
                ExecutionOrderModel.order_id.desc(),
            )
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [_order_row_to_dict(row) for row in rows]

    def list_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        limit: int | None = None,
        offset: int = 0,
        open_only: bool = False,
    ) -> list[dict]:
        query = (
            self._orders_for_scope_query(
                product_type=product_type,
                margin_mode=margin_mode,
                symbols=symbols,
                open_only=open_only,
            )
            .order_by(
                ExecutionOrderModel.updated_at.desc(),
                ExecutionOrderModel.created_at.desc(),
                ExecutionOrderModel.order_id.desc(),
            )
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [_order_row_to_dict(row) for row in rows]

    def count_orders(self) -> int:
        with self.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(ExecutionOrderModel)) or 0)

    def count_orders_for_scope(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        open_only: bool = False,
    ) -> int:
        query = select(func.count()).select_from(
            self._orders_for_scope_query(
                product_type=product_type,
                margin_mode=margin_mode,
                symbols=symbols,
                open_only=open_only,
            ).subquery()
        )
        with self.session_factory() as session:
            return int(session.scalar(query) or 0)

    def _orders_for_scope_query(
        self,
        *,
        product_type: str,
        margin_mode: str,
        symbols: tuple[str, ...] = (),
        open_only: bool = False,
    ):
        query = select(ExecutionOrderModel).where(
            ExecutionOrderModel.product_type == product_type,
            ExecutionOrderModel.margin_mode == margin_mode,
        )
        scoped_symbols = tuple(symbol for symbol in symbols if symbol)
        if scoped_symbols:
            query = query.where(ExecutionOrderModel.symbol.in_(scoped_symbols))
        if open_only:
            query = query.where(~ExecutionOrderModel.state.in_(_TERMINAL_ORDER_STATES))
        return query


class PostgresExecutionOrderHistoryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def append_transition(
        self,
        *,
        order_id: str,
        from_state: str | None,
        to_state: str,
        reason_code: str | None,
        source: str,
        source_message_id: str | None,
        payload: dict,
        created_at: datetime,
    ) -> None:
        with self.session_factory() as session:
            self.append_transition_in_session(
                session,
                order_id=order_id,
                from_state=from_state,
                to_state=to_state,
                reason_code=reason_code,
                source=source,
                source_message_id=source_message_id,
                payload=payload,
                created_at=created_at,
            )
            session.commit()

    def append_transition_in_session(
        self,
        session: Session,
        *,
        order_id: str,
        from_state: str | None,
        to_state: str,
        reason_code: str | None,
        source: str,
        source_message_id: str | None,
        payload: dict,
        created_at: datetime,
    ) -> None:
        session.add(
            ExecutionOrderStateHistoryModel(
                order_id=order_id,
                from_state=from_state,
                to_state=to_state,
                reason_code=reason_code,
                source=source,
                source_message_id=source_message_id,
                payload=dump_payload_exact(payload),
                created_at=created_at,
            )
        )

    def history_for_order(self, order_id: str) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ExecutionOrderStateHistoryModel)
                .where(ExecutionOrderStateHistoryModel.order_id == order_id)
                .order_by(ExecutionOrderStateHistoryModel.id)
            ).all()
        return [_order_history_row_to_dict(row) for row in rows]


def _order_row_to_dict(row: ExecutionOrderModel) -> dict:
    return {
        "order_id": row.order_id,
        "intent_id": row.intent_id,
        "decision_id": row.decision_id,
        "execution_attempt_id": row.execution_attempt_id,
        "client_order_id": row.client_order_id,
        "venue_order_id": row.venue_order_id,
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "time_in_force": row.time_in_force,
        "requested_qty": row.requested_qty,
        "limit_price": row.limit_price,
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
        "product_type": row.product_type,
        "margin_mode": row.margin_mode,
        "execution_action": row.execution_action,
        "position_intent": row.position_intent,
        "execution_style": row.execution_style,
        "state": row.state,
        "state_version": row.state_version,
        "source_system": row.source_system,
        "last_exchange_ts": row.last_exchange_ts,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "raw_payload": dict(row.raw_payload),
    }


def _order_history_row_to_dict(row: ExecutionOrderStateHistoryModel) -> dict:
    return {
        "id": row.id,
        "order_id": row.order_id,
        "from_state": row.from_state,
        "to_state": row.to_state,
        "reason_code": row.reason_code,
        "source": row.source,
        "source_message_id": row.source_message_id,
        "payload": dict(row.payload),
        "created_at": row.created_at,
    }
