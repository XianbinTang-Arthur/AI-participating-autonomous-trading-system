from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.schemas.operator import ProcessingFailureRecord
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta, PortfolioSnapshotOrigin
from aats.services.accounting import resolve_symbol_currencies
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.ledger.persistent_lot_book import PersistentLotBookService
from aats.services.ledger.settlement_posting import FillSettlementProjection, LedgerSettlementPostingService
from aats.services.portfolio_service.positions import PortfolioService, PortfolioState
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.storage.base import ExecutionRepository, FillOutcomeRepository, PortfolioRepository


class LedgerBackedPortfolioService:
    def __init__(
        self,
        *,
        bus: EventBus,
        state: PortfolioState,
        snapshot_builder: PortfolioSnapshotBuilder,
        portfolio_repo: PortfolioRepository,
        fill_outcome_repo: FillOutcomeRepository,
        price_provider: Callable[[str], Decimal],
        execution_repo: ExecutionRepository,
        settlement_posting_service: LedgerSettlementPostingService,
        persistent_lot_book_service: PersistentLotBookService | None = None,
        initial_usdt_balance: Decimal | float,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.state = state
        self.snapshot_builder = snapshot_builder
        self.portfolio_repo = portfolio_repo
        self.fill_outcome_repo = fill_outcome_repo
        self.price_provider = price_provider
        self.execution_repo = execution_repo
        self.settlement_posting_service = settlement_posting_service
        self.persistent_lot_book_service = persistent_lot_book_service
        self.initial_usdt_balance = Decimal(str(initial_usdt_balance))
        self.metrics = metrics
        self.logger = get_logger("aats.ledger_portfolio_service")
        self.lot_projection_builder = LotBasedProjectionBuilder()

    async def bootstrap_snapshot(self, *, snapshot_origin: PortfolioSnapshotOrigin = "runtime_bootstrap") -> None:
        self._ensure_opening_balance()
        if snapshot_origin in {"runtime_bootstrap", "exchange_import", "operator_rebaseline"} and self.state.loaded_from_exchange_snapshot():
            projection_state = self.state
        else:
            projection_state = self._rebuild_projection_state()
        snapshot = self.snapshot_builder.build(
            state=projection_state,
            price_provider=self.price_provider,
            snapshot_origin=snapshot_origin,
        )
        self.state = projection_state
        self.portfolio_repo.save_snapshot(snapshot)
        self._sync_persistent_lots()
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component="ledger_portfolio_service",
        )

    async def handle_fill_event(self, message: dict) -> None:
        fill = parse_payload(message, FillEvent)
        if self.fill_outcome_repo.get_outcome(fill.fill_id) is not None:
            return
        self._ensure_opening_balance()
        before_balances = self._current_balances(fill=fill)
        before_state = self._rebuild_projection_state(
            exclude_fill_id=fill.fill_id,
            balances_override=before_balances,
        )
        projected_after_state = self._rebuild_projection_state(
            balances_override=before_balances,
        )
        if fill.fill_id not in projected_after_state._applied_fill_ids:
            return
        starting_position = before_state.position_for_fill(fill)
        ending_position = projected_after_state.position_for_fill(fill)
        starting_quantity = Decimal("0") if starting_position is None else starting_position.quantity
        ending_quantity = Decimal("0") if ending_position is None else ending_position.quantity
        starting_avg_entry_price = Decimal("0") if starting_position is None else starting_position.avg_entry_price
        ending_avg_entry_price = Decimal("0") if ending_position is None else ending_position.avg_entry_price
        realized_pnl_delta = projected_after_state.realized_pnl - before_state.realized_pnl
        fee_delta = projected_after_state.total_fees_paid - before_state.total_fees_paid
        projection = FillSettlementProjection(
            base_currency=resolve_symbol_currencies(fill.symbol)[0],
            quote_currency=resolve_symbol_currencies(fill.symbol)[1],
            starting_quantity=starting_quantity,
            ending_quantity=ending_quantity,
            realized_pnl_delta=realized_pnl_delta + fee_delta,
            fee_delta=fee_delta,
        )
        try:
            self.settlement_posting_service.post_fill_effects(fill=fill, projection=projection)
            after_balances = self._current_balances(fill=fill)
            after_state = self._rebuild_projection_state(balances_override=after_balances)
            self.state = after_state
            snapshot = self.snapshot_builder.build(
                state=after_state,
                price_provider=self.price_provider,
                decision_id=fill.decision_id,
                source_intent_id=fill.intent_id,
                source_fill_id=fill.fill_id,
                snapshot_origin="fill_derived",
            )
            self.portfolio_repo.save_snapshot(snapshot)
            self._sync_persistent_lots()
        except Exception as exc:
            await self._emit_processing_failure(
                stage="ledger_portfolio_projection",
                message=str(exc),
                fill=fill,
                retriable=True,
            )
            raise

        balance_delta = PortfolioService._balance_delta_event(
            fill=fill,
            balances_before=before_balances,
            balances_after=after_balances,
            realized_pnl_delta=realized_pnl_delta,
            fee_delta=fee_delta,
        )
        try:
            self.fill_outcome_repo.save_outcome(
                FillOutcomeRecord.from_fill_and_balance_delta(
                    fill=fill,
                    balance_delta=balance_delta,
                    starting_position_qty=starting_quantity,
                    starting_avg_entry_price=starting_avg_entry_price,
                    ending_position_qty=ending_quantity,
                    ending_avg_entry_price=ending_avg_entry_price,
                )
            )
        except Exception as exc:
            log_event(
                self.logger,
                "fill_outcome_persist_failed",
                level="warning",
                **correlation_fields(
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    order_id=fill.client_order_id,
                    fill_id=fill.fill_id,
                    symbol=fill.symbol,
                    error=str(exc),
                ),
            )
            await self._emit_processing_failure(
                stage="ledger_fill_outcome_persist",
                message=str(exc),
                fill=fill,
                retriable=True,
            )
        if self.metrics is not None:
            self.metrics.increment("fills_processed")
            self.metrics.increment("ledger_truth_snapshots")
        log_event(
            self.logger,
            "ledger_fill_applied",
            **correlation_fields(
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                order_id=fill.client_order_id,
                fill_id=fill.fill_id,
                symbol=fill.symbol,
                ending_quantity=ending_quantity,
                realized_pnl_delta=realized_pnl_delta,
                fee_delta=fee_delta,
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_BALANCE_DELTAS,
            key=fill.symbol,
            payload_model=balance_delta,
            source_component="ledger_portfolio_service",
        )
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component="ledger_portfolio_service",
        )

    def _ensure_opening_balance(self) -> None:
        self.settlement_posting_service.ensure_initial_balance(
            currency="USDT",
            amount=self.initial_usdt_balance,
            product_type=self.state.default_product_type,
            margin_mode=self.state.default_margin_mode,
        )

    def _rebuild_projection_state(
        self,
        *,
        exclude_fill_id: str | None = None,
        balances_override: dict[str, Decimal] | None = None,
    ) -> PortfolioState:
        fills = [
            fill
            for fill in self.execution_repo.fills()
            if exclude_fill_id is None or fill.fill_id != exclude_fill_id
        ]
        balances = (
            {currency: Decimal(str(amount)) for currency, amount in balances_override.items()}
            if balances_override is not None
            else self.settlement_posting_service.available_balances(
                product_type=self.state.default_product_type,
                margin_mode=self.state.default_margin_mode,
            )
        )
        if "USDT" not in balances:
            balances["USDT"] = Decimal("0")
        return self.lot_projection_builder.rebuild_portfolio_state(
            fills=fills,
            balances=balances,
            default_product_type=self.state.default_product_type,
            default_margin_mode=self.state.default_margin_mode,
        )

    def _current_balances(self, *, fill: FillEvent) -> dict[str, Decimal]:
        balances = self.settlement_posting_service.available_balances(
            product_type=fill.product_type,
            margin_mode=fill.margin_mode,
        )
        if "USDT" not in balances:
            balances["USDT"] = Decimal("0")
        return balances

    def _sync_persistent_lots(self) -> None:
        if self.persistent_lot_book_service is None:
            return
        self.persistent_lot_book_service.rebuild_from_fills(
            fills=self.execution_repo.fills(),
            product_type=self.state.default_product_type,
            margin_mode=self.state.default_margin_mode,
        )

    async def _emit_processing_failure(
        self,
        *,
        stage: str,
        message: str,
        fill: FillEvent,
        retriable: bool,
    ) -> None:
        if self.metrics is not None:
            self.metrics.increment("processing_failures")
        try:
            await publish_model(
                bus=self.bus,
                topic=topics.PROCESSING_FAILURES,
                key=fill.symbol,
                payload_model=ProcessingFailureRecord(
                    subsystem="ledger_portfolio_service",
                    stage=stage,
                    severity="error",
                    message=message,
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    order_id=fill.client_order_id,
                    fill_id=fill.fill_id,
                    symbol=fill.symbol,
                    product_type=fill.product_type,
                    margin_mode=fill.margin_mode,
                    retriable=retriable,
                    observed_at=utc_now(),
                ),
                source_component="ledger_portfolio_service",
            )
        except Exception:
            pass
