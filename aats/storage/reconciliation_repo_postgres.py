from __future__ import annotations

import json

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.reconciliation import (
    BaselineGenerationRecord,
    ExchangeAckWatermark,
    ReconciliationFinding,
    ReconciliationReport,
    ReconciliationStateSnapshot,
)
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.scope_metadata import reconciliation_scope_metadata
from aats.storage.sqlalchemy_models import (
    BaselineGenerationModel,
    ExchangeAckWatermarkModel,
    ReconciliationFindingModel,
    ReconciliationReportModel,
    ReconciliationStateSnapshotModel,
)


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
                    payload=self._json_ready(report.model_dump(mode="json")),
                )
            )
            if report.findings:
                self._replace_findings_in_session(
                    session=session,
                    reconciliation_id=report.reconciliation_id,
                    findings=report.findings,
                )
            session.commit()

    def save_findings(self, findings: list[ReconciliationFinding]) -> None:
        if not findings:
            return
        with self.session_factory() as session:
            self._replace_findings_in_session(
                session=session,
                reconciliation_id=findings[0].reconciliation_id,
                findings=findings,
            )
            session.commit()

    def findings_for_reconciliation(self, *, reconciliation_id: str) -> list[ReconciliationFinding]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ReconciliationFindingModel)
                .where(ReconciliationFindingModel.reconciliation_id == reconciliation_id)
                .order_by(ReconciliationFindingModel.created_at, ReconciliationFindingModel.finding_id)
            ).all()
        return [self._to_finding(row) for row in rows]

    def save_state_snapshot(self, snapshot: ReconciliationStateSnapshot) -> None:
        # Stage 5 (5a-2)：reconciliation_state_snapshots 是 append-only，但 PK 是
        # snapshot_id (string)，多进程崩溃恢复期间可能因为 retry 把同一份 snapshot
        # 重复 enqueue。session.add + commit 会撞 PK 唯一约束抛 IntegrityError，
        # 导致整个 reconciliation pipeline 失败重启。改成 ON CONFLICT DO NOTHING
        # 后，重复插入静默成功（行已存在视为同一份历史快照），符合幂等语义。
        with self.session_factory() as session:
            stmt = (
                pg_insert(ReconciliationStateSnapshotModel)
                .values(
                    snapshot_id=snapshot.snapshot_id,
                    reconciliation_id=snapshot.reconciliation_id,
                    product_type=snapshot.product_type,
                    margin_mode=snapshot.margin_mode,
                    primary_symbol=snapshot.primary_symbol,
                    recovery_state=snapshot.recovery_state,
                    resume_eligible=snapshot.resume_eligible,
                    safe_to_trade=snapshot.safe_to_trade,
                    review_required=snapshot.review_required,
                    only_reduce_required=snapshot.only_reduce_required,
                    halt_required=snapshot.halt_required,
                    bundle_recovery_required=snapshot.bundle_recovery_required,
                    resume_blocked_reasons_json=self._json_ready(list(snapshot.resume_blocked_reasons_json)),
                    derived_from_generation_id=snapshot.derived_from_generation_id,
                    exchange_ack_watermark_id=snapshot.exchange_ack_watermark_id,
                    details_json=self._json_ready(snapshot.details_json),
                    created_at=snapshot.created_at,
                )
                .on_conflict_do_nothing(index_elements=["snapshot_id"])
            )
            session.execute(stmt)
            session.commit()

    def latest_state_snapshot_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> ReconciliationStateSnapshot | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ReconciliationStateSnapshotModel)
                .where(ReconciliationStateSnapshotModel.product_type == scope.product_type)
                .where(ReconciliationStateSnapshotModel.margin_mode == scope.margin_mode)
                .order_by(
                    desc(ReconciliationStateSnapshotModel.created_at),
                    desc(ReconciliationStateSnapshotModel.snapshot_id),
                )
                .limit(1)
            )
        return self._to_state_snapshot(row) if row is not None else None

    def save_baseline_generation(self, generation: BaselineGenerationRecord) -> None:
        with self.session_factory() as session:
            session.add(
                BaselineGenerationModel(
                    generation_id=generation.generation_id,
                    baseline_event_ref=generation.baseline_event_ref,
                    baseline_id=generation.baseline_id,
                    baseline_kind=generation.baseline_kind,
                    account_source=generation.account_source,
                    product_type=generation.product_type,
                    margin_mode=generation.margin_mode,
                    allowed_symbols=self._json_ready(list(generation.allowed_symbols)),
                    exchange_snapshot_ts=generation.exchange_snapshot_ts,
                    imported_at=generation.imported_at,
                    safe_for_automatic_continuation=generation.safe_for_automatic_continuation,
                    requires_operator_review=generation.requires_operator_review,
                    previous_generation_id=generation.previous_generation_id,
                    previous_baseline_ref=generation.previous_baseline_ref,
                    exchange_ack_watermark_id=generation.exchange_ack_watermark_id,
                    operator_action_ref=generation.operator_action_ref,
                    trigger_reason=generation.trigger_reason,
                    reason_codes=self._json_ready(list(generation.reason_codes)),
                    balance_count=generation.balance_count,
                    position_count=generation.position_count,
                    open_order_count=generation.open_order_count,
                    fill_count=generation.fill_count,
                    created_at=generation.created_at,
                )
            )
            session.commit()

    def latest_baseline_generation_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> BaselineGenerationRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(BaselineGenerationModel)
                .where(BaselineGenerationModel.product_type == scope.product_type)
                .where(BaselineGenerationModel.margin_mode == scope.margin_mode)
                .order_by(desc(BaselineGenerationModel.imported_at), desc(BaselineGenerationModel.generation_id))
                .limit(1)
            )
        return self._to_baseline_generation(row) if row is not None else None

    def save_exchange_ack_watermark(self, watermark: ExchangeAckWatermark) -> None:
        with self.session_factory() as session:
            session.add(
                ExchangeAckWatermarkModel(
                    watermark_id=watermark.watermark_id,
                    account_source=watermark.account_source,
                    product_type=watermark.product_type,
                    margin_mode=watermark.margin_mode,
                    allowed_symbols=self._json_ready(list(watermark.allowed_symbols)),
                    acknowledged_at=watermark.acknowledged_at,
                    latest_bill_id=watermark.latest_bill_id,
                    latest_bill_ts=watermark.latest_bill_ts,
                    latest_fill_id=watermark.latest_fill_id,
                    latest_fill_ts=watermark.latest_fill_ts,
                    latest_order_snapshot_ts=watermark.latest_order_snapshot_ts,
                    latest_reconciliation_id=watermark.latest_reconciliation_id,
                    baseline_event_ref=watermark.baseline_event_ref,
                    operator_action_ref=watermark.operator_action_ref,
                    details_json=self._json_ready(watermark.details_json),
                    created_at=watermark.created_at,
                )
            )
            session.commit()

    def latest_exchange_ack_watermark_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> ExchangeAckWatermark | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ExchangeAckWatermarkModel)
                .where(ExchangeAckWatermarkModel.product_type == scope.product_type)
                .where(ExchangeAckWatermarkModel.margin_mode == scope.margin_mode)
                .order_by(desc(ExchangeAckWatermarkModel.acknowledged_at), desc(ExchangeAckWatermarkModel.watermark_id))
                .limit(1)
            )
        return self._to_exchange_ack_watermark(row) if row is not None else None

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
        payload.setdefault("findings", payload.get("findings") or [])
        payload.setdefault("finding_summary", payload.get("finding_summary") or {})
        payload.setdefault("baseline_generation_id", payload.get("baseline_generation_id"))
        payload.setdefault("exchange_ack_watermark_id", payload.get("exchange_ack_watermark_id"))
        payload.setdefault("mismatch_categories", payload.get("mismatch_categories") or [])
        payload.setdefault("mismatch_reasons", payload.get("mismatch_reasons") or [])
        payload.setdefault("safety_impacts", payload.get("safety_impacts") or [])
        payload.setdefault("recovery_classification", payload.get("recovery_classification"))
        payload.setdefault("auto_repairable", bool(payload.get("auto_repairable", False)))
        payload.setdefault("resume_blocking", bool(payload.get("resume_blocking", False)))
        payload.setdefault("review_required", bool(payload.get("review_required", False)))
        payload.setdefault("only_reduce_required", bool(payload.get("only_reduce_required", False)))
        payload.setdefault("only_reduce_reasons", payload.get("only_reduce_reasons") or [])
        payload.setdefault("unknown_state_details", payload.get("unknown_state_details") or [])
        payload.setdefault("recommended_operator_action", payload.get("recommended_operator_action"))
        payload.setdefault("remediation_action", payload.get("remediation_action"))
        payload.setdefault("structural_review_required", bool(payload.get("structural_review_required", False)))
        payload.setdefault("financial_review_required", bool(payload.get("financial_review_required", False)))
        payload.setdefault("observational_only", bool(payload.get("observational_only", False)))
        return ReconciliationReport.model_validate(payload)

    @staticmethod
    def _replace_findings_in_session(
        *,
        session: Session,
        reconciliation_id: str,
        findings: list[ReconciliationFinding],
    ) -> None:
        existing = session.scalars(
            select(ReconciliationFindingModel).where(ReconciliationFindingModel.reconciliation_id == reconciliation_id)
        ).all()
        for row in existing:
            session.delete(row)
        session.add_all(
            [
                ReconciliationFindingModel(
                    finding_id=item.finding_id,
                    reconciliation_id=item.reconciliation_id,
                    product_type=item.product_type,
                    margin_mode=item.margin_mode,
                    primary_symbol=item.primary_symbol,
                    strategy_sleeve_id=item.strategy_sleeve_id,
                    allocation_id=item.allocation_id,
                    strategy_bundle_id=item.strategy_bundle_id,
                    scope_kind=item.scope_kind,
                    scope_ref=item.scope_ref,
                    layer=item.layer,
                    finding_type=item.finding_type,
                    severity_class=item.severity_class,
                    structural=item.structural,
                    financial=item.financial,
                    observational=item.observational,
                    review_required=item.review_required,
                    only_reduce_required=item.only_reduce_required,
                    halt_required=item.halt_required,
                    blocks_resume=item.blocks_resume,
                    reason_code=item.reason_code,
                    details_json=PostgresReconciliationRepository._json_ready(item.details_json),
                    created_at=item.created_at,
                )
                for item in findings
            ]
        )

    @staticmethod
    def _json_ready(value):
        return json.loads(json.dumps(value, default=str))

    @staticmethod
    def _to_finding(row: ReconciliationFindingModel) -> ReconciliationFinding:
        return ReconciliationFinding.model_validate(
            {
                "finding_id": row.finding_id,
                "reconciliation_id": row.reconciliation_id,
                "product_type": row.product_type,
                "margin_mode": row.margin_mode,
                "primary_symbol": row.primary_symbol,
                "strategy_sleeve_id": row.strategy_sleeve_id,
                "allocation_id": row.allocation_id,
                "strategy_bundle_id": row.strategy_bundle_id,
                "scope_kind": row.scope_kind,
                "scope_ref": row.scope_ref,
                "layer": row.layer,
                "finding_type": row.finding_type,
                "severity_class": row.severity_class,
                "structural": row.structural,
                "financial": row.financial,
                "observational": row.observational,
                "review_required": row.review_required,
                "only_reduce_required": row.only_reduce_required,
                "halt_required": row.halt_required,
                "blocks_resume": row.blocks_resume,
                "reason_code": row.reason_code,
                "details_json": row.details_json,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _to_state_snapshot(row: ReconciliationStateSnapshotModel) -> ReconciliationStateSnapshot:
        return ReconciliationStateSnapshot.model_validate(
            {
                "snapshot_id": row.snapshot_id,
                "reconciliation_id": row.reconciliation_id,
                "product_type": row.product_type,
                "margin_mode": row.margin_mode,
                "primary_symbol": row.primary_symbol,
                "recovery_state": row.recovery_state,
                "resume_eligible": row.resume_eligible,
                "safe_to_trade": row.safe_to_trade,
                "review_required": row.review_required,
                "only_reduce_required": row.only_reduce_required,
                "halt_required": row.halt_required,
                "bundle_recovery_required": row.bundle_recovery_required,
                "resume_blocked_reasons_json": list(row.resume_blocked_reasons_json or []),
                "derived_from_generation_id": row.derived_from_generation_id,
                "exchange_ack_watermark_id": row.exchange_ack_watermark_id,
                "details_json": row.details_json,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _to_baseline_generation(row: BaselineGenerationModel) -> BaselineGenerationRecord:
        return BaselineGenerationRecord.model_validate(
            {
                "generation_id": row.generation_id,
                "baseline_event_ref": row.baseline_event_ref,
                "baseline_id": row.baseline_id,
                "baseline_kind": row.baseline_kind,
                "account_source": row.account_source,
                "product_type": row.product_type,
                "margin_mode": row.margin_mode,
                "allowed_symbols": list(row.allowed_symbols or []),
                "exchange_snapshot_ts": row.exchange_snapshot_ts,
                "imported_at": row.imported_at,
                "safe_for_automatic_continuation": row.safe_for_automatic_continuation,
                "requires_operator_review": row.requires_operator_review,
                "previous_generation_id": row.previous_generation_id,
                "previous_baseline_ref": row.previous_baseline_ref,
                "exchange_ack_watermark_id": row.exchange_ack_watermark_id,
                "operator_action_ref": row.operator_action_ref,
                "trigger_reason": row.trigger_reason,
                "reason_codes": list(row.reason_codes or []),
                "balance_count": row.balance_count,
                "position_count": row.position_count,
                "open_order_count": row.open_order_count,
                "fill_count": row.fill_count,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _to_exchange_ack_watermark(row: ExchangeAckWatermarkModel) -> ExchangeAckWatermark:
        return ExchangeAckWatermark.model_validate(
            {
                "watermark_id": row.watermark_id,
                "account_source": row.account_source,
                "product_type": row.product_type,
                "margin_mode": row.margin_mode,
                "allowed_symbols": list(row.allowed_symbols or []),
                "acknowledged_at": row.acknowledged_at,
                "latest_bill_id": row.latest_bill_id,
                "latest_bill_ts": row.latest_bill_ts,
                "latest_fill_id": row.latest_fill_id,
                "latest_fill_ts": row.latest_fill_ts,
                "latest_order_snapshot_ts": row.latest_order_snapshot_ts,
                "latest_reconciliation_id": row.latest_reconciliation_id,
                "baseline_event_ref": row.baseline_event_ref,
                "operator_action_ref": row.operator_action_ref,
                "details_json": row.details_json,
                "created_at": row.created_at,
            }
        )
