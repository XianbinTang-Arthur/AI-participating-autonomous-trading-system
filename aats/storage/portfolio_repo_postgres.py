from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.portfolio import PortfolioSnapshot
from aats.storage.sqlalchemy_models import PortfolioSnapshotModel


class PostgresPortfolioRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        with self.session_factory() as session:
            session.add(
                PortfolioSnapshotModel(
                    snapshot_ts=snapshot.snapshot_ts,
                    created_at=snapshot.created_at,
                    total_equity=snapshot.total_equity,
                    realized_pnl=snapshot.realized_pnl,
                    unrealized_pnl=snapshot.unrealized_pnl,
                    payload=snapshot.model_dump(mode="json"),
                )
            )
            session.commit()

    def latest(self) -> PortfolioSnapshot | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(PortfolioSnapshotModel).order_by(desc(PortfolioSnapshotModel.sequence_id)).limit(1)
            )
        return PortfolioSnapshot.model_validate(row.payload) if row is not None else None

    def history(self) -> list[PortfolioSnapshot]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(PortfolioSnapshotModel).order_by(PortfolioSnapshotModel.sequence_id)
            ).all()
        return [PortfolioSnapshot.model_validate(row.payload) for row in rows]
