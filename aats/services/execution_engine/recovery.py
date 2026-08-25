from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from aats.bootstrap.settings import AATSSettings

if TYPE_CHECKING:
    from aats.services.execution_engine.obligation_cache import ObligationHotStateCache
    from aats.services.execution_engine.obligation_writer import ObligationWriter
    from aats.services.ledger.persistent_lot_book import PersistentLotBookService
    from aats.services.portfolio_service.outbox import PostgresPortfolioOutboxPublisher
    from aats.services.strategy_engines.sleeve_pnl_projection import SleevePnLProjectionService
from aats.schemas.common import utc_now
from aats.schemas.exchange import AccountBaselineSnapshot
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta, PortfolioSnapshot, is_trusted_baseline_snapshot
from aats.schemas.execution import FillEvent, OrderObligation, OrderState
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import RecoveryStatus
from aats.services.accounting import remaining_obligation_amount
from aats.services.execution_engine.bundle_recovery import obligation_matches_scope, scoped_bundle_recovery_assessment
from aats.services.execution_engine.state_machine import TERMINAL_ORDER_STATES as _TERMINAL_ORDER_STATES
from aats.services.fill_ordering import fill_processing_sort_key
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.runtime_layers import RecoveryPolicy
from aats.services.portfolio_service.position_keys import position_key_for_snapshot_position
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.portfolio_service.fill_projection_writer import save_fill_outcome_direct_legacy_only
from aats.services.portfolio_service.initial_balance import effective_portfolio_initial_usdt_balance
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshot_writer import save_snapshot_direct_legacy_only
from aats.services.execution_engine.obligation_writer import save_obligation_direct_legacy_only
from aats.services.runtime_scope import (
    fills_for_scope,
    latest_reconciliation_for_scope,
    latest_snapshot_for_scope,
    order_states_for_scope,
    runtime_state_scope,
)
from aats.services.strategy_engines.independent.replay import recovery_snapshots_from_allocation_decisions
from aats.bootstrap.logging import get_logger, log_event
from aats.storage.base import (
    ExecutionObligationRepository,
    ExecutionRepository,
    FillOutcomeRepository,
    PortfolioRepository,
    ReconciliationRepository,
    StrategyRuntimeRepository,
)


@dataclass(slots=True)
class RecoveryArtifacts:
    status: RecoveryStatus
    rebuilt_snapshot_saved: bool = False
    rebuilt_snapshot: PortfolioSnapshot | None = None


