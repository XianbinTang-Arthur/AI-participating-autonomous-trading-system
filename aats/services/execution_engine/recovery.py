from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.exchange import AccountBaselineSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.execution import FillEvent
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import RecoveryStatus
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.runtime_layers import RecoveryPolicy
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.runtime_scope import (
    fills_for_scope,
    latest_reconciliation_for_scope,
    latest_snapshot_for_scope,
    order_states_for_scope,
    runtime_state_scope,
)
from aats.storage.base import ExecutionRepository, PortfolioRepository, ReconciliationRepository


@dataclass(slots=True)
class RecoveryArtifacts:
    status: RecoveryStatus
    rebuilt_snapshot_saved: bool = False


class ExecutionRecoveryService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        execution_repo: ExecutionRepository,
        portfolio_repo: PortfolioRepository,
        reconciliation_repo: ReconciliationRepository,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], float],
        kill_switch: KillSwitch,
        bootstrap_portfolio_from_exchange: bool,
        reconciliation_stale_after_seconds: float,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.execution_repo = execution_repo
        self.portfolio_repo = portfolio_repo
        self.reconciliation_repo = reconciliation_repo
        self.reconstruction_service = reconstruction_service
        self.price_provider = price_provider
        self.kill_switch = kill_switch
        self.bootstrap_portfolio_from_exchange = bootstrap_portfolio_from_exchange
        self.reconciliation_stale_after_seconds = reconciliation_stale_after_seconds
        self.recovery_policy = recovery_policy or RecoveryPolicy(
            name="default_recovery",
            startup_baseline_import_supported=bootstrap_portfolio_from_exchange,
            operator_rebaseline_supported=bootstrap_portfolio_from_exchange,
            account_snapshot_required=bootstrap_portfolio_from_exchange,
            review_required_blocks_resume=bootstrap_portfolio_from_exchange,
            reconciliation_required_for_execution_state=True,
            exchange_portfolio_comparison_enabled=bootstrap_portfolio_from_exchange,
        )
        self.runtime_scope = runtime_state_scope(settings)

    def recover(
        self,
        *,
        portfolio_state: PortfolioState,
        account_baseline: AccountBaselineSnapshot | None = None,
        account_baseline_event_id: str | None = None,
    ) -> RecoveryArtifacts:
        fills = fills_for_scope(self.execution_repo, self.runtime_scope)
        latest_snapshot = latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope)
        latest_reconciliation = latest_reconciliation_for_scope(self.reconciliation_repo, self.runtime_scope)
        open_orders = order_states_for_scope(self.execution_repo, self.runtime_scope, open_only=True)
        notes: list[str] = []
        rebuilt_snapshot_saved = False
        divergence_count = 0
        recovery_action: str | None = None
        safe_startup = True

        if account_baseline is not None:
            notes.append(account_baseline.baseline_status)
            notes.extend(account_baseline.reason_codes)
            if account_baseline.requires_operator_review:
                self._halt_for_recovery(
                    reason="baseline_import_requires_review",
                    action="halted_imported_baseline_requires_review",
                    notes=notes,
                )
                recovery_action = "halted_imported_baseline_requires_review"
                safe_startup = False

        if latest_snapshot is not None:
            portfolio_state.load_portfolio_snapshot(
                latest_snapshot,
                applied_fill_ids={fill.fill_id for fill in fills},
                total_fees_paid=PortfolioState.total_fee_cost_in_quote(fills),
            )
            if self.bootstrap_portfolio_from_exchange:
                notes.append("reconstruction_validation_skipped_bootstrap_exchange")
            else:
                rebuilt = self.reconstruction_service.rebuild_snapshot(
                    fills=fills,
                    price_provider=self._recovery_price_provider(latest_snapshot),
                ).model_copy(
                    update={
                        "product_type": self.runtime_scope.product_type,
                        "margin_mode": self.runtime_scope.margin_mode,
                    }
                )
                divergence_count = self._divergence_count(latest_snapshot, rebuilt)
                if divergence_count:
                    self._halt_for_recovery(
                        reason="recovery_portfolio_divergence",
                        action="halted_for_portfolio_divergence",
                        notes=notes,
                    )
                    recovery_action = "halted_for_portfolio_divergence"
                    safe_startup = False
                    notes.append("stored_snapshot_differs_from_fill_reconstruction")
        elif fills:
            if self.bootstrap_portfolio_from_exchange:
                self._halt_for_recovery(
                    reason="recovery_snapshot_missing",
                    action="halted_missing_bootstrap_snapshot",
                    notes=notes,
                )
                recovery_action = "halted_missing_bootstrap_snapshot"
                safe_startup = False
                notes.append("bootstrap_exchange_snapshot_missing")
            else:
                rebuilt_snapshot = self.reconstruction_service.rebuild_snapshot(
                    fills=fills,
                    price_provider=self.price_provider,
                ).model_copy(
                    update={
                        "product_type": self.runtime_scope.product_type,
                        "margin_mode": self.runtime_scope.margin_mode,
                    }
                )
                portfolio_state.load_portfolio_snapshot(
                    rebuilt_snapshot,
                    applied_fill_ids={fill.fill_id for fill in fills},
                    total_fees_paid=PortfolioState.total_fee_cost_in_quote(fills),
                )
                self.portfolio_repo.save_snapshot(rebuilt_snapshot)
                rebuilt_snapshot_saved = True
                notes.append("portfolio_rebuilt_from_fills")
        else:
            notes.append("cold_start_no_execution_state")

        safe_startup, recovery_action = self._apply_reconciliation_safety(
            latest_reconciliation=latest_reconciliation,
            latest_snapshot=latest_snapshot or latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope),
            fills=fills,
            open_orders=open_orders,
            safe_startup=safe_startup,
            recovery_action=recovery_action,
            notes=notes,
        )

        if open_orders:
            self._halt_for_recovery(
                reason="recovery_open_orders_present",
                action="halted_open_orders_require_review",
                notes=notes,
            )
            recovery_action = recovery_action or "halted_open_orders_require_review"
            safe_startup = False
            notes.append("open_orders_restored_require_operator_review")

        status = RecoveryStatus(
            status=(
                account_baseline.baseline_status
                if account_baseline is not None
                else
                "recovered_halted"
                if self.kill_switch.halted
                else "recovered"
                if latest_snapshot is not None or fills
                else "cold_start"
            ),
            recovery_state=self._initial_recovery_state(
                account_baseline=account_baseline,
                halted=self.kill_switch.halted,
                latest_reconciliation=latest_reconciliation,
            ),
            recovered_order_count=len(order_states_for_scope(self.execution_repo, self.runtime_scope)),
            recovered_fill_count=len(fills),
            recovered_snapshot_available=latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope) is not None,
            rebuilt_snapshot_saved=rebuilt_snapshot_saved,
            recovered_reconciliation_available=latest_reconciliation is not None,
            latest_reconciliation_id=latest_reconciliation.reconciliation_id if latest_reconciliation else None,
            latest_reconciliation_severity=latest_reconciliation.severity if latest_reconciliation else None,
            open_order_count=len(open_orders),
            divergence_count=divergence_count,
            safe_startup=safe_startup and not self.kill_switch.halted,
            safe_to_trade=safe_startup and not self.kill_switch.halted,
            resume_eligible=safe_startup and not self.kill_switch.halted,
            review_required=bool(
                (account_baseline is not None and account_baseline.requires_operator_review)
                or (latest_reconciliation is not None and latest_reconciliation.review_required)
            ),
            rebaseline_available=bool(
                (account_baseline is not None and account_baseline.requires_operator_review)
                or (latest_reconciliation is not None and latest_reconciliation.review_required)
                or self.kill_switch.halted
            ),
            halted=self.kill_switch.halted,
            recovery_action=recovery_action,
            baseline_imported=account_baseline is not None,
            baseline_status=account_baseline.baseline_status if account_baseline is not None else None,
            baseline_imported_at=account_baseline.imported_at if account_baseline is not None else None,
            baseline_event_ref=account_baseline_event_id,
            baseline_source=account_baseline.account_source if account_baseline is not None else None,
            baseline_safe_for_automatic_continuation=(
                account_baseline.safe_for_automatic_continuation if account_baseline is not None else False
            ),
            baseline_requires_operator_review=(
                account_baseline.requires_operator_review if account_baseline is not None else False
            ),
            baseline_balance_count=account_baseline.balance_count if account_baseline is not None else 0,
            baseline_position_count=account_baseline.position_count if account_baseline is not None else 0,
            baseline_open_order_count=account_baseline.open_order_count if account_baseline is not None else 0,
            baseline_fill_count=account_baseline.fill_count if account_baseline is not None else 0,
            last_rebaseline_at=(
                account_baseline.imported_at
                if account_baseline is not None and account_baseline.baseline_kind == "operator_rebaseline"
                else None
            ),
            last_rebaseline_event_ref=(
                account_baseline_event_id
                if account_baseline is not None and account_baseline.baseline_kind == "operator_rebaseline"
                else None
            ),
            notes=self._dedupe_notes(notes),
        )
        return RecoveryArtifacts(status=status, rebuilt_snapshot_saved=rebuilt_snapshot_saved)

    @staticmethod
    def _initial_recovery_state(
        *,
        account_baseline: AccountBaselineSnapshot | None,
        halted: bool,
        latest_reconciliation: ReconciliationReport | None,
    ) -> str:
        if account_baseline is not None and account_baseline.baseline_kind == "operator_rebaseline":
            return "rebaseline_completed" if not halted else "resume_blocked"
        if account_baseline is not None and account_baseline.requires_operator_review:
            return "review_required"
        if latest_reconciliation is not None:
            if latest_reconciliation.halt_required:
                return "resume_blocked"
            if latest_reconciliation.review_required:
                return "review_required"
        if halted:
            return "resume_blocked"
        return "normal_operation"

    def _apply_reconciliation_safety(
        self,
        *,
        latest_reconciliation: ReconciliationReport | None,
        latest_snapshot: PortfolioSnapshot | None,
        fills: list[FillEvent],
        open_orders: list,
        safe_startup: bool,
        recovery_action: str | None,
        notes: list[str],
    ) -> tuple[bool, str | None]:
        has_execution_state = bool(fills or open_orders)
        if latest_reconciliation is None:
            if (
                self.recovery_policy.reconciliation_required_for_execution_state
                and has_execution_state
                and latest_snapshot is not None
            ):
                self._halt_for_recovery(
                    reason="recovery_reconciliation_missing",
                    action="halted_missing_reconciliation_context",
                    notes=notes,
                )
                notes.append("reconciliation_context_missing_for_execution_state")
                return False, recovery_action or "halted_missing_reconciliation_context"
            return safe_startup, recovery_action

        notes.append("reconciliation_context_restored")
        age_seconds = (utc_now() - latest_reconciliation.as_of_ts).total_seconds()
        if (
            self.recovery_policy.reconciliation_required_for_execution_state
            and age_seconds > self.reconciliation_stale_after_seconds
        ):
            self._halt_for_recovery(
                reason="recovery_reconciliation_stale",
                action="halted_stale_reconciliation_context",
                notes=notes,
            )
            notes.append("latest_reconciliation_is_stale")
            return False, recovery_action or "halted_stale_reconciliation_context"
        if latest_reconciliation.halt_required:
            self._halt_for_recovery(
                reason="recovery_reconciliation_halt_required",
                action="halted_reconciliation_requires_review",
                notes=notes,
            )
            notes.append("latest_reconciliation_requires_operator_review")
            return False, recovery_action or "halted_reconciliation_requires_review"
        return safe_startup, recovery_action

    def _halt_for_recovery(self, *, reason: str, action: str, notes: list[str]) -> None:
        self.kill_switch.halt(reason=reason)
        notes.append(action)

    @staticmethod
    def _dedupe_notes(notes: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for note in notes:
            if note in seen:
                continue
            seen.add(note)
            ordered.append(note)
        return ordered

    def _recovery_price_provider(self, snapshot: PortfolioSnapshot) -> Callable[[str], float]:
        snapshot_marks: dict[str, float] = {}
        for position in snapshot.positions:
            if abs(position.position_qty) > 1e-12:
                snapshot_marks[position.symbol] = position.position_notional / position.position_qty
            elif position.avg_entry_price > 0.0:
                snapshot_marks[position.symbol] = position.avg_entry_price

        def provider(symbol: str) -> float:
            live_price = self.price_provider(symbol)
            if live_price > 0.0:
                return live_price
            return snapshot_marks.get(symbol, 0.0)

        return provider

    @staticmethod
    def _divergence_count(left, right) -> int:
        count = 0
        if ExecutionRecoveryService._dict_diverges(left.balances, right.balances):
            count += 1
        left_positions = {position.symbol: (position.position_qty, position.avg_entry_price) for position in left.positions}
        right_positions = {position.symbol: (position.position_qty, position.avg_entry_price) for position in right.positions}
        if ExecutionRecoveryService._position_diverges(left_positions, right_positions):
            count += 1
        for field_name in (
            "realized_pnl",
            "unrealized_pnl",
            "total_equity",
            "gross_exposure",
            "net_exposure",
        ):
            if abs(getattr(left, field_name) - getattr(right, field_name)) > 1e-9:
                count += 1
        return count

    @staticmethod
    def _dict_diverges(left: dict[str, float], right: dict[str, float]) -> bool:
        keys = set(left) | set(right)
        return any(abs(left.get(key, 0.0) - right.get(key, 0.0)) > 1e-9 for key in keys)

    @staticmethod
    def _position_diverges(
        left: dict[str, tuple[float, float]],
        right: dict[str, tuple[float, float]],
    ) -> bool:
        keys = set(left) | set(right)
        for key in keys:
            left_qty, left_avg = left.get(key, (0.0, 0.0))
            right_qty, right_avg = right.get(key, (0.0, 0.0))
            if abs(left_qty - right_qty) > 1e-9 or abs(left_avg - right_avg) > 1e-9:
                return True
        return False
