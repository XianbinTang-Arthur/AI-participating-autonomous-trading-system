from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.portfolio import FillOutcomeRecord
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.sqlalchemy_models import FillOutcomeModel


class PostgresFillOutcomeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_outcome(self, outcome: FillOutcomeRecord) -> FillOutcomeRecord:
        with self.session_factory() as session:
            self.save_outcome_in_session(session, outcome)
            session.commit()
        return outcome

    def save_outcome_in_session(self, session: Session, outcome: FillOutcomeRecord) -> FillOutcomeRecord:
        row = session.get(FillOutcomeModel, outcome.fill_id)
        payload = dump_payload_exact(outcome)
        if row is None:
            row = FillOutcomeModel(
                fill_id=outcome.fill_id,
                decision_id=outcome.decision_id,
                intent_id=outcome.intent_id,
                order_id=outcome.order_id,
                symbol=outcome.symbol,
                venue=outcome.venue,
                side=outcome.side,
                fill_qty=outcome.fill_qty,
                fill_price=outcome.fill_price,
                fill_notional=outcome.fill_notional,
                fee_amount=outcome.fee_amount,
                fee_currency=outcome.fee_currency,
                liquidity_role=outcome.liquidity_role,
                exchange_timestamp=outcome.exchange_timestamp,
                ingestion_timestamp=outcome.ingestion_timestamp,
                order_status_after_fill=outcome.order_status_after_fill,
                strategy_family=outcome.strategy_family,
                strategy_sleeve_id=outcome.strategy_sleeve_id,
                allocation_id=outcome.allocation_id,
                strategy_bundle_id=outcome.strategy_bundle_id,
                strategy_leg_role=outcome.strategy_leg_role,
                target_leverage=outcome.target_leverage,
                exposure_side=outcome.exposure_side,
                execution_action=outcome.execution_action,
                position_intent=outcome.position_intent,
                starting_position_qty=outcome.starting_position_qty,
                starting_avg_entry_price=outcome.starting_avg_entry_price,
                ending_position_qty=outcome.ending_position_qty,
                ending_avg_entry_price=outcome.ending_avg_entry_price,
                realized_pnl_delta=outcome.realized_pnl_delta,
                fee_delta=outcome.fee_delta,
                product_type=outcome.product_type,
                margin_mode=outcome.margin_mode,
                created_at=outcome.created_at,
                payload=payload,
            )
            session.add(row)
        else:
            row.decision_id = outcome.decision_id
            row.intent_id = outcome.intent_id
            row.order_id = outcome.order_id
            row.symbol = outcome.symbol
            row.venue = outcome.venue
            row.side = outcome.side
            row.fill_qty = outcome.fill_qty
            row.fill_price = outcome.fill_price
            row.fill_notional = outcome.fill_notional
            row.fee_amount = outcome.fee_amount
            row.fee_currency = outcome.fee_currency
            row.liquidity_role = outcome.liquidity_role
            row.exchange_timestamp = outcome.exchange_timestamp
            row.ingestion_timestamp = outcome.ingestion_timestamp
            row.order_status_after_fill = outcome.order_status_after_fill
            row.strategy_family = outcome.strategy_family
            row.strategy_sleeve_id = outcome.strategy_sleeve_id
            row.allocation_id = outcome.allocation_id
            row.strategy_bundle_id = outcome.strategy_bundle_id
            row.strategy_leg_role = outcome.strategy_leg_role
            row.target_leverage = outcome.target_leverage
            row.exposure_side = outcome.exposure_side
            row.execution_action = outcome.execution_action
            row.position_intent = outcome.position_intent
            row.starting_position_qty = outcome.starting_position_qty
            row.starting_avg_entry_price = outcome.starting_avg_entry_price
            row.ending_position_qty = outcome.ending_position_qty
            row.ending_avg_entry_price = outcome.ending_avg_entry_price
            row.realized_pnl_delta = outcome.realized_pnl_delta
            row.fee_delta = outcome.fee_delta
            row.product_type = outcome.product_type
            row.margin_mode = outcome.margin_mode
            row.created_at = outcome.created_at
            row.payload = payload
        return outcome

    def get_outcome(self, fill_id: str) -> FillOutcomeRecord | None:
        with self.session_factory() as session:
            row = session.get(FillOutcomeModel, fill_id)
        return None if row is None else self._to_outcome(row)

    def outcomes(self) -> list[FillOutcomeRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillOutcomeModel).order_by(FillOutcomeModel.created_at, FillOutcomeModel.fill_id)
            ).all()
        return [self._to_outcome(row) for row in rows]

    def outcomes_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillOutcomeRecord]:
        query = self._scope_query(select(FillOutcomeModel), scope)
        if since is not None:
            query = query.where(FillOutcomeModel.created_at >= since)
        query = query.order_by(desc(FillOutcomeModel.created_at), desc(FillOutcomeModel.fill_id))
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_outcome(row) for row in reversed(rows)]

    @staticmethod
    def _scope_query(query, scope: RuntimeStateScope):
        allowed_symbols = tuple(scope.allowed_symbols) if scope.allowed_symbols else (scope.default_symbol,)
        symbol_clause = FillOutcomeModel.symbol.in_(allowed_symbols)
        regular_clause = and_(
            symbol_clause,
            FillOutcomeModel.product_type == scope.product_type,
            FillOutcomeModel.margin_mode == scope.margin_mode,
            or_(FillOutcomeModel.strategy_family.is_(None), FillOutcomeModel.strategy_family != "smart_arbitrage"),
        )
        if scope.product_type != "derivatives":
            return query.where(regular_clause)
        smart_clause = and_(
            symbol_clause,
            FillOutcomeModel.strategy_family == "smart_arbitrage",
            or_(
                and_(FillOutcomeModel.product_type == "spot", FillOutcomeModel.margin_mode == "cash"),
                and_(FillOutcomeModel.product_type == scope.product_type, FillOutcomeModel.margin_mode == scope.margin_mode),
            ),
        )
        return query.where(or_(regular_clause, smart_clause))

    @staticmethod
    def _to_outcome(row: FillOutcomeModel) -> FillOutcomeRecord:
        payload = dict(row.payload)
        payload.setdefault("strategy_family", row.strategy_family)
        payload.setdefault("strategy_sleeve_id", row.strategy_sleeve_id)
        payload.setdefault("allocation_id", row.allocation_id)
        payload.setdefault("strategy_bundle_id", row.strategy_bundle_id)
        payload.setdefault("strategy_leg_role", row.strategy_leg_role)
        return FillOutcomeRecord.model_validate(payload)