class ExecutionRecoveryService:
    _RECOVERY_COMPARISON_EPSILON = Decimal("1e-8")
    _STALE_RECONCILIATION_HALT_REASON = "recovery_reconciliation_stale"

    def __init__(
        self,
        *,
        settings: AATSSettings,
        execution_repo: ExecutionRepository,
        obligation_repo: ExecutionObligationRepository,
        portfolio_repo: PortfolioRepository,
        reconciliation_repo: ReconciliationRepository,
        strategy_runtime_repo: StrategyRuntimeRepository | None,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], Decimal],
        kill_switch: KillSwitch,
        bootstrap_portfolio_from_exchange: bool,
        reconciliation_stale_after_seconds: float,
        exchange_coupled: bool | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        fill_outcome_repo: FillOutcomeRepository | None = None,
        event_store: object | None = None,
        obligation_cache: "ObligationHotStateCache | None" = None,
        obligation_writer: "ObligationWriter | None" = None,
        persistent_lot_book_service: "PersistentLotBookService | None" = None,
        portfolio_outbox_publisher: "PostgresPortfolioOutboxPublisher | None" = None,
        sleeve_pnl_projection_service: "SleevePnLProjectionService | None" = None,
    ) -> None:
        self.settings = settings
        self.execution_repo = execution_repo
        self.obligation_repo = obligation_repo
        self.portfolio_repo = portfolio_repo
        self.reconciliation_repo = reconciliation_repo
        self.strategy_runtime_repo = strategy_runtime_repo
        self.reconstruction_service = reconstruction_service
        self.price_provider = price_provider
        self.kill_switch = kill_switch
        self.bootstrap_portfolio_from_exchange = bootstrap_portfolio_from_exchange
        self.exchange_coupled = bool(exchange_coupled) if exchange_coupled is not None else bootstrap_portfolio_from_exchange
        self.reconciliation_stale_after_seconds = reconciliation_stale_after_seconds
        self.fill_outcome_repo = fill_outcome_repo
        self.event_store = event_store
        self.persistent_lot_book_service = persistent_lot_book_service
        self.portfolio_outbox_publisher = portfolio_outbox_publisher
        self.sleeve_pnl_projection_service = sleeve_pnl_projection_service
        # Stage 6 Slice 6.5：跨进程 obligation 缓存。_cleanup_orphan_obligations
        # 修复遗留 obligation 后会 best-effort 广播到 cache，让后续进程读路径
        # 拿到的是 release 后的状态。None = 未接线（legacy path），行为退化。
        # 设计文档：docs/task/stage_6_slice_6_5_obligation_hot_state_design.md
        self._obligation_cache = obligation_cache
        self._obligation_writer = obligation_writer
        self.logger = get_logger("aats.recovery")
        self.recovery_policy = recovery_policy or RecoveryPolicy(
            name="default_recovery",
            product_type=settings.trading_product_type,
            startup_baseline_import_supported=bootstrap_portfolio_from_exchange,
            operator_rebaseline_supported=bootstrap_portfolio_from_exchange,
            account_snapshot_required=bootstrap_portfolio_from_exchange,
            review_required_blocks_resume=bootstrap_portfolio_from_exchange,
            reconciliation_required_for_execution_state=True,
            exchange_portfolio_comparison_enabled=bootstrap_portfolio_from_exchange,
            derivatives_position_comparison_enabled=settings.trading_product_type == "derivatives",
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
        scoped_order_states = order_states_for_scope(self.execution_repo, self.runtime_scope)
        open_orders = [order for order in scoped_order_states if str(order.status).upper() not in _TERMINAL_ORDER_STATES]
        notes: list[str] = []
        rebuilt_snapshot_saved = False
        rebuilt_snapshot_for_event: PortfolioSnapshot | None = None
        divergence_count = 0
        recovery_action: str | None = None
        safe_startup = True
        released_orphan_obligation_count = self._cleanup_orphan_obligations()
        if released_orphan_obligation_count:
            notes.append(f"released_orphan_obligations:{released_orphan_obligation_count}")
        scoped_strategy_bundles = self._scoped_recent_strategy_bundles()
        bundle_recovery = scoped_bundle_recovery_assessment(
            scope=self.runtime_scope,
            order_states=scoped_order_states,
            obligations=self._scoped_active_obligations(),
            strategy_bundles=scoped_strategy_bundles,
        )
        independent_recovery_snapshots = self._independent_recovery_snapshots(
            scoped_order_states=scoped_order_states,
            strategy_bundles=scoped_strategy_bundles,
        )
        if independent_recovery_snapshots:
            notes.append(f"independent_recovery_snapshots:{len(independent_recovery_snapshots)}")
            blocked_snapshot_count = sum(
                1 for item in independent_recovery_snapshots if item.recovery_blockers
            )
            if blocked_snapshot_count:
                notes.append(f"independent_recovery_blocked_books:{blocked_snapshot_count}")
        if bundle_recovery.bundle_recovery_required:
            notes.append(f"bundle_recovery_required:{bundle_recovery.open_bundle_count}")
            if bundle_recovery.unbundled_open_order_count:
                notes.append(f"unbundled_open_orders:{bundle_recovery.unbundled_open_order_count}")

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
                total_fees_paid=PortfolioState.total_fee_delta_in_quote(fills),
            )
            rebuilt = self._rebuild_snapshot_for_validation(
                latest_snapshot=latest_snapshot,
                fills=fills,
            )
            divergence_count = self._divergence_count(latest_snapshot, rebuilt)
            if divergence_count:
                # Fill event log is the more authoritative source — auto-heal
                # by adopting the rebuilt snapshot derived from fills.
                healed_snapshot = rebuilt.model_copy(
                    update={
                        "snapshot_origin": "recovery_auto_healed",
                        "product_type": self.runtime_scope.product_type,
                        "margin_mode": self.runtime_scope.margin_mode,
                    }
                )
                portfolio_state.load_portfolio_snapshot(
                    healed_snapshot,
                    applied_fill_ids={fill.fill_id for fill in fills},
                    total_fees_paid=PortfolioState.total_fee_delta_in_quote(fills),
                )
                self._persist_recovery_snapshot(
                    healed_snapshot,
                    source_component="execution_recovery",
                )
                rebuilt_snapshot_saved = True
                # Note: intentionally NOT setting rebuilt_snapshot_for_event here.
                # The snapshot is persisted above; publishing it as an event during
                # bootstrap would trigger the reconciliation subscriber before the
                # runtime is fully initialised, producing a premature halt finding.
                notes.append(f"auto_healed_portfolio_divergence:{divergence_count}")
                notes.append("stored_snapshot_replaced_by_fill_reconstruction")
            elif self.bootstrap_portfolio_from_exchange:
                notes.append("reconstruction_validation_completed_bootstrap_exchange")
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
                        "snapshot_origin": "recovery_rebuild",
                        "product_type": self.runtime_scope.product_type,
                        "margin_mode": self.runtime_scope.margin_mode,
                    }
                )
                portfolio_state.load_portfolio_snapshot(
                    rebuilt_snapshot,
                    applied_fill_ids={fill.fill_id for fill in fills},
                    total_fees_paid=PortfolioState.total_fee_delta_in_quote(fills),
                )
                self._persist_recovery_snapshot(
                    rebuilt_snapshot,
                    source_component="execution_recovery",
                )
                rebuilt_snapshot_saved = True
                rebuilt_snapshot_for_event = rebuilt_snapshot
                notes.append("portfolio_rebuilt_from_fills")
        else:
            notes.append("cold_start_no_execution_state")

        # --- Fill gap detection & compensation ---
        gap_count = self._detect_and_compensate_fill_gaps(
            fills=fills,
            portfolio_state=portfolio_state,
            notes=notes,
        )
        if gap_count:
            notes.append(f"fill_gap_compensated:{gap_count}")
        self._rebuild_persistent_lot_book_from_recovery_fills(
            fills=fills,
            notes=notes,
        )

        # --- Orphaned decision intent detection ---
        orphaned_intent_count = self._detect_orphaned_intents()
        if orphaned_intent_count:
            notes.append(f"orphaned_order_intents:{orphaned_intent_count}")

        safe_startup, recovery_action = self._apply_reconciliation_safety(
            latest_reconciliation=latest_reconciliation,
            latest_snapshot=latest_snapshot or latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope),
            fills=fills,
            open_orders=open_orders,
            safe_startup=safe_startup,
            recovery_action=recovery_action,
            notes=notes,
        )
        if latest_reconciliation is not None and latest_reconciliation.only_reduce_required:
            notes.append("reconciliation_only_reduce_required")
            notes.extend(latest_reconciliation.only_reduce_reasons)

        if open_orders:
            if bundle_recovery.bundle_recovery_required and not bundle_recovery.recovery_blocking:
                notes.append("structured_strategy_bundle_open_orders_detected")
                recovery_action = recovery_action or "tracking_strategy_bundle_recovery"
            else:
                self._halt_for_recovery(
                    reason="recovery_open_orders_present",
                    action="halted_open_orders_require_review",
                    notes=notes,
                )
                recovery_action = recovery_action or "halted_open_orders_require_review"
                safe_startup = False
                notes.append("open_orders_restored_require_operator_review")

        self._clear_stale_reconciliation_halt_if_resolved(
            latest_reconciliation=latest_reconciliation,
            safe_startup=safe_startup,
            open_order_count=len(open_orders),
            bundle_recovery_required=bundle_recovery.bundle_recovery_required,
            bundle_recovery_blocking=bundle_recovery.recovery_blocking,
            notes=notes,
        )

        only_reduce_reasons = (
            list(latest_reconciliation.only_reduce_reasons)
            if latest_reconciliation is not None
            else []
        )
        reconciliation_only_reduce_required = bool(
            latest_reconciliation is not None and latest_reconciliation.only_reduce_required
        )
        reconciliation_review_required = bool(
            latest_reconciliation is not None and latest_reconciliation.review_required
        )
        resume_blocked_reasons: list[str] = []
        if reconciliation_review_required:
            resume_blocked_reasons.append("operator_rebaseline_required")
        if reconciliation_only_reduce_required:
            resume_blocked_reasons.extend(only_reduce_reasons or ["only_reduce_required"])
        if bundle_recovery.bundle_recovery_required:
            only_reduce_reasons.append("strategy_bundle_recovery_in_progress")
            resume_blocked_reasons.append(
                "strategy_bundle_recovery_requires_review"
                if bundle_recovery.recovery_blocking
                else "strategy_bundle_recovery_in_progress"
            )
        only_reduce_reasons = self._dedupe_notes(only_reduce_reasons)

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
                bundle_recovery_required=bundle_recovery.bundle_recovery_required,
                bundle_recovery_blocking=bundle_recovery.recovery_blocking,
            ),
            recovered_order_count=len(scoped_order_states),
            recovered_fill_count=len(fills),
            recovered_snapshot_available=latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope) is not None,
            rebuilt_snapshot_saved=rebuilt_snapshot_saved,
            recovered_reconciliation_available=latest_reconciliation is not None,
            latest_reconciliation_id=latest_reconciliation.reconciliation_id if latest_reconciliation else None,
            latest_reconciliation_severity=latest_reconciliation.severity if latest_reconciliation else None,
            reconciliation_classification=(
                latest_reconciliation.recovery_classification if latest_reconciliation is not None else None
            ),
            open_order_count=len(open_orders),
            divergence_count=divergence_count,
            safe_startup=safe_startup and not self.kill_switch.halted,
            safe_to_trade=(
                safe_startup
                and not self.kill_switch.halted
                and not bundle_recovery.bundle_recovery_required
                and not reconciliation_only_reduce_required
                and not reconciliation_review_required
            ),
            resume_eligible=(
                safe_startup
                and not self.kill_switch.halted
                and not bundle_recovery.bundle_recovery_required
                and not reconciliation_only_reduce_required
                and not reconciliation_review_required
            ),
            review_required=bool(
                (account_baseline is not None and account_baseline.requires_operator_review)
                or reconciliation_review_required
                or bundle_recovery.recovery_blocking
            ),
            only_reduce_required=bool(
                reconciliation_only_reduce_required
                or bundle_recovery.bundle_recovery_required
            ),
            only_reduce_reasons=only_reduce_reasons,
            unknown_state_details=(
                list(latest_reconciliation.unknown_state_details)
                if latest_reconciliation is not None
                else []
            ),
            rebaseline_available=bool(
                (account_baseline is not None and account_baseline.requires_operator_review)
                or (latest_reconciliation is not None and latest_reconciliation.review_required)
                or self.kill_switch.halted
                or bundle_recovery.recovery_blocking
            ),
            halted=self.kill_switch.halted,
            recovery_action=recovery_action,
            bundle_recovery_required=bundle_recovery.bundle_recovery_required,
            bundle_recovery_count=bundle_recovery.open_bundle_count,
            recoverable_bundle_count=bundle_recovery.recoverable_bundle_count,
            unbundled_open_order_count=bundle_recovery.unbundled_open_order_count,
            bundle_summaries=list(bundle_recovery.bundle_summaries),
            independent_recovery_snapshots=[asdict(item) for item in independent_recovery_snapshots],
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
            resume_blocked_reasons=self._dedupe_notes(resume_blocked_reasons),
            notes=self._dedupe_notes(notes),
        )
        return RecoveryArtifacts(
            status=status,
            rebuilt_snapshot_saved=rebuilt_snapshot_saved,
            rebuilt_snapshot=rebuilt_snapshot_for_event,
        )

    def clear_stale_reconciliation_halt_if_resolved(
        self,
        latest_reconciliation: ReconciliationReport | None = None,
    ) -> bool:
        report = latest_reconciliation_for_scope(self.reconciliation_repo, self.runtime_scope)
        if latest_reconciliation is not None and (
            report is None
            or report.reconciliation_id != latest_reconciliation.reconciliation_id
            or report.as_of_ts != latest_reconciliation.as_of_ts
        ):
            return False

        scoped_order_states = order_states_for_scope(self.execution_repo, self.runtime_scope)
        open_orders = [
            order for order in scoped_order_states if str(order.status).upper() not in _TERMINAL_ORDER_STATES
        ]
        bundle_recovery = scoped_bundle_recovery_assessment(
            scope=self.runtime_scope,
            order_states=scoped_order_states,
            obligations=self._scoped_active_obligations(),
            strategy_bundles=self._scoped_recent_strategy_bundles(),
        )
        fills = fills_for_scope(self.execution_repo, self.runtime_scope)
        latest_snapshot = latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope)
        safe_startup = not bool(fills and latest_snapshot is None and self.bootstrap_portfolio_from_exchange)
        notes: list[str] = []
        cleared = self._clear_stale_reconciliation_halt_if_resolved(
            latest_reconciliation=report,
            safe_startup=safe_startup,
            open_order_count=len(open_orders),
            bundle_recovery_required=bundle_recovery.bundle_recovery_required,
            bundle_recovery_blocking=bundle_recovery.recovery_blocking,
            notes=notes,
        )
        if cleared:
            log_event(
                self.logger,
                "recovery_reconciliation_stale_halt_cleared_after_report",
                reason=self._STALE_RECONCILIATION_HALT_REASON,
                reconciliation_id=report.reconciliation_id if report is not None else None,
            )
        return cleared

    def _persist_recovery_snapshot(
        self,
        snapshot: PortfolioSnapshot,
        *,
        source_component: str,
    ) -> None:
        if self.portfolio_outbox_publisher is not None:
            self.portfolio_outbox_publisher.persist_snapshot_sync(
                snapshot=snapshot,
                source_component=source_component,
            )
            return
        save_snapshot_direct_legacy_only(
            portfolio_repo=self.portfolio_repo,
            snapshot=snapshot,
            source_component=source_component,
            logger=self.logger,
        )

    def _rebuild_snapshot_for_validation(
        self,
        *,
        latest_snapshot: PortfolioSnapshot,
        fills: list[FillEvent],
    ) -> PortfolioSnapshot:
        if not self.bootstrap_portfolio_from_exchange:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=self._recovery_price_provider(latest_snapshot),
            ).model_copy(
                update={
                    "snapshot_origin": "manual_rebuild",
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )

        baseline_snapshot = self._trusted_baseline_snapshot()
        if baseline_snapshot is None:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=self._recovery_price_provider(latest_snapshot),
            ).model_copy(
                update={
                    "snapshot_origin": "manual_rebuild",
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )

        state = PortfolioState(
            initial_usdt_balance=self.reconstruction_service.initial_usdt_balance,
            default_product_type=self.runtime_scope.product_type,
            default_margin_mode=self.runtime_scope.margin_mode,
        )
        state.load_portfolio_snapshot(baseline_snapshot)
        baseline_ts = baseline_snapshot.snapshot_ts
        for fill in sorted(fills, key=fill_processing_sort_key):
            if fill.ingestion_timestamp >= baseline_ts:
                state.apply_fill(fill)
        return self.reconstruction_service.snapshot_builder.build(
            state=state,
            price_provider=self._recovery_price_provider(latest_snapshot),
            snapshot_origin="manual_rebuild",
        ).model_copy(
            update={
                "product_type": self.runtime_scope.product_type,
                "margin_mode": self.runtime_scope.margin_mode,
            }
        )

    def _trusted_baseline_snapshot(self) -> PortfolioSnapshot | None:
        snapshots = self.portfolio_repo.history_for_scope(scope=self.runtime_scope)
        candidates = [
            snapshot
            for snapshot in snapshots
            if is_trusted_baseline_snapshot(snapshot)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.snapshot_ts, item.created_at))

    def _cleanup_orphan_obligations(self) -> int:
        order_states = {
            state.client_order_id: state
            for state in order_states_for_scope(self.execution_repo, self.runtime_scope)
        }
        released = 0
        for obligation in self._scoped_active_obligations():
            order_state = order_states.get(obligation.client_order_id)
            updated = self._resolved_obligation(obligation=obligation, order_state=order_state)
            if updated is None:
                continue
            if self._obligation_writer is not None:
                saved = self._obligation_writer.persist_obligation_sync(
                    obligation=updated,
                    source_component="execution_recovery",
                    reason_code="orphan_obligation_cleanup",
                )
            else:
                saved = save_obligation_direct_legacy_only(
                    obligation_repo=self.obligation_repo,
                    obligation=updated,
                    source_component="execution_recovery",
                    reason_code="orphan_obligation_cleanup",
                    logger=self.logger,
                )
                # Stage 6 Slice 6.5：legacy path。Postgres production path 由
                # obligation writer 的 commit hook 负责发布 cache。
                if saved is not None and self._obligation_cache is not None:
                    self._obligation_cache.fire_and_forget_publish(saved)
            released += 1
        return released

    def _scoped_active_obligations(self) -> list[OrderObligation]:
        obligations: list[OrderObligation] = []
        for obligation in self.obligation_repo.active_obligations():
            if not obligation_matches_scope(obligation, self.runtime_scope):
                continue
            obligations.append(obligation)
        return obligations

    def _scoped_recent_strategy_bundles(self):
        if self.strategy_runtime_repo is None:
            return []
        bundles = self.strategy_runtime_repo.recent_execution_bundles(
            product_type=self.runtime_scope.product_type,
            margin_mode=self.runtime_scope.margin_mode,
            limit=50,
        )
        return [
            bundle
            for bundle in bundles
            if self.runtime_scope.symbol_allowed(bundle.selected_symbol)
        ]

    def _scoped_recent_allocation_decisions(
        self,
        *,
        strategy_bundles: list,
    ) -> list:
        if self.strategy_runtime_repo is None:
            return []
        decisions: list = []
        seen: set[str] = set()
        latest = self.strategy_runtime_repo.latest_allocation_decision(
            product_type=self.runtime_scope.product_type,
            margin_mode=self.runtime_scope.margin_mode,
        )
        if latest is not None and self.runtime_scope.symbol_allowed(latest.symbol):
            decisions.append(latest)
            seen.add(latest.allocation_id)
        get_allocation_decision = getattr(self.strategy_runtime_repo, "get_allocation_decision", None)
        if not callable(get_allocation_decision):
            return decisions
        for bundle in strategy_bundles:
            allocation_id = str(getattr(bundle, "allocation_id", "") or "").strip()
            if not allocation_id or allocation_id in seen:
                continue
            decision = get_allocation_decision(allocation_id)
            if decision is None or not self.runtime_scope.symbol_allowed(decision.symbol):
                continue
            seen.add(allocation_id)
            decisions.append(decision)
        return decisions

    def _independent_recovery_snapshots(
        self,
        *,
        scoped_order_states: list[OrderState],
        strategy_bundles: list,
    ):
        if self.strategy_runtime_repo is None:
            return ()
        decisions = self._scoped_recent_allocation_decisions(strategy_bundles=strategy_bundles)
        if not decisions:
            return ()
        open_orders = [
            order
            for order in scoped_order_states
            if str(order.status).upper() not in _TERMINAL_ORDER_STATES
        ]
        return recovery_snapshots_from_allocation_decisions(
            decisions=decisions,
            open_orders=open_orders,
            recent_bundles=strategy_bundles,
        )

    def _resolved_obligation(
        self,
        *,
        obligation: OrderObligation,
        order_state: OrderState | None,
    ) -> OrderObligation | None:
        if order_state is None:
            return self._terminalized_obligation(obligation=obligation, target_status="FAILED")
        if order_state.status not in _TERMINAL_ORDER_STATES:
            return None
        target_status = "CANCELED" if order_state.status == "CANCELED" else "RELEASED" if order_state.status == "FILLED" else "FAILED"
        return self._terminalized_obligation(obligation=obligation, target_status=target_status)

    @staticmethod
    def _terminalized_obligation(
        *,
        obligation: OrderObligation,
        target_status: str,
    ) -> OrderObligation | None:
        remaining_amount = remaining_obligation_amount(obligation)
        if remaining_amount <= Decimal("1e-12") and obligation.status == target_status:
            return None
        return obligation.model_copy(
            update={
                "released_amount": obligation.released_amount + max(remaining_amount, Decimal("0")),
                "status": target_status,
                "last_update_ts": utc_now(),
            }
        )

    @staticmethod
    def _initial_recovery_state(
        *,
        account_baseline: AccountBaselineSnapshot | None,
        halted: bool,
        latest_reconciliation: ReconciliationReport | None,
        bundle_recovery_required: bool = False,
        bundle_recovery_blocking: bool = False,
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
        if bundle_recovery_blocking:
            return "review_required"
        if bundle_recovery_required:
            return "bundle_recovery"
        if latest_reconciliation is not None:
            if latest_reconciliation.only_reduce_required:
                return "only_reduce"
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
        # Stage 6 Slice 6.4：合并的 KillSwitch 自动跨进程广播
        self.kill_switch.halt(reason=reason)
        notes.append(action)

    def _clear_stale_reconciliation_halt_if_resolved(
        self,
        *,
        latest_reconciliation: ReconciliationReport | None,
        safe_startup: bool,
        open_order_count: int,
        bundle_recovery_required: bool,
        bundle_recovery_blocking: bool,
        notes: list[str],
    ) -> bool:
        status = self.kill_switch.status()
        if not status.get("halted") or status.get("reason") != self._STALE_RECONCILIATION_HALT_REASON:
            return False
        if latest_reconciliation is None:
            return False
        age_seconds = (utc_now() - latest_reconciliation.as_of_ts).total_seconds()
        if age_seconds > self.reconciliation_stale_after_seconds:
            return False
        if (
            latest_reconciliation.halt_required
            or latest_reconciliation.review_required
            or latest_reconciliation.only_reduce_required
            or bool(getattr(latest_reconciliation, "resume_blocking", False))
        ):
            return False
        if not safe_startup or open_order_count or bundle_recovery_required or bundle_recovery_blocking:
            return False
        notes.append("recovery_reconciliation_fresh_explicit_operator_resume_required")
        return False

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

    def _recovery_price_provider(self, snapshot: PortfolioSnapshot) -> Callable[[str], Decimal]:
        snapshot_marks: dict[str, Decimal] = {}
        for position in snapshot.positions:
            if (
                abs(position.unrealized_pnl) <= self._RECOVERY_COMPARISON_EPSILON
                and position.avg_entry_price > Decimal("0")
            ):
                snapshot_marks[position.symbol] = position.avg_entry_price
            elif abs(position.position_qty) > self._RECOVERY_COMPARISON_EPSILON:
                snapshot_marks[position.symbol] = position.position_notional / position.position_qty
            elif position.avg_entry_price > Decimal("0"):
                snapshot_marks[position.symbol] = position.avg_entry_price

        def provider(symbol: str) -> Decimal:
            live_price = self.price_provider(symbol)
            if live_price > Decimal("0"):
                return live_price
            return snapshot_marks.get(symbol, Decimal("0"))

        return provider

    @staticmethod
    def _divergence_count(left, right) -> int:
        count = 0
        if ExecutionRecoveryService._dict_diverges(left.balances, right.balances):
            count += 1
        left_positions = {
            position_key_for_snapshot_position(position): (position.position_qty, position.avg_entry_price)
            for position in left.positions
        }
        right_positions = {
            position_key_for_snapshot_position(position): (position.position_qty, position.avg_entry_price)
            for position in right.positions
        }
        if ExecutionRecoveryService._position_diverges(left_positions, right_positions):
            count += 1
        for field_name in (
            "realized_pnl",
            "unrealized_pnl",
            "total_equity",
            "gross_exposure",
            "net_exposure",
        ):
            if abs(getattr(left, field_name) - getattr(right, field_name)) > ExecutionRecoveryService._RECOVERY_COMPARISON_EPSILON:
                count += 1
        return count

    @staticmethod
    def _dict_diverges(left: dict[str, Decimal], right: dict[str, Decimal]) -> bool:
        keys = set(left) | set(right)
        return any(
            abs(left.get(key, Decimal("0")) - right.get(key, Decimal("0"))) > ExecutionRecoveryService._RECOVERY_COMPARISON_EPSILON
            for key in keys
        )

    @staticmethod
    def _position_diverges(
        left: dict[str, tuple[Decimal, Decimal]],
        right: dict[str, tuple[Decimal, Decimal]],
    ) -> bool:
        keys = set(left) | set(right)
        for key in keys:
            left_qty, left_avg = left.get(key, (Decimal("0"), Decimal("0")))
            right_qty, right_avg = right.get(key, (Decimal("0"), Decimal("0")))
            if (
                abs(left_qty - right_qty) > ExecutionRecoveryService._RECOVERY_COMPARISON_EPSILON
                or abs(left_avg - right_avg) > ExecutionRecoveryService._RECOVERY_COMPARISON_EPSILON
            ):
                return True
        return False

    def _detect_orphaned_intents(self) -> int:
        """Detect order intents that were published but never reached execution.

        Scans recent ``execution.order_intents`` events from the event store and
        checks whether each intent has a corresponding order state in the
        execution repository.  Orphaned intents are logged as warnings but are
        NOT auto-retried — market conditions may have changed since the original
        decision.

        Returns the number of orphaned intents detected.
        """
        if self.event_store is None:
            return 0

        recent_by_topic = getattr(self.event_store, "recent_by_topic", None)
        if not callable(recent_by_topic):
            return 0

        try:
            recent_intents = recent_by_topic("execution.order_intents", limit=200)
        except Exception as exc:
            log_event(
                self.logger,
                "orphaned_intent_scan_failed",
                level="warning",
                error=str(exc),
            )
            return 0

        if not recent_intents:
            return 0

        orphaned = 0
        for envelope in recent_intents:
            payload = getattr(envelope, "payload", None) or {}
            intent_id = str(payload.get("intent_id") or "").strip()
            if not intent_id:
                continue

            # Check scope — only inspect intents for the current runtime scope.
            intent_symbol = str(payload.get("symbol") or "")
            intent_product = str(payload.get("product_type") or "")
            intent_margin = str(payload.get("margin_mode") or "")
            if intent_product and intent_product != self.runtime_scope.product_type:
                continue
            if intent_margin and intent_margin != self.runtime_scope.margin_mode:
                continue
            if intent_symbol and not self.runtime_scope.symbol_allowed(intent_symbol):
                continue

            if self.execution_repo.has_intent(intent_id):
                continue

            # This intent was published but has no order state — orphaned.
            orphaned += 1
            log_event(
                self.logger,
                "orphaned_order_intent_detected",
                level="warning",
                intent_id=intent_id,
                symbol=intent_symbol,
                side=str(payload.get("side") or ""),
                quantity=str(payload.get("quantity") or ""),
                decision_id=str(payload.get("decision_id") or ""),
            )

        return orphaned

    def _detect_and_compensate_fill_gaps(
        self,
        *,
        fills: list[FillEvent],
        portfolio_state: PortfolioState,
        notes: list[str],
    ) -> int:
        """Backfill missing FillOutcomeRecord rows from deterministic replay.

        Note: We cannot rely on ``portfolio_state.has_applied_fill()``
        because ``load_portfolio_snapshot()`` pre-marks ALL fill IDs as
        applied.  The sole reliable detection signal is the absence of a
        FillOutcomeRecord in ``fill_outcome_repo``.

        Returns the number of outcome records actually compensated.
        """
        _ = portfolio_state
        if self.fill_outcome_repo is None or not fills:
            return 0
        compensated_count = 0
        failed_count = 0
        replay_state = PortfolioState(
            initial_usdt_balance=effective_portfolio_initial_usdt_balance(
                self.settings,
                exchange_coupled=self.exchange_coupled,
            ),
            default_product_type=self.runtime_scope.product_type,
            default_margin_mode=self.runtime_scope.margin_mode,
        )
        for fill in sorted(fills, key=fill_processing_sort_key):
            existing_outcome = self.fill_outcome_repo.get_outcome(fill.fill_id)
            balances_before = dict(replay_state.balances)
            application_result = replay_state.apply_fill(fill)
            balances_after = dict(replay_state.balances)
            if existing_outcome is not None:
                continue
            if not application_result.applied:
                continue
            balance_delta = self._balance_delta_event_from_replay(
                fill=fill,
                balances_before=balances_before,
                balances_after=balances_after,
                realized_pnl_delta=application_result.realized_pnl_delta,
                fee_delta=application_result.fee_delta,
            )
            outcome = FillOutcomeRecord.from_fill_and_balance_delta(
                fill=fill,
                balance_delta=balance_delta,
                starting_position_qty=application_result.starting_quantity,
                starting_avg_entry_price=application_result.starting_avg_entry_price,
                ending_position_qty=application_result.ending_quantity,
                ending_avg_entry_price=application_result.ending_avg_entry_price,
            )
            snapshot = self.reconstruction_service.snapshot_builder.build(
                state=replay_state,
                price_provider=self.price_provider,
                decision_id=fill.decision_id,
                source_intent_id=fill.intent_id,
                source_fill_id=fill.fill_id,
                snapshot_origin="fill_derived",
            ).model_copy(
                update={
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )
            try:
                if self.portfolio_outbox_publisher is not None:
                    pre_commit_actions = []
                    if self.sleeve_pnl_projection_service is not None:
                        pre_commit_actions.append(
                            lambda session, outcome=outcome: self.sleeve_pnl_projection_service.save_fill_outcome_in_session(
                                session,
                                outcome=outcome,
                            )
                        )
                    self.portfolio_outbox_publisher.persist_fill_projection_sync(
                        snapshot=snapshot,
                        balance_delta=balance_delta,
                        outcome=outcome,
                        source_component="execution_recovery",
                        pre_commit_actions=tuple(pre_commit_actions),
                    )
                else:
                    save_fill_outcome_direct_legacy_only(
                        fill_outcome_repo=self.fill_outcome_repo,
                        outcome=outcome,
                        source_component="execution_recovery",
                        logger=self.logger,
                    )
            except Exception as exc:  # pragma: no cover - repository specific failure path
                failed_count += 1
                log_event(
                    self.logger,
                    "fill_outcome_backfill_failed",
                    level="error",
                    fill_id=fill.fill_id,
                    symbol=fill.symbol,
                    side=fill.side,
                    qty=str(fill.fill_qty),
                    error=str(exc),
                )
                continue
            compensated_count += 1
            log_event(
                self.logger,
                "fill_outcome_backfilled_from_replay",
                level="info",
                fill_id=fill.fill_id,
                symbol=fill.symbol,
                side=fill.side,
                qty=str(fill.fill_qty),
                realized_pnl_delta=str(outcome.realized_pnl_delta),
                fee_delta=str(outcome.fee_delta),
            )
        if failed_count:
            notes.append(f"fill_gap_backfill_failed:{failed_count}")
        return compensated_count

    def _rebuild_persistent_lot_book_from_recovery_fills(
        self,
        *,
        fills: list[FillEvent],
        notes: list[str],
    ) -> None:
        if self.persistent_lot_book_service is None or not fills:
            return
        try:
            self.persistent_lot_book_service.rebuild_from_fills(
                fills=fills,
                product_type=self.runtime_scope.product_type,
                margin_mode=self.runtime_scope.margin_mode,
            )
        except Exception as exc:  # pragma: no cover - repository specific failure path
            notes.append("persistent_lot_book_rebuild_failed:1")
            log_event(
                self.logger,
                "persistent_lot_book_rebuild_from_recovery_failed",
                level="warning",
                fill_count=len(fills),
                product_type=self.runtime_scope.product_type,
                margin_mode=self.runtime_scope.margin_mode,
                error=str(exc),
            )
            return
        notes.append(f"persistent_lot_book_rebuilt:{len(fills)}")
        log_event(
            self.logger,
            "persistent_lot_book_rebuilt_from_recovery_fills",
            level="info",
            fill_count=len(fills),
            product_type=self.runtime_scope.product_type,
            margin_mode=self.runtime_scope.margin_mode,
        )

    @staticmethod
    def _balance_delta_event_from_replay(
        *,
        fill: FillEvent,
        balances_before: dict[str, Decimal],
        balances_after: dict[str, Decimal],
        realized_pnl_delta: Decimal,
        fee_delta: Decimal,
    ) -> PortfolioBalanceDelta:
        currencies = sorted(set(balances_before) | set(balances_after))
        balance_deltas = {
            currency: to_decimal(balances_after.get(currency, 0)) - to_decimal(balances_before.get(currency, 0))
            for currency in currencies
            if to_decimal(balances_after.get(currency, 0)) != to_decimal(balances_before.get(currency, 0))
        }
        return PortfolioBalanceDelta(
            decision_id=fill.decision_id,
            intent_id=fill.intent_id,
            order_id=fill.client_order_id,
            fill_id=fill.fill_id,
            symbol=fill.symbol,
            balances_before={currency: to_decimal(value) for currency, value in balances_before.items()},
            balances_after={currency: to_decimal(value) for currency, value in balances_after.items()},
            balance_deltas=balance_deltas,
            realized_pnl_delta=realized_pnl_delta,
            fee_delta=fee_delta,
            product_type=fill.product_type,
            margin_mode=fill.margin_mode,
        )
