from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.reconciliation import ReconciliationReport
from aats.storage.scope_metadata import reconciliation_scope_metadata
from aats.storage.sqlalchemy_models import ReconciliationReportModel


class PostgresReconciliationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_report(self, report: ReconciliationReport) -> None:
        scope = reconciliation_scope_metadata(report)
        with self.session_factory() as session:
            session.add(
                ReconciliationReportModel(
                    reconciliation_id=report.reconciliation_id,
                    decision_id=scope["decision_id"],
                    as_of_ts=report.as_of_ts,
                    created_at=report.created_at,
                    severity=report.severity,
                    halt_required=report.halt_required,
                    product_type=scope["product_type"],
                    margin_mode=scope["margin_mode"],
                    primary_symbol=scope["primary_symbol"],
                    payload=report.model_dump(mode="json"),
                )
            )
            session.commit()

    def latest(self) -> ReconciliationReport | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ReconciliationReportModel)
                .order_by(desc(ReconciliationReportModel.as_of_ts), desc(ReconciliationReportModel.reconciliation_id))
                .limit(1)
            )
        return ReconciliationReport.model_validate(row.payload) if row is not None else None

    def history(self) -> list[ReconciliationReport]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ReconciliationReportModel).order_by(
                    ReconciliationReportModel.as_of_ts,
                    ReconciliationReportModel.reconciliation_id,
                )
            ).all()
        return [ReconciliationReport.model_validate(row.payload) for row in rows]

    def recent_history(self, *, limit: int) -> list[ReconciliationReport]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ReconciliationReportModel)
                .order_by(desc(ReconciliationReportModel.as_of_ts), desc(ReconciliationReportModel.reconciliation_id))
                .limit(limit)
            ).all()
        return [ReconciliationReport.model_validate(row.payload) for row in reversed(rows)]
