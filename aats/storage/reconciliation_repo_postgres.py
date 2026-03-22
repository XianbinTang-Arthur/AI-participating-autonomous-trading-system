from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.reconciliation import ReconciliationReport
from aats.services.runtime_scope import RuntimeStateScope
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
        return self._to_report(row) if row is not None else None

    def history(self) -> list[ReconciliationReport]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ReconciliationReportModel).order_by(
                    ReconciliationReportModel.as_of_ts,
                    ReconciliationReportModel.reconciliation_id,
                )
            ).all()
        return [self._to_report(row) for row in rows]

    def recent_history(self, *, limit: int) -> list[ReconciliationReport]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ReconciliationReportModel)
                .order_by(desc(ReconciliationReportModel.as_of_ts), desc(ReconciliationReportModel.reconciliation_id))
                .limit(limit)
            ).all()
        return [self._to_report(row) for row in reversed(rows)]

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[ReconciliationReport]:
        query = (
            select(ReconciliationReportModel)
            .where(ReconciliationReportModel.product_type == scope.product_type)
            .where(ReconciliationReportModel.margin_mode == scope.margin_mode)
            .order_by(ReconciliationReportModel.as_of_ts, ReconciliationReportModel.reconciliation_id)
        )
        if limit is not None:
            query = query.limit(limit)
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_report(row) for row in rows]

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> ReconciliationReport | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ReconciliationReportModel)
                .where(ReconciliationReportModel.product_type == scope.product_type)
                .where(ReconciliationReportModel.margin_mode == scope.margin_mode)
                .order_by(desc(ReconciliationReportModel.as_of_ts), desc(ReconciliationReportModel.reconciliation_id))
                .limit(1)
            )
        return self._to_report(row) if row is not None else None

    @staticmethod
    def _to_report(row: ReconciliationReportModel) -> ReconciliationReport:
        payload = dict(row.payload)
        payload.setdefault("reconciliation_id", row.reconciliation_id)
        payload.setdefault("decision_id", row.decision_id)
        payload.setdefault("as_of_ts", row.as_of_ts)
        payload.setdefault("created_at", row.created_at)
        payload.setdefault("severity", row.severity)
        payload.setdefault("halt_required", row.halt_required)
        payload.setdefault("product_type", row.product_type or payload.get("product_type") or "spot")
        payload.setdefault("margin_mode", row.margin_mode or payload.get("margin_mode") or "cash")
        primary_symbol = row.primary_symbol or payload.get("primary_symbol")
        allowed_symbols = list(payload.get("allowed_symbols") or ([] if primary_symbol is None else [primary_symbol]))
        payload.setdefault("allowed_symbols", allowed_symbols)
        payload.setdefault("portfolio_snapshot_ref", payload.get("portfolio_snapshot_ref"))
        payload.setdefault("exchange_snapshot_ts", payload.get("exchange_snapshot_ts"))
        payload.setdefault("exchange_comparison_enabled", bool(payload.get("exchange_comparison_enabled", False)))
        payload.setdefault("order_diff", payload.get("order_diff") or {})
        payload.setdefault("fill_diff", payload.get("fill_diff") or {})
        payload.setdefault("balance_diff", payload.get("balance_diff") or {})
        payload.setdefault("position_diff", payload.get("position_diff") or {})
        payload.setdefault("exchange_bills_summary", payload.get("exchange_bills_summary") or {})
        payload.setdefault("exchange_bills_explanations", payload.get("exchange_bills_explanations") or [])
        payload.setdefault("mismatch_categories", payload.get("mismatch_categories") or [])
        payload.setdefault("mismatch_reasons", payload.get("mismatch_reasons") or [])
        payload.setdefault("safety_impacts", payload.get("safety_impacts") or [])
        payload.setdefault("recovery_classification", payload.get("recovery_classification"))
        payload.setdefault("auto_repairable", bool(payload.get("auto_repairable", False)))
        payload.setdefault("resume_blocking", bool(payload.get("resume_blocking", False)))
        payload.setdefault("review_required", bool(payload.get("review_required", False)))
        payload.setdefault("recommended_operator_action", payload.get("recommended_operator_action"))
        payload.setdefault("remediation_action", payload.get("remediation_action"))
        return ReconciliationReport.model_validate(payload)
