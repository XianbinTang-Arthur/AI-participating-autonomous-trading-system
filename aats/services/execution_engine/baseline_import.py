from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.exchange import AccountBaselineSnapshot, ExchangeAccountSnapshot
from aats.schemas.reconciliation import BaselineGenerationRecord, ExchangeAckWatermark
from aats.storage.base import EventStore, ReconciliationRepository
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.runtime_scope import RuntimeStateScope


@dataclass(slots=True)
class ImportedAccountBaseline:
    snapshot: AccountBaselineSnapshot
    event_id: str


class AccountBaselineImportService:
    def __init__(
        self,
        *,
        event_store: EventStore,
        reconciliation_repo: ReconciliationRepository | None = None,
    ) -> None:
        self.event_store = event_store
        self.reconciliation_repo = reconciliation_repo

    def import_snapshot(
        self,
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        portfolio_state: PortfolioState,
        product_type: str,
        margin_mode: str,
        allowed_symbols: Sequence[str],
        exchange_bills_summary: dict[str, object] | None = None,
    ) -> ImportedAccountBaseline:
        requires_review = bool(exchange_snapshot.open_orders)
        baseline_generation, watermark = self._prepare_generation_records(
            exchange_snapshot=exchange_snapshot,
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=allowed_symbols,
            baseline_kind="startup_import",
            previous_baseline_ref=None,
            operator_action_ref=None,
            trigger_reason=None,
            safe_for_automatic_continuation=not requires_review,
            requires_operator_review=requires_review,
            exchange_bills_summary=exchange_bills_summary,
        )
        baseline = self._build_baseline(
            exchange_snapshot=exchange_snapshot,
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=allowed_symbols,
            baseline_status=(
                "baseline_import_requires_review"
                if requires_review
                else "baseline_imported"
            ),
            baseline_kind="startup_import",
            safe_for_automatic_continuation=not requires_review,
            requires_operator_review=requires_review,
            previous_baseline_ref=None,
            operator_action_ref=None,
            trigger_reason=None,
            baseline_generation_id=None if baseline_generation is None else baseline_generation.generation_id,
            exchange_ack_watermark_id=None if watermark is None else watermark.watermark_id,
        )
        return self._persist_baseline(
            baseline=baseline,
            exchange_snapshot=exchange_snapshot,
            portfolio_state=portfolio_state,
            source_component="startup_recovery",
            baseline_generation=baseline_generation,
            watermark=watermark,
        )

    def rebaseline_snapshot(
        self,
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        portfolio_state: PortfolioState,
        product_type: str,
        margin_mode: str,
        allowed_symbols: Sequence[str],
        previous_baseline_ref: str | None,
        operator_action_ref: str | None,
        trigger_reason: str,
        exchange_bills_summary: dict[str, object] | None = None,
    ) -> ImportedAccountBaseline:
        requires_review = bool(exchange_snapshot.open_orders)
        baseline_generation, watermark = self._prepare_generation_records(
            exchange_snapshot=exchange_snapshot,
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=allowed_symbols,
            baseline_kind="operator_rebaseline",
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=operator_action_ref,
            trigger_reason=trigger_reason,
            safe_for_automatic_continuation=not requires_review,
            requires_operator_review=requires_review,
            exchange_bills_summary=exchange_bills_summary,
        )
        baseline = self._build_baseline(
            exchange_snapshot=exchange_snapshot,
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=allowed_symbols,
            baseline_status="rebaseline_completed",
            baseline_kind="operator_rebaseline",
            safe_for_automatic_continuation=not requires_review,
            requires_operator_review=requires_review,
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=operator_action_ref,
            trigger_reason=trigger_reason,
            baseline_generation_id=None if baseline_generation is None else baseline_generation.generation_id,
            exchange_ack_watermark_id=None if watermark is None else watermark.watermark_id,
        )
        return self._persist_baseline(
            baseline=baseline,
            exchange_snapshot=exchange_snapshot,
            portfolio_state=portfolio_state,
            source_component="operator_api",
            baseline_generation=baseline_generation,
            watermark=watermark,
        )

    def _persist_baseline(
        self,
        *,
        baseline: AccountBaselineSnapshot,
        exchange_snapshot: ExchangeAccountSnapshot,
        portfolio_state: PortfolioState,
        source_component: str,
        baseline_generation: BaselineGenerationRecord | None,
        watermark: ExchangeAckWatermark | None,
    ) -> ImportedAccountBaseline:
        envelope = build_envelope(
            topic=topics.ACCOUNT_BASELINES,
            key=exchange_snapshot.account_source,
            payload_model=baseline,
            source_component=source_component,
        )
        if self.reconciliation_repo is not None:
            persisted_watermark = (
                None
                if watermark is None
                else watermark.model_copy(update={"baseline_event_ref": envelope.event_id})
            )
            persisted_generation = (
                None
                if baseline_generation is None
                else baseline_generation.model_copy(update={"baseline_event_ref": envelope.event_id})
            )
            if persisted_watermark is not None:
                self.reconciliation_repo.save_exchange_ack_watermark(persisted_watermark)
            if persisted_generation is not None:
                self.reconciliation_repo.save_baseline_generation(persisted_generation)
        self.event_store.append(envelope)
        portfolio_state.load_exchange_snapshot(exchange_snapshot)
        return ImportedAccountBaseline(snapshot=baseline, event_id=envelope.event_id)

    @staticmethod
    def _build_baseline(
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        product_type: str,
        margin_mode: str,
        allowed_symbols: Sequence[str],
        baseline_status: str,
        baseline_kind: str,
        safe_for_automatic_continuation: bool,
        requires_operator_review: bool,
        previous_baseline_ref: str | None,
        operator_action_ref: str | None,
        trigger_reason: str | None,
        baseline_generation_id: str | None,
        exchange_ack_watermark_id: str | None,
    ) -> AccountBaselineSnapshot:
        reason_codes = AccountBaselineImportService._reason_codes(
            snapshot=exchange_snapshot,
            baseline_kind=baseline_kind,
        )
        return AccountBaselineSnapshot(
            baseline_generation_id=baseline_generation_id,
            exchange_ack_watermark_id=exchange_ack_watermark_id,
            account_source=exchange_snapshot.account_source,
            exchange_snapshot_ts=exchange_snapshot.fetched_at,
            imported_at=exchange_snapshot.fetched_at,
            product_type=product_type,  # type: ignore[arg-type]
            margin_mode=margin_mode,  # type: ignore[arg-type]
            allowed_symbols=list(allowed_symbols),
            baseline_status=baseline_status,
            baseline_kind=baseline_kind,
            safe_for_automatic_continuation=safe_for_automatic_continuation,
            requires_operator_review=requires_operator_review,
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=operator_action_ref,
            trigger_reason=trigger_reason,
            reason_codes=reason_codes,
            balance_count=len(exchange_snapshot.balances),
            position_count=len(exchange_snapshot.positions),
            open_order_count=len(exchange_snapshot.open_orders),
            fill_count=len(exchange_snapshot.fills),
            balances=list(exchange_snapshot.balances),
            positions=list(exchange_snapshot.positions),
            open_orders=list(exchange_snapshot.open_orders),
            fills=list(exchange_snapshot.fills),
            account_mode=exchange_snapshot.account_mode,
        )

    def _prepare_generation_records(
        self,
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        product_type: str,
        margin_mode: str,
        allowed_symbols: Sequence[str],
        baseline_kind: str,
        previous_baseline_ref: str | None,
        operator_action_ref: str | None,
        trigger_reason: str | None,
        safe_for_automatic_continuation: bool,
        requires_operator_review: bool,
        exchange_bills_summary: dict[str, object] | None,
    ) -> tuple[BaselineGenerationRecord | None, ExchangeAckWatermark | None]:
        if self.reconciliation_repo is None:
            return None, None
        scope = RuntimeStateScope(
            product_type=product_type,  # type: ignore[arg-type]
            margin_mode=margin_mode,  # type: ignore[arg-type]
            allowed_symbols=tuple(allowed_symbols),
            default_symbol=allowed_symbols[0] if allowed_symbols else "",
        )
        previous_generation = self.reconciliation_repo.latest_baseline_generation_for_scope(scope=scope)
        watermark = ExchangeAckWatermark(
            account_source=exchange_snapshot.account_source,
            product_type=product_type,  # type: ignore[arg-type]
            margin_mode=margin_mode,  # type: ignore[arg-type]
            allowed_symbols=list(allowed_symbols),
            acknowledged_at=exchange_snapshot.fetched_at,
            latest_bill_id=(
                None
                if not isinstance(exchange_bills_summary, dict)
                else exchange_bills_summary.get("latest_bill_id")
            ),
            latest_bill_ts=(
                None
                if not isinstance(exchange_bills_summary, dict)
                else exchange_bills_summary.get("latest_bill_ts")
            ),
            latest_fill_id=_latest_fill.fill_id if (_latest_fill := (
                max(exchange_snapshot.fills, key=lambda item: ((item.fill_ts or exchange_snapshot.fetched_at), item.fill_id))
                if exchange_snapshot.fills else None
            )) else None,
            latest_fill_ts=_latest_fill.fill_ts if _latest_fill else None,
            latest_order_snapshot_ts=exchange_snapshot.fetched_at,
            operator_action_ref=operator_action_ref,
        )
        generation = BaselineGenerationRecord(
            baseline_event_ref="pending_baseline_event",
            baseline_kind=baseline_kind,  # type: ignore[arg-type]
            account_source=exchange_snapshot.account_source,
            product_type=product_type,  # type: ignore[arg-type]
            margin_mode=margin_mode,  # type: ignore[arg-type]
            allowed_symbols=list(allowed_symbols),
            exchange_snapshot_ts=exchange_snapshot.fetched_at,
            imported_at=exchange_snapshot.fetched_at,
            safe_for_automatic_continuation=safe_for_automatic_continuation,
            requires_operator_review=requires_operator_review,
            previous_generation_id=None if previous_generation is None else previous_generation.generation_id,
            previous_baseline_ref=previous_baseline_ref,
            exchange_ack_watermark_id=watermark.watermark_id,
            operator_action_ref=operator_action_ref,
            trigger_reason=trigger_reason,
            reason_codes=self._reason_codes(snapshot=exchange_snapshot, baseline_kind=baseline_kind),
            balance_count=len(exchange_snapshot.balances),
            position_count=len(exchange_snapshot.positions),
            open_order_count=len(exchange_snapshot.open_orders),
            fill_count=len(exchange_snapshot.fills),
        )
        return generation, watermark

    @staticmethod
    def _reason_codes(
        *,
        snapshot: ExchangeAccountSnapshot,
        baseline_kind: str,
    ) -> list[str]:
        reason_codes: list[str] = []
        if baseline_kind == "operator_rebaseline":
            reason_codes.append("operator_rebaseline_confirmed")
        if snapshot.positions:
            reason_codes.append("non_zero_positions_imported")
        if snapshot.fills:
            reason_codes.append("historical_fills_imported")
        if snapshot.open_orders:
            reason_codes.append("open_orders_present")
        if not reason_codes:
            reason_codes.append("clean_account_balances_only")
        return reason_codes
