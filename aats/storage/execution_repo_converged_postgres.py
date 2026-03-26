from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.base import ExecutionRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.sqlalchemy_models import ExecutionFillModelV2, ExecutionOrderModel


_TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}


class ConvergedPostgresExecutionRepository(ExecutionRepository):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        execution_order_repo: PostgresExecutionOrderRepository,
        execution_order_history_repo: PostgresExecutionOrderHistoryRepository | None,
        execution_fill_repo: PostgresExecutionFillRepositoryV2,
    ) -> None:
        self.session_factory = session_factory
        self.execution_order_repo = execution_order_repo
        self.execution_order_history_repo = execution_order_history_repo
        self.execution_fill_repo = execution_fill_repo
        self.state_machine = OrderStateMachine()

    def save_order_state(self, state: OrderState) -> OrderState:
        with self.session_factory() as session:
            persisted, _previous = self.save_order_state_in_session(session, state)
            session.commit()
        return persisted

    def save_order_state_in_session(self, session: Session, state: OrderState) -> tuple[OrderState, OrderState | None]:
        existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
            session,
            state.client_order_id,
            for_update=True,
        )
        previous = self._hydrate_order_state(existing) if existing is not None else None
        validation = self.state_machine.validate_transition(
            current_status=None if previous is None else previous.status,
            next_status=state.status,
        )
        if not validation.accepted and validation.reason == "invalid_transition":
            raise ValueError(
                f"invalid_order_state_transition current={None if previous is None else previous.status} next={state.status}"
            )
        merged = self.state_machine.merge(current=previous, incoming=state)
        raw_payload = {
            "source_system": merged.submission_mode or "converged_execution_repo",
            "order_state": dump_payload_exact(merged),
        }
        if existing is None:
            self.execution_order_repo.create_order_in_session(
                session,
                order_id=merged.client_order_id,
                intent=self._intent_from_order_state(merged),
                initial_state=merged.status,
                created_at=merged.created_at,
                raw_payload=raw_payload,
            )
            if self.execution_order_history_repo is not None:
                self.execution_order_history_repo.append_transition_in_session(
                    session,
                    order_id=merged.client_order_id,
                    from_state=None,
                    to_state=merged.status,
                    reason_code="converged_execution_seed",
                    source="execution_repo",
                    source_message_id=merged.intent_id,
                    payload=dump_payload_exact(merged),
                    created_at=merged.last_update_ts or merged.created_at,
                )
        else:
            self.execution_order_repo.update_order_state_in_session(
                session,
                order_id=str(existing["order_id"]),
                expected_state_version=int(existing["state_version"]),
                next_state=merged.status,
                venue_order_id=merged.exchange_order_id,
                last_exchange_ts=merged.last_exchange_update_ts or merged.last_update_ts,
                updated_at=merged.last_update_ts or merged.created_at,
                raw_payload=raw_payload,
            )
            if self.execution_order_history_repo is not None and previous is not None and previous.status != merged.status:
                self.execution_order_history_repo.append_transition_in_session(
                    session,
                    order_id=str(existing["order_id"]),
                    from_state=previous.status,
                    to_state=merged.status,
                    reason_code="converged_execution_update",
                    source="execution_repo",
                    source_message_id=merged.intent_id,
                    payload=dump_payload_exact(merged),
                    created_at=merged.last_update_ts or merged.created_at,
                )
        return merged, previous

    def has_intent(self, intent_id: str) -> bool:
        return self.execution_order_repo.get_order_by_intent(intent_id) is not None

    def save_fill(self, fill: FillEvent) -> bool:
        with self.session_factory() as session:
            saved = self.save_fill_in_session(session, fill)
            session.commit()
            return saved

    def save_fill_in_session(self, session: Session, fill: FillEvent) -> bool:
        existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
            session,
            fill.client_order_id,
            for_update=True,
        )
        if existing is None:
            synthetic_intent = Phase1ExecutionShadowService.intent_from_fill(fill)
            self.execution_order_repo.create_order_in_session(
                session,
                order_id=fill.client_order_id,
                intent=synthetic_intent,
                initial_state=fill.order_status_after_fill or "FILLED",
                created_at=fill.created_at,
                raw_payload={
                    "source_system": "converged_fill_backfill",
                    "client_order_id": fill.client_order_id,
                    "venue_order_id": fill.exchange_order_id,
                    "fill_event": dump_payload_exact(fill),
                },
            )
            order_id = fill.client_order_id
        else:
            order_id = str(existing["order_id"])
        return self.execution_fill_repo.save_fill_in_session(
            session,
            fill=fill,
            order_id=order_id,
            source=fill.venue.lower(),
            raw_payload={
                "venue_fill_id": fill.fill_id,
                "fill_event": dump_payload_exact(fill),
            },
        )

    def order_states(self) -> list[OrderState]:
        return [self._hydrate_order_state(row) for row in self.execution_order_repo.list_orders(limit=None)]

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        row = self.execution_order_repo.get_order_by_client_order_id(client_order_id)
        return None if row is None else self._hydrate_order_state(row)

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        states = self.order_states()
        if statuses is not None:
            status_set = set(statuses)
            states = [state for state in states if state.status in status_set]
        states.sort(key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id), reverse=True)
        return states[:limit]

    def open_order_states(self) -> list[OrderState]:
        return [self._hydrate_order_state(row) for row in self.execution_order_repo.open_orders()]

    def fills(self) -> list[FillEvent]:
        return [self._hydrate_fill(row) for row in self.execution_fill_repo.fills_since(limit=None)]

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        return [self._hydrate_fill(row) for row in self.execution_fill_repo.fills_since(since=since, limit=limit)]

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        row = self.execution_order_repo.get_order_by_client_order_id(client_order_id)
        order_id = client_order_id if row is None else str(row["order_id"])
        return [self._hydrate_fill(fill) for fill in self.execution_fill_repo.fills_for_order(order_id)]

    def order_states_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        statuses: tuple[str, ...] | None = None,
        limit: int | None = None,
        open_only: bool = False,
    ) -> list[OrderState]:
        query = select(ExecutionOrderModel)
        if open_only:
            query = query.where(~ExecutionOrderModel.state.in_(_TERMINAL_STATUSES))
        if statuses is not None:
            query = query.where(ExecutionOrderModel.state.in_(tuple(statuses)))
        query = self._scope_order_query(query, scope).order_by(
            ExecutionOrderModel.updated_at.desc(),
            ExecutionOrderModel.created_at.desc(),
            ExecutionOrderModel.order_id.desc(),
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.execute(query).scalars().all()
        return [self._hydrate_order_state(_order_model_to_dict(row)) for row in rows]

    def fills_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        query = select(ExecutionFillModelV2)
        if since is not None:
            query = query.where(ExecutionFillModelV2.ingestion_ts >= since)
        query = self._scope_fill_query(query, scope).order_by(
            ExecutionFillModelV2.ingestion_ts.asc(),
            ExecutionFillModelV2.fill_id.asc(),
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.execute(query).scalars().all()
        return [self._hydrate_fill(_fill_model_to_dict(row)) for row in rows]

    @staticmethod
    def _symbol_clause(model, scope: RuntimeStateScope):
        allowed_symbols = tuple(scope.allowed_symbols) if scope.allowed_symbols else (scope.default_symbol,)
        return model.symbol.in_(allowed_symbols)

    @classmethod
    def _scope_order_query(cls, query, scope: RuntimeStateScope):
        symbol_clause = cls._symbol_clause(ExecutionOrderModel, scope)
        regular_clause = and_(
            symbol_clause,
            ExecutionOrderModel.product_type == scope.product_type,
            ExecutionOrderModel.margin_mode == scope.margin_mode,
            or_(ExecutionOrderModel.strategy_family.is_(None), ExecutionOrderModel.strategy_family != "smart_arbitrage"),
        )
        if scope.product_type == "spot":
            smart_clause = and_(
                symbol_clause,
                ExecutionOrderModel.strategy_family == "smart_arbitrage",
                ExecutionOrderModel.product_type == "spot",
                ExecutionOrderModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
            )
            return query.where(or_(regular_clause, smart_clause))
        if scope.product_type != "derivatives":
            return query.where(regular_clause)
        smart_clause = and_(
            symbol_clause,
            ExecutionOrderModel.strategy_family == "smart_arbitrage",
            or_(
                and_(
                    ExecutionOrderModel.product_type == "spot",
                    ExecutionOrderModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
                ),
                and_(ExecutionOrderModel.product_type == scope.product_type, ExecutionOrderModel.margin_mode == scope.margin_mode),
            ),
        )
        return query.where(or_(regular_clause, smart_clause))

    @classmethod
    def _scope_fill_query(cls, query, scope: RuntimeStateScope):
        symbol_clause = cls._symbol_clause(ExecutionFillModelV2, scope)
        query = query.join(ExecutionOrderModel, ExecutionOrderModel.order_id == ExecutionFillModelV2.order_id)
        regular_clause = and_(
            symbol_clause,
            ExecutionOrderModel.product_type == scope.product_type,
            ExecutionOrderModel.margin_mode == scope.margin_mode,
            or_(ExecutionOrderModel.strategy_family.is_(None), ExecutionOrderModel.strategy_family != "smart_arbitrage"),
        )
        if scope.product_type == "spot":
            smart_clause = and_(
                symbol_clause,
                ExecutionOrderModel.strategy_family == "smart_arbitrage",
                ExecutionOrderModel.product_type == "spot",
                ExecutionOrderModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
            )
            return query.where(or_(regular_clause, smart_clause))
        if scope.product_type != "derivatives":
            return query.where(regular_clause)
        smart_clause = and_(
            symbol_clause,
            ExecutionOrderModel.strategy_family == "smart_arbitrage",
            or_(
                and_(
                    ExecutionOrderModel.product_type == "spot",
                    ExecutionOrderModel.margin_mode.in_(tuple(scope.smart_arbitrage_spot_margin_modes)),
                ),
                and_(ExecutionOrderModel.product_type == scope.product_type, ExecutionOrderModel.margin_mode == scope.margin_mode),
            ),
        )
        return query.where(or_(regular_clause, smart_clause))

    @staticmethod
    def _hydrate_order_state(row: dict) -> OrderState:
        payload = dict(row.get("raw_payload") or {})
        order_payload = payload.get("order_state")
        if isinstance(order_payload, dict):
            return OrderState.model_validate(order_payload)
        return OrderState(
            decision_id=str(row.get("decision_id") or ""),
            intent_id=str(row.get("intent_id") or ""),
            symbol=str(row.get("symbol") or ""),
            client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
            exchange_order_id=row.get("venue_order_id"),
            status=str(row.get("state") or "CREATED"),
            submission_mode=str(payload.get("source_system") or "converged_execution_repo"),
            submitted_ts=row.get("created_at") if str(row.get("state") or "CREATED") != "CREATED" else None,
            last_update_ts=row.get("updated_at") or row.get("created_at"),
            last_exchange_update_ts=row.get("last_exchange_ts"),
            requested_qty=Decimal(str(row.get("requested_qty") or "0")),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal(str(row.get("requested_qty") or "0")),
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=bool(row.get("reduce_only", False)),
            close_only=bool(row.get("close_only", False)),
            td_mode=(
                row.get("td_mode")
                or payload.get("td_mode")
                or payload.get("fill_event", {}).get("td_mode")
                or row.get("margin_mode")
            ),
            position_mode=row.get("position_mode") or payload.get("position_mode"),
            pos_side=row.get("pos_side") or payload.get("pos_side"),
            reduce_only_reason=row.get("reduce_only_reason") or payload.get("reduce_only_reason"),
            close_only_reason=row.get("close_only_reason") or payload.get("close_only_reason"),
            instrument_family=row.get("instrument_family") or payload.get("instrument_family"),
            settle_currency=row.get("settle_currency") or payload.get("settle_currency"),
            strategy_family=row.get("strategy_family") or payload.get("strategy_family"),
            strategy_sleeve_id=row.get("strategy_sleeve_id") or payload.get("strategy_sleeve_id"),
            allocation_id=row.get("allocation_id") or payload.get("allocation_id"),
            strategy_bundle_id=row.get("strategy_bundle_id") or payload.get("strategy_bundle_id"),
            strategy_leg_role=row.get("strategy_leg_role") or payload.get("strategy_leg_role"),
            strategy_pair_id=payload.get("strategy_pair_id"),
            strategy_opportunity_kind=payload.get("strategy_opportunity_kind"),
            strategy_execution_mode=payload.get("strategy_execution_mode"),
            strategy_state_phase=payload.get("strategy_state_phase"),
            product_type=str(row.get("product_type") or "spot"),
            margin_mode=str(row.get("margin_mode") or "cash"),
            target_leverage=float(payload.get("target_leverage") or 1.0),
            exposure_side=str(payload.get("exposure_side") or "flat"),
            execution_action=row.get("execution_action"),
            position_intent=str(row.get("position_intent") or "open_long"),
            submission_payload={},
        )

    @staticmethod
    def _hydrate_fill(row: dict) -> FillEvent:
        payload = dict(row.get("raw_payload") or {})
        fill_payload = payload.get("fill_event")
        if isinstance(fill_payload, dict):
            return FillEvent.model_validate(fill_payload)
        return FillEvent(
            fill_id=str(row["fill_id"]),
            decision_id=str(row.get("decision_id") or ""),
            intent_id=str(row.get("intent_id") or ""),
            client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
            exchange_order_id=str(row.get("venue_order_id") or ""),
            symbol=str(row["symbol"]),
            venue=str(row.get("source_system") or "OKX").upper(),
            side=str(row["side"]),
            fill_qty=Decimal(str(row["fill_qty"])),
            fill_price=Decimal(str(row["fill_price"])),
            fee_amount=Decimal(str(row.get("fee_amount") or "0")),
            fee_currency=row.get("fee_currency"),
            reduce_only=bool(row.get("reduce_only", False)),
            close_only=bool(row.get("close_only", False)),
            td_mode=row.get("td_mode") or payload.get("td_mode"),
            position_mode=row.get("position_mode") or payload.get("position_mode"),
            pos_side=row.get("pos_side") or payload.get("pos_side"),
            reduce_only_reason=row.get("reduce_only_reason") or payload.get("reduce_only_reason"),
            close_only_reason=row.get("close_only_reason") or payload.get("close_only_reason"),
            instrument_family=row.get("instrument_family") or payload.get("instrument_family"),
            settle_currency=row.get("settle_currency") or payload.get("settle_currency"),
            strategy_family=row.get("strategy_family") or payload.get("strategy_family"),
            strategy_sleeve_id=row.get("strategy_sleeve_id") or payload.get("strategy_sleeve_id"),
            allocation_id=row.get("allocation_id") or payload.get("allocation_id"),
            strategy_bundle_id=row.get("strategy_bundle_id") or payload.get("strategy_bundle_id"),
            strategy_leg_role=row.get("strategy_leg_role") or payload.get("strategy_leg_role"),
            strategy_pair_id=payload.get("strategy_pair_id"),
            strategy_opportunity_kind=payload.get("strategy_opportunity_kind"),
            strategy_execution_mode=payload.get("strategy_execution_mode"),
            strategy_state_phase=payload.get("strategy_state_phase"),
            liquidity_role=str(row.get("liquidity_role") or "taker"),
            exchange_timestamp=row["exchange_ts"],
            ingestion_timestamp=row["ingestion_ts"],
        )

    @staticmethod
    def _intent_from_order_state(order_state: OrderState) -> OrderIntent:
        side = "buy"
        if order_state.position_intent in {"open_short", "reduce_short", "close_short"}:
            side = "sell"
        return OrderIntent(
            intent_id=order_state.intent_id,
            decision_id=order_state.decision_id,
            symbol=order_state.symbol,
            side=side,
            quantity=order_state.requested_qty,
            execution_style=order_state.submission_mode or "converged_execution_repo",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=order_state.reduce_only,
            close_only=order_state.close_only,
            td_mode=order_state.td_mode,
            position_mode=order_state.position_mode,
            pos_side=order_state.pos_side,
            reduce_only_reason=order_state.reduce_only_reason,
            close_only_reason=order_state.close_only_reason,
            instrument_family=order_state.instrument_family,
            settle_currency=order_state.settle_currency,
            strategy_family=order_state.strategy_family,
            strategy_sleeve_id=order_state.strategy_sleeve_id,
            allocation_id=order_state.allocation_id,
            strategy_bundle_id=order_state.strategy_bundle_id,
            strategy_leg_role=order_state.strategy_leg_role,
            strategy_pair_id=order_state.strategy_pair_id,
            strategy_opportunity_kind=order_state.strategy_opportunity_kind,
            strategy_execution_mode=order_state.strategy_execution_mode,
            strategy_state_phase=order_state.strategy_state_phase,
            idempotency_key=order_state.client_order_id,
            product_type=order_state.product_type,
            target_leverage=order_state.target_leverage,
            margin_mode=order_state.margin_mode,
            exposure_side=order_state.exposure_side,
            execution_action=order_state.execution_action,
            position_intent=order_state.position_intent,
        )


def _order_model_to_dict(row: ExecutionOrderModel) -> dict:
    return {
        "order_id": row.order_id,
        "intent_id": row.intent_id,
        "decision_id": row.decision_id,
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
        "state": row.state,
        "state_version": row.state_version,
        "source_system": row.source_system,
        "last_exchange_ts": row.last_exchange_ts,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "raw_payload": dict(row.raw_payload),
    }


def _fill_model_to_dict(row: ExecutionFillModelV2) -> dict:
    return {
        "fill_id": row.fill_id,
        "venue_fill_id": row.venue_fill_id,
        "order_id": row.order_id,
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
        "exchange_ts": row.exchange_ts,
        "ingestion_ts": row.ingestion_ts,
        "source_system": row.source_system,
        "raw_payload": dict(row.raw_payload),
        "created_at": row.created_at,
    }
