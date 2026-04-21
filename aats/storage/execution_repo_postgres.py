from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from aats.bootstrap.logging import get_logger, log_event
from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import FillEvent, OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.scope_metadata import fill_scope_metadata, order_scope_metadata
from aats.storage.sqlalchemy_models import FillEventModel, OrderStateModel


class PostgresExecutionRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.state_machine = OrderStateMachine()
        self.logger = get_logger("aats.execution_repo")

    def save_order_state(self, state: OrderState) -> OrderState:
        with self.session_factory() as session:
            merged, _current = self.save_order_state_in_session(session, state)
            session.commit()
            return merged

    def save_order_state_in_session(self, session: Session, state: OrderState) -> tuple[OrderState, OrderState | None]:
        row = session.get(OrderStateModel, state.client_order_id)
        if row is None:
            row = session.scalar(
                select(OrderStateModel).where(OrderStateModel.intent_id == state.intent_id).limit(1)
            )
        current = self._to_order_state(row) if row is not None else None
        validation = self.state_machine.validate_transition(
            current_status=None if current is None else current.status,
            next_status=state.status,
        )
        if not validation.accepted:
            log_event(
                self.logger,
                "order_state_transition_rejected",
                level="warning",
                decision_id=state.decision_id,
                intent_id=state.intent_id,
                order_id=state.client_order_id,
                current_status=None if current is None else current.status,
                incoming_status=state.status,
                reason=validation.reason,
            )
            if validation.reason == "invalid_transition":
                raise ValueError(
                    f"invalid_order_state_transition current={None if current is None else current.status} next={state.status}"
                )
        merged = self.state_machine.merge(current=current, incoming=state)
        payload = dump_payload_exact(merged)
        scope = order_scope_metadata(merged)
        if row is None:
            row = OrderStateModel(
                client_order_id=merged.client_order_id,
                decision_id=merged.decision_id,
                intent_id=merged.intent_id,
                symbol=merged.symbol,
                exchange_order_id=merged.exchange_order_id,
                created_at=merged.created_at,
                status=merged.status,
                submitted_ts=merged.submitted_ts,
                last_update_ts=merged.last_update_ts,
                requested_qty=merged.requested_qty,
                filled_qty=merged.filled_qty,
                remaining_qty=merged.remaining_qty,
                average_fill_price=merged.average_fill_price,
                fees=merged.fees,
                reduce_only=merged.reduce_only,
                close_only=merged.close_only,
                td_mode=merged.td_mode,
                position_mode=merged.position_mode,
                pos_side=merged.pos_side,
                reduce_only_reason=merged.reduce_only_reason,
                close_only_reason=merged.close_only_reason,
                instrument_family=merged.instrument_family,
                settle_currency=merged.settle_currency,
                strategy_family=merged.strategy_family,
                strategy_sleeve_id=merged.strategy_sleeve_id,
                allocation_id=merged.allocation_id,
                strategy_bundle_id=merged.strategy_bundle_id,
                strategy_leg_role=merged.strategy_leg_role,
                product_type=scope["product_type"],
                margin_mode=scope["margin_mode"],
                position_intent=scope["position_intent"],
                payload=payload,
            )
            session.add(row)
        else:
            if row.client_order_id != merged.client_order_id:
                session.delete(row)
                session.flush()
                row = OrderStateModel(client_order_id=merged.client_order_id)
                session.add(row)
            row.decision_id = merged.decision_id
            row.intent_id = merged.intent_id
            row.symbol = merged.symbol
            row.exchange_order_id = merged.exchange_order_id
            row.created_at = merged.created_at
            row.status = merged.status
            row.submitted_ts = merged.submitted_ts
            row.last_update_ts = merged.last_update_ts
            row.requested_qty = merged.requested_qty
            row.filled_qty = merged.filled_qty
            row.remaining_qty = merged.remaining_qty
            row.average_fill_price = merged.average_fill_price
            row.fees = merged.fees
            row.reduce_only = merged.reduce_only
            row.close_only = merged.close_only
            row.td_mode = merged.td_mode
            row.position_mode = merged.position_mode
            row.pos_side = merged.pos_side
            row.reduce_only_reason = merged.reduce_only_reason
            row.close_only_reason = merged.close_only_reason
            row.instrument_family = merged.instrument_family
            row.settle_currency = merged.settle_currency
            row.strategy_family = merged.strategy_family
            row.strategy_sleeve_id = merged.strategy_sleeve_id
            row.allocation_id = merged.allocation_id
            row.strategy_bundle_id = merged.strategy_bundle_id
            row.strategy_leg_role = merged.strategy_leg_role
            row.product_type = scope["product_type"]
            row.margin_mode = scope["margin_mode"]
            row.position_intent = scope["position_intent"]
            row.payload = payload
        return merged, current

    def has_intent(self, intent_id: str) -> bool:
        with self.session_factory() as session:
            return session.scalar(select(OrderStateModel.intent_id).where(OrderStateModel.intent_id == intent_id)) is not None

    def save_fill(self, fill: FillEvent) -> bool:
        scope = fill_scope_metadata(fill)
        with self.session_factory() as session:
            saved = self.save_fill_in_session(session, fill, scope=scope)
            session.commit()
            return saved

    def save_fill_in_session(
        self,
        session: Session,
        fill: FillEvent,
        *,
        scope: dict[str, str | None] | None = None,
    ) -> bool:
        # Stage 5：原子幂等插入。
        # 旧版用 SELECT-then-INSERT 检查重复——同进程内 reconciliation 重放 +
        # 实时 fill outbox 重投递可能两个 worker 都通过 SELECT 然后都 INSERT，
        # 第二个会抛 IntegrityError 而不是 silently return False。
        # 改用 Postgres 原生 INSERT ... ON CONFLICT (fill_id) DO NOTHING：
        #   - rowcount == 1：插入成功，return True
        #   - rowcount == 0：fill_id 已存在，return False（幂等）
        # 这样把"check 然后 insert"两步合并成一条原子 SQL，消除 TOCTOU race。
        resolved_scope = scope or fill_scope_metadata(fill)
        stmt = (
            pg_insert(FillEventModel)
            .values(
                fill_id=fill.fill_id,
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                client_order_id=fill.client_order_id,
                exchange_order_id=fill.exchange_order_id,
                symbol=fill.symbol,
                side=fill.side,
                fill_qty=fill.fill_qty,
                fill_price=fill.fill_price,
                fee_amount=fill.fee_amount,
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
                product_type=resolved_scope["product_type"],
                margin_mode=resolved_scope["margin_mode"],
                position_intent=resolved_scope["position_intent"],
                exchange_timestamp=fill.exchange_timestamp,
                ingestion_timestamp=fill.ingestion_timestamp,
                created_at=fill.created_at,
                payload=dump_payload_exact(fill),
            )
            .on_conflict_do_nothing(index_elements=["fill_id"])
            .returning(FillEventModel.fill_id)
        )
        inserted = session.scalar(stmt)
        return inserted is not None

    def order_states(self) -> list[OrderState]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(OrderStateModel).order_by(OrderStateModel.created_at, OrderStateModel.client_order_id)
            ).all()
        return [self._to_order_state(row) for row in rows]

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        with self.session_factory() as session:
            row = session.get(OrderStateModel, client_order_id)
        return self._to_order_state(row) if row is not None else None

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        with self.session_factory() as session:
            query = select(OrderStateModel)
            if statuses is not None:
                query = query.where(OrderStateModel.status.in_(tuple(statuses)))
            rows = session.scalars(
                query.order_by(desc(OrderStateModel.last_update_ts), desc(OrderStateModel.created_at)).limit(limit)
            ).all()
        return [self._to_order_state(row) for row in rows]

    def open_order_states(self) -> list[OrderState]:
        final_statuses = ("FILLED", "CANCELED", "REJECTED", "BLOCKED", "DRY_RUN", "FAILED", "EXPIRED")
        with self.session_factory() as session:
            rows = session.scalars(
                select(OrderStateModel)
                .where(~OrderStateModel.status.in_(final_statuses))
                .order_by(OrderStateModel.created_at, OrderStateModel.client_order_id)
            ).all()
        return [self._to_order_state(row) for row in rows]

    def fills(self) -> list[FillEvent]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillEventModel).order_by(
                    FillEventModel.exchange_timestamp,
                    FillEventModel.ingestion_timestamp,
                    FillEventModel.fill_id,
                )
            ).all()
        return [self._to_fill_event(row) for row in rows]

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillEventModel)
                .where(FillEventModel.client_order_id == client_order_id)
                .order_by(
                    FillEventModel.exchange_timestamp,
                    FillEventModel.ingestion_timestamp,
                    FillEventModel.fill_id,
                )
            ).all()
        return [self._to_fill_event(row) for row in rows]

    def fills_for_decisions(self, decision_ids: list[str]) -> list[FillEvent]:
        # 2026-04-21：SQL-side filter 替代旧 `fills() + [if decision_id in allowed]`
        # 载入全表 + Python 过滤的反模式。FillEventModel.decision_id 有 index
        # （见 sqlalchemy_models.py:417），WHERE IN ANY 极快；fills 表无界增长，
        # 替代前版在 AI shadow evaluation 里会扫全表。
        if not decision_ids:
            return []
        # 去重并保持 deterministic order（便于 EXPLAIN 可复现）
        unique_ids = sorted({str(did) for did in decision_ids if did})
        if not unique_ids:
            return []
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillEventModel)
                .where(FillEventModel.decision_id.in_(unique_ids))
                .order_by(
                    FillEventModel.exchange_timestamp,
                    FillEventModel.ingestion_timestamp,
                    FillEventModel.fill_id,
                )
            ).all()
        return [self._to_fill_event(row) for row in rows]

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        query = select(FillEventModel)
        if since is not None:
            query = query.where(FillEventModel.ingestion_timestamp >= since)
        query = query.order_by(
            FillEventModel.exchange_timestamp,
            FillEventModel.ingestion_timestamp,
            FillEventModel.fill_id,
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_fill_event(row) for row in rows]

    def order_states_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        statuses: tuple[str, ...] | None = None,
        limit: int | None = None,
        open_only: bool = False,
    ) -> list[OrderState]:
        query = select(OrderStateModel)
        if open_only:
            final_statuses = ("FILLED", "CANCELED", "REJECTED", "BLOCKED", "DRY_RUN", "FAILED", "EXPIRED")
            query = query.where(~OrderStateModel.status.in_(final_statuses))
        if statuses is not None:
            query = query.where(OrderStateModel.status.in_(tuple(statuses)))
        query = self._scope_order_query(query, scope).order_by(OrderStateModel.created_at, OrderStateModel.client_order_id)
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_order_state(row) for row in rows]

    def fills_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        query = select(FillEventModel)
        if since is not None:
            query = query.where(FillEventModel.ingestion_timestamp >= since)
        query = self._scope_fill_query(query, scope).order_by(
            FillEventModel.exchange_timestamp,
            FillEventModel.ingestion_timestamp,
            FillEventModel.fill_id,
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_fill_event(row) for row in rows]

    @staticmethod
    def _symbol_clause(model, scope: RuntimeStateScope):
        allowed_symbols = tuple(scope.allowed_symbols) if scope.allowed_symbols else (scope.default_symbol,)
        return model.symbol.in_(allowed_symbols)

    @classmethod
    def _scope_order_query(cls, query, scope: RuntimeStateScope):
        symbol_clause = cls._symbol_clause(OrderStateModel, scope)
        regular_clause = and_(
            symbol_clause,
            OrderStateModel.product_type == scope.product_type,
            OrderStateModel.margin_mode == scope.margin_mode,
            or_(OrderStateModel.strategy_family.is_(None), OrderStateModel.strategy_family != "smart_arbitrage"),
        )
        if scope.product_type == "spot":
            smart_clause = and_(
                symbol_clause,
                OrderStateModel.strategy_family == "smart_arbitrage",
                OrderStateModel.product_type == "spot",
                OrderStateModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
            )
            return query.where(or_(regular_clause, smart_clause))
        if scope.product_type != "derivatives":
            return query.where(regular_clause)
        smart_clause = and_(
            symbol_clause,
            OrderStateModel.strategy_family == "smart_arbitrage",
            or_(
                and_(
                    OrderStateModel.product_type == "spot",
                    OrderStateModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
                ),
                and_(OrderStateModel.product_type == scope.product_type, OrderStateModel.margin_mode == scope.margin_mode),
            ),
        )
        return query.where(or_(regular_clause, smart_clause))

    @classmethod
    def _scope_fill_query(cls, query, scope: RuntimeStateScope):
        symbol_clause = cls._symbol_clause(FillEventModel, scope)
        regular_clause = and_(
            symbol_clause,
            FillEventModel.product_type == scope.product_type,
            FillEventModel.margin_mode == scope.margin_mode,
            or_(FillEventModel.strategy_family.is_(None), FillEventModel.strategy_family != "smart_arbitrage"),
        )
        if scope.product_type == "spot":
            smart_clause = and_(
                symbol_clause,
                FillEventModel.strategy_family == "smart_arbitrage",
                FillEventModel.product_type == "spot",
                FillEventModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
            )
            return query.where(or_(regular_clause, smart_clause))
        if scope.product_type != "derivatives":
            return query.where(regular_clause)
        smart_clause = and_(
            symbol_clause,
            FillEventModel.strategy_family == "smart_arbitrage",
            or_(
                and_(
                    FillEventModel.product_type == "spot",
                    FillEventModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
                ),
                and_(FillEventModel.product_type == scope.product_type, FillEventModel.margin_mode == scope.margin_mode),
            ),
        )
        return query.where(or_(regular_clause, smart_clause))

    @staticmethod
    def _to_order_state(row: OrderStateModel) -> OrderState:
        payload = dict(row.payload)
        payload.setdefault("decision_id", row.decision_id)
        payload.setdefault("intent_id", row.intent_id)
        payload.setdefault("symbol", row.symbol)
        payload.setdefault("client_order_id", row.client_order_id)
        payload.setdefault("exchange_order_id", row.exchange_order_id)
        payload.setdefault("status", row.status)
        payload.setdefault("submitted_ts", row.submitted_ts)
        payload.setdefault("last_update_ts", row.last_update_ts)
        payload.setdefault("requested_qty", row.requested_qty)
        payload.setdefault("filled_qty", row.filled_qty)
        payload.setdefault("remaining_qty", row.remaining_qty)
        payload.setdefault("average_fill_price", row.average_fill_price)
        payload.setdefault("fees", row.fees)
        payload.setdefault("reduce_only", False if row.reduce_only is None else row.reduce_only)
        payload.setdefault("close_only", False if row.close_only is None else row.close_only)
        payload.setdefault("td_mode", row.td_mode)
        payload.setdefault("position_mode", row.position_mode)
        payload.setdefault("pos_side", row.pos_side)
        payload.setdefault("reduce_only_reason", row.reduce_only_reason)
        payload.setdefault("close_only_reason", row.close_only_reason)
        payload.setdefault("instrument_family", row.instrument_family)
        payload.setdefault("settle_currency", row.settle_currency)
        payload.setdefault("strategy_family", row.strategy_family)
        payload.setdefault("strategy_sleeve_id", row.strategy_sleeve_id)
        payload.setdefault("allocation_id", row.allocation_id)
        payload.setdefault("strategy_bundle_id", row.strategy_bundle_id)
        payload.setdefault("strategy_leg_role", row.strategy_leg_role)
        payload.setdefault("product_type", row.product_type)
        payload.setdefault("margin_mode", row.margin_mode)
        payload.setdefault("position_intent", row.position_intent)
        return OrderState.model_validate(payload)

    @staticmethod
    def _to_fill_event(row: FillEventModel) -> FillEvent:
        payload = dict(row.payload)
        payload.setdefault("fill_id", row.fill_id)
        payload.setdefault("decision_id", row.decision_id)
        payload.setdefault("intent_id", row.intent_id)
        payload.setdefault("client_order_id", row.client_order_id)
        payload.setdefault("exchange_order_id", row.exchange_order_id)
        payload.setdefault("symbol", row.symbol)
        payload.setdefault("side", row.side)
        payload.setdefault("fill_qty", row.fill_qty)
        payload.setdefault("fill_price", row.fill_price)
        payload.setdefault("fee_amount", row.fee_amount)
        payload.setdefault("reduce_only", False if row.reduce_only is None else row.reduce_only)
        payload.setdefault("close_only", False if row.close_only is None else row.close_only)
        payload.setdefault("td_mode", row.td_mode)
        payload.setdefault("position_mode", row.position_mode)
        payload.setdefault("pos_side", row.pos_side)
        payload.setdefault("reduce_only_reason", row.reduce_only_reason)
        payload.setdefault("close_only_reason", row.close_only_reason)
        payload.setdefault("instrument_family", row.instrument_family)
        payload.setdefault("settle_currency", row.settle_currency)
        payload.setdefault("strategy_family", row.strategy_family)
        payload.setdefault("strategy_sleeve_id", row.strategy_sleeve_id)
        payload.setdefault("allocation_id", row.allocation_id)
        payload.setdefault("strategy_bundle_id", row.strategy_bundle_id)
        payload.setdefault("strategy_leg_role", row.strategy_leg_role)
        payload.setdefault("product_type", row.product_type)
        payload.setdefault("margin_mode", row.margin_mode)
        payload.setdefault("position_intent", row.position_intent)
        payload.setdefault("exchange_timestamp", row.exchange_timestamp)
        payload.setdefault("ingestion_timestamp", row.ingestion_timestamp)
        payload.setdefault("liquidity_role", payload.get("liquidity_role") or "taker")
        return FillEvent.model_validate(payload)
