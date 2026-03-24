from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.portfolio import FillOutcomeRecord
from aats.services.runtime_scope import RuntimeStateScope, filter_fill_outcomes
from aats.storage.sqlalchemy_models import FillOutcomeModel


class PostgresFillOutcomeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_outcome(self, outcome: FillOutcomeRecord) -> FillOutcomeRecord:
        with self.session_factory() as session:
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
            session.commit()
        return outcome

    def get_outcome(self, fill_id: str) -> FillOutcomeRecord | None:
        with self.session_factory() as session:
            row = session.get(FillOutcomeModel, fill_id)
        return None if row is None else FillOutcomeRecord.model_validate(row.payload)

    def outcomes(self) -> list[FillOutcomeRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillOutcomeModel).order_by(FillOutcomeModel.created_at, FillOutcomeModel.fill_id)
            ).all()
        return [FillOutcomeRecord.model_validate(row.payload) for row in rows]

    def outcomes_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillOutcomeRecord]:
        query = select(FillOutcomeModel)
        if since is not None:
            query = query.where(FillOutcomeModel.created_at >= since)
        query = query.order_by(desc(FillOutcomeModel.created_at), desc(FillOutcomeModel.fill_id))
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        outcomes = filter_fill_outcomes(
            [FillOutcomeRecord.model_validate(row.payload) for row in reversed(rows)],
            scope,
        )
        if limit is not None:
            outcomes = outcomes[-limit:]
        return outcomes
