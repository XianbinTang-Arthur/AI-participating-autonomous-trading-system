from __future__ import annotations

from dataclasses import dataclass

from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.exchange import AccountBaselineSnapshot, ExchangeAccountSnapshot
from aats.storage.base import EventStore
from aats.services.portfolio_service.positions import PortfolioState


@dataclass(slots=True)
class ImportedAccountBaseline:
    snapshot: AccountBaselineSnapshot
    event_id: str


class AccountBaselineImportService:
    def __init__(self, *, event_store: EventStore) -> None:
        self.event_store = event_store

    def import_snapshot(
        self,
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        portfolio_state: PortfolioState,
    ) -> ImportedAccountBaseline:
        requires_review = bool(exchange_snapshot.open_orders)
        baseline = self._build_baseline(
            exchange_snapshot=exchange_snapshot,
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
        )
        return self._persist_baseline(
            baseline=baseline,
            exchange_snapshot=exchange_snapshot,
            portfolio_state=portfolio_state,
            source_component="startup_recovery",
        )

    def rebaseline_snapshot(
        self,
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        portfolio_state: PortfolioState,
        previous_baseline_ref: str | None,
        operator_action_ref: str | None,
        trigger_reason: str,
    ) -> ImportedAccountBaseline:
        requires_review = bool(exchange_snapshot.open_orders)
        baseline = self._build_baseline(
            exchange_snapshot=exchange_snapshot,
            baseline_status="rebaseline_completed",
            baseline_kind="operator_rebaseline",
            safe_for_automatic_continuation=not requires_review,
            requires_operator_review=requires_review,
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=operator_action_ref,
            trigger_reason=trigger_reason,
        )
        return self._persist_baseline(
            baseline=baseline,
            exchange_snapshot=exchange_snapshot,
            portfolio_state=portfolio_state,
            source_component="operator_api",
        )

    def _persist_baseline(
        self,
        *,
        baseline: AccountBaselineSnapshot,
        exchange_snapshot: ExchangeAccountSnapshot,
        portfolio_state: PortfolioState,
        source_component: str,
    ) -> ImportedAccountBaseline:
        envelope = build_envelope(
            topic=topics.ACCOUNT_BASELINES,
            key=exchange_snapshot.account_source,
            payload_model=baseline,
            source_component=source_component,
        )
        self.event_store.append(envelope)
        portfolio_state.load_exchange_snapshot(exchange_snapshot)
        return ImportedAccountBaseline(snapshot=baseline, event_id=envelope.event_id)

    @staticmethod
    def _build_baseline(
        *,
        exchange_snapshot: ExchangeAccountSnapshot,
        baseline_status: str,
        baseline_kind: str,
        safe_for_automatic_continuation: bool,
        requires_operator_review: bool,
        previous_baseline_ref: str | None,
        operator_action_ref: str | None,
        trigger_reason: str | None,
    ) -> AccountBaselineSnapshot:
        reason_codes = AccountBaselineImportService._reason_codes(
            snapshot=exchange_snapshot,
            baseline_kind=baseline_kind,
        )
        return AccountBaselineSnapshot(
            account_source=exchange_snapshot.account_source,
            exchange_snapshot_ts=exchange_snapshot.fetched_at,
            imported_at=exchange_snapshot.fetched_at,
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
