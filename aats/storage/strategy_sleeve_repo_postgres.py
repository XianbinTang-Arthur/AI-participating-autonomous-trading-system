from __future__ import annotations

from sqlalchemy import asc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact, utc_now
from aats.schemas.strategy_runtime import StrategySleeveRecord
from aats.storage.sqlalchemy_models import StrategySleeveModel


class PostgresStrategySleeveRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_sleeve(self, sleeve: StrategySleeveRecord) -> StrategySleeveRecord:
        with self.session_factory() as session:
            row = session.get(StrategySleeveModel, sleeve.sleeve_id)
            updated_at = utc_now()
            persisted = sleeve.model_copy(update={"updated_at": updated_at})
            payload = dump_payload_exact(persisted)
            if row is None:
                row = StrategySleeveModel(
                    sleeve_id=persisted.sleeve_id,
                    family=persisted.family,
                    name=persisted.name,
                    product_scope=persisted.product_scope,
                    margin_scope=persisted.margin_scope,
                    automatic_enabled=persisted.automatic_enabled,
                    inventory_policy=persisted.inventory_policy,
                    status=persisted.status,
                    created_at=persisted.created_at,
                    updated_at=updated_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.family = persisted.family
                row.name = persisted.name
                row.product_scope = persisted.product_scope
                row.margin_scope = persisted.margin_scope
                row.automatic_enabled = persisted.automatic_enabled
                row.inventory_policy = persisted.inventory_policy
                row.status = persisted.status
                row.updated_at = updated_at
                row.payload = payload
            session.commit()
        return persisted

    def get_sleeve(self, sleeve_id: str) -> StrategySleeveRecord | None:
        with self.session_factory() as session:
            row = session.get(StrategySleeveModel, sleeve_id)
        return None if row is None else StrategySleeveRecord.model_validate(row.payload)

    def list_sleeves(self) -> list[StrategySleeveRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(StrategySleeveModel).order_by(
                    asc(StrategySleeveModel.family),
                    asc(StrategySleeveModel.name),
                    asc(StrategySleeveModel.sleeve_id),
                )
            ).all()
        return [StrategySleeveRecord.model_validate(row.payload) for row in rows]
