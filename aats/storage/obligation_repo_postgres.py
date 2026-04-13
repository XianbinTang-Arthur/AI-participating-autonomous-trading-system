from __future__ import annotations

import zlib
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.execution import OrderObligation
from aats.services.accounting import remaining_obligation_amount
from aats.storage.sqlalchemy_models import OrderObligationModel


def _currency_advisory_lock_key(currency: str) -> int:
    """crc32 → 正 int32，跨进程稳定。"""
    return zlib.crc32(currency.encode("utf-8")) & 0x7FFFFFFF


class PostgresExecutionObligationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_obligation(self, obligation: OrderObligation) -> OrderObligation:
        with self.session_factory() as session:
            self.save_obligation_in_session(session, obligation)
            session.commit()
            return obligation

    def save_obligation_in_session(self, session: Session, obligation: OrderObligation) -> OrderObligation:
        row = session.get(OrderObligationModel, obligation.client_order_id)
        payload = obligation.model_dump(mode="json")
        if row is None:
            row = OrderObligationModel(
                client_order_id=obligation.client_order_id,
                obligation_id=obligation.obligation_id,
                decision_id=obligation.decision_id,
                intent_id=obligation.intent_id,
                symbol=obligation.symbol,
                reserve_currency=obligation.reserve_currency,
                status=obligation.status,
                reserved_amount=obligation.reserved_amount,
                consumed_amount=obligation.consumed_amount,
                released_amount=obligation.released_amount,
                strategy_family=obligation.strategy_family,
                strategy_sleeve_id=obligation.strategy_sleeve_id,
                allocation_id=obligation.allocation_id,
                strategy_bundle_id=obligation.strategy_bundle_id,
                strategy_leg_role=obligation.strategy_leg_role,
                product_type=obligation.product_type,
                margin_mode=obligation.margin_mode,
                last_update_ts=obligation.last_update_ts,
                created_at=obligation.created_at,
                payload=payload,
            )
            session.add(row)
        else:
            row.obligation_id = obligation.obligation_id
            row.decision_id = obligation.decision_id
            row.intent_id = obligation.intent_id
            row.symbol = obligation.symbol
            row.reserve_currency = obligation.reserve_currency
            row.status = obligation.status
            row.reserved_amount = obligation.reserved_amount
            row.consumed_amount = obligation.consumed_amount
            row.released_amount = obligation.released_amount
            row.strategy_family = obligation.strategy_family
            row.strategy_sleeve_id = obligation.strategy_sleeve_id
            row.allocation_id = obligation.allocation_id
            row.strategy_bundle_id = obligation.strategy_bundle_id
            row.strategy_leg_role = obligation.strategy_leg_role
            row.product_type = obligation.product_type
            row.margin_mode = obligation.margin_mode
            row.last_update_ts = obligation.last_update_ts
            row.created_at = obligation.created_at
            row.payload = payload
        return obligation

    def get_obligation(self, client_order_id: str) -> OrderObligation | None:
        with self.session_factory() as session:
            row = session.get(OrderObligationModel, client_order_id)
        return OrderObligation.model_validate(row.payload) if row is not None else None

    def active_obligations(self) -> list[OrderObligation]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(OrderObligationModel).where(
                    OrderObligationModel.status.in_(("ACTIVE", "PARTIALLY_CONSUMED"))
                )
            ).all()
        return [OrderObligation.model_validate(row.payload) for row in rows]

    def all_obligations(self) -> list[OrderObligation]:
        with self.session_factory() as session:
            rows = session.scalars(select(OrderObligationModel)).all()
        return [OrderObligation.model_validate(row.payload) for row in rows]

    def reserve_obligation_transactional(
        self,
        obligation: OrderObligation,
        snapshot_available_balance: Decimal,
        epsilon: Decimal,
    ) -> OrderObligation:
        """在单个事务内通过 advisory lock 序列化 reservation。

        流程：获取 currency 级 advisory lock → 幂等检查 → 重新读取
        active obligations → 验证可用余额 → 写入。commit 时自动释放锁。
        """
        from aats.services.execution_engine.obligations import ExecutionReservationError

        lock_key = _currency_advisory_lock_key(obligation.reserve_currency)
        with self.session_factory() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            existing = session.get(OrderObligationModel, obligation.client_order_id)
            if existing is not None:
                return OrderObligation.model_validate(existing.payload)
            rows = session.scalars(
                select(OrderObligationModel).where(
                    OrderObligationModel.status.in_(("ACTIVE", "PARTIALLY_CONSUMED")),
                    OrderObligationModel.reserve_currency == obligation.reserve_currency,
                    OrderObligationModel.client_order_id != obligation.client_order_id,
                )
            ).all()
            reserved_elsewhere = sum(
                remaining_obligation_amount(OrderObligation.model_validate(r.payload))
                for r in rows
            )
            available_after = snapshot_available_balance - reserved_elsewhere
            if obligation.reserved_amount > available_after + epsilon:
                raise ExecutionReservationError(
                    "local_obligation_insufficient_available_balance:"
                    f"{obligation.reserve_currency}:"
                    f"{float(obligation.reserved_amount):.12f}>"
                    f"{float(available_after):.12f}"
                )
            self.save_obligation_in_session(session, obligation)
            session.commit()
            return obligation
