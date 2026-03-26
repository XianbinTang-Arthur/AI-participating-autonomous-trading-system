from __future__ import annotations

from decimal import Decimal
from collections.abc import Awaitable, Callable

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.services.accounting import (
    derivatives_initial_margin_requirement,
    fill_fee_cost_in_quote,
    remaining_obligation_amount,
    resolve_symbol_currencies,
    spot_buy_quote_requirement,
)
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import to_decimal


class ExecutionReservationError(RuntimeError):
    pass


class ExecutionObligationService:
    _EPSILON = Decimal("1e-12")

    def __init__(
        self,
        *,
        settings: AATSSettings,
        obligation_repo,
        account_snapshot_loader: Callable[[], Awaitable[ExchangeAccountSnapshot | None]] | None = None,
        price_provider: Callable[[str], Decimal] | None = None,
        fee_resolver: EffectiveFeeResolver | None = None,
    ) -> None:
        self.settings = settings
        self.obligation_repo = obligation_repo
        self.account_snapshot_loader = account_snapshot_loader
        self.price_provider = price_provider
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(settings=settings)

    async def reserve_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderObligation | None:
        obligation = await self.preview_reservation_for_intent(
            intent=intent,
            client_order_id=client_order_id,
        )
        if obligation is None:
            return None
        return self.obligation_repo.save_obligation(obligation)

    def persist_previewed_obligation(self, obligation: OrderObligation | None) -> OrderObligation | None:
        if obligation is None:
            return None
        return self.obligation_repo.save_obligation(obligation)

    async def preview_reservation_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderObligation | None:
        existing = self.obligation_repo.get_obligation(client_order_id)
        if existing is not None:
            return existing
        if self.account_snapshot_loader is None:
            if self._account_snapshot_required():
                raise ExecutionReservationError("local_obligation_account_snapshot_unavailable")
            return None
        snapshot = await self.account_snapshot_loader()
        if snapshot is None:
            if self._account_snapshot_required():
                raise ExecutionReservationError("local_obligation_account_snapshot_unavailable")
            return None

        reserve_currency, reserved_amount, reference_price = self._reservation_spec(intent=intent)
        if reserve_currency is None or reserved_amount <= self._EPSILON:
            return None

        available_balance = self._snapshot_available_balance(snapshot=snapshot, currency=reserve_currency)
        reserved_elsewhere = sum(
            self.remaining_amount(obligation)
            for obligation in self.obligation_repo.active_obligations()
            if obligation.reserve_currency == reserve_currency and obligation.client_order_id != client_order_id
        )
        available_after_obligations = available_balance - reserved_elsewhere
        if reserved_amount > available_after_obligations + self._EPSILON:
            raise ExecutionReservationError(
                "local_obligation_insufficient_available_balance:"
                f"{reserve_currency}:{float(reserved_amount):.12f}>{float(available_after_obligations):.12f}"
            )

        obligation = OrderObligation(
            client_order_id=client_order_id,
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            reserve_currency=reserve_currency,
            reserved_amount=reserved_amount,
            status="ACTIVE",
            product_type=intent.product_type,
            margin_mode=intent.margin_mode,
            strategy_family=intent.strategy_family,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            allocation_id=intent.allocation_id,
            strategy_bundle_id=intent.strategy_bundle_id,
            strategy_leg_role=intent.strategy_leg_role,
            strategy_pair_id=intent.strategy_pair_id,
            strategy_opportunity_kind=intent.strategy_opportunity_kind,
            strategy_execution_mode=intent.strategy_execution_mode,
            strategy_state_phase=intent.strategy_state_phase,
            reference_price=reference_price,
            last_update_ts=utc_now(),
        )
        return obligation

    def _account_snapshot_required(self) -> bool:
        return self.settings.account_backend == "okx" and self.settings.account_read_enabled

    def consume_for_fill(self, fill: FillEvent) -> OrderObligation | None:
        updated = self.preview_obligation_for_fill(fill)
        if updated is None:
            return None
        return self.obligation_repo.save_obligation(updated)

    def preview_obligation_for_fill(self, fill: FillEvent) -> OrderObligation | None:
        obligation = self.obligation_repo.get_obligation(fill.client_order_id)
        if obligation is None:
            return None
        if fill.fill_id in obligation.consumed_fill_ids:
            return obligation
        consume_amount = self._fill_consumption_amount(fill=fill, obligation=obligation)
        if consume_amount <= self._EPSILON:
            return obligation
        consumed_amount = obligation.consumed_amount + consume_amount
        return obligation.model_copy(
            update={
                "consumed_amount": consumed_amount,
                "consumed_fill_ids": [*obligation.consumed_fill_ids, fill.fill_id],
                "status": self._consumption_status(
                    reserved_amount=obligation.reserved_amount,
                    consumed_amount=consumed_amount,
                    released_amount=obligation.released_amount,
                ),
                "last_update_ts": utc_now(),
            }
        )

    def finalize_for_order_state(self, order_state: OrderState) -> OrderObligation | None:
        updated = self.preview_obligation_for_order_state(order_state)
        if updated is None:
            return None
        return self.obligation_repo.save_obligation(updated)

    def preview_obligation_for_order_state(self, order_state: OrderState) -> OrderObligation | None:
        if order_state.status not in {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}:
            return None
        obligation = self.obligation_repo.get_obligation(order_state.client_order_id)
        if obligation is None:
            return None
        remaining_amount = self.remaining_amount(obligation)
        if remaining_amount <= self._EPSILON and obligation.status in {"RELEASED", "CANCELED", "FAILED"}:
            return obligation
        terminal_status = (
            "CANCELED"
            if order_state.status == "CANCELED"
            else "RELEASED"
            if order_state.status == "FILLED"
            else "FAILED"
        )
        return obligation.model_copy(
            update={
                "released_amount": obligation.released_amount + max(remaining_amount, Decimal("0")),
                "status": terminal_status,
                "last_update_ts": utc_now(),
            }
        )

    @classmethod
    def remaining_amount(cls, obligation: OrderObligation) -> Decimal:
        return remaining_obligation_amount(obligation)

    def _reservation_spec(self, *, intent: OrderIntent) -> tuple[str | None, Decimal, Decimal | None]:
        base_currency, quote_currency = resolve_symbol_currencies(intent.symbol)
        reference_price = intent.limit_price if intent.limit_price is not None else self._reference_price(intent.symbol)
        if intent.product_type == "spot":
            if intent.side == "buy":
                reserved_amount = spot_buy_quote_requirement(
                    quantity=intent.quantity,
                    reference_price=reference_price,
                    max_slippage_tolerance_bps=intent.max_slippage_tolerance_bps,
                    taker_fee_bps=self.fee_resolver.taker_fee_bps_decimal(symbol=intent.symbol),
                )
                if quote_currency is None or reserved_amount is None or reserved_amount <= Decimal("0"):
                    return None, Decimal("0"), reference_price
                return quote_currency, reserved_amount, reference_price
            if self._is_margin_backed_smart_arbitrage_spot_short(intent=intent):
                return None, Decimal("0"), reference_price
            if base_currency is None:
                return None, Decimal("0"), reference_price
            return base_currency, intent.quantity, reference_price
        reserved_amount = derivatives_initial_margin_requirement(
            quantity=intent.quantity,
            reference_price=reference_price,
            target_leverage=intent.target_leverage,
            max_slippage_tolerance_bps=intent.max_slippage_tolerance_bps,
        )
        if quote_currency is None or reserved_amount is None or reserved_amount <= Decimal("0"):
            return None, Decimal("0"), reference_price
        return quote_currency, reserved_amount, reference_price

    @staticmethod
    def _is_margin_backed_smart_arbitrage_spot_short(*, intent: OrderIntent) -> bool:
        if intent.product_type != "spot" or intent.side != "sell":
            return False
        if intent.margin_mode not in {"cross", "isolated"}:
            return False
        if intent.strategy_family != "smart_arbitrage":
            return False
        if intent.strategy_execution_mode is None:
            return True
        return intent.strategy_execution_mode == "margin_reverse_carry"

    def _fill_consumption_amount(
        self,
        *,
        fill: FillEvent,
        obligation: OrderObligation,
    ) -> Decimal:
        base_currency, quote_currency = resolve_symbol_currencies(fill.symbol)
        if obligation.product_type == "spot":
            if obligation.reserve_currency == quote_currency:
                return (fill.fill_qty * fill.fill_price) + fill_fee_cost_in_quote(
                    fill,
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                )
            if obligation.reserve_currency == base_currency:
                return fill.fill_qty
            return Decimal("0")
        leverage = max(to_decimal(fill.target_leverage), Decimal("1"))
        return (abs(fill.fill_qty) * fill.fill_price) / to_decimal(leverage)

    def _reference_price(self, symbol: str) -> Decimal | None:
        if self.price_provider is None:
            return None
        price = to_decimal(self.price_provider(symbol))
        return price if price > Decimal("0") else None

    @staticmethod
    def _snapshot_available_balance(
        *,
        snapshot: ExchangeAccountSnapshot,
        currency: str,
    ) -> Decimal:
        for balance in snapshot.balances:
            if balance.currency == currency:
                return to_decimal(balance.available)
        return Decimal("0")

    @classmethod
    def _consumption_status(
        cls,
        *,
        reserved_amount: Decimal,
        consumed_amount: Decimal,
        released_amount: Decimal,
    ) -> str:
        remaining = max(reserved_amount - consumed_amount - released_amount, Decimal("0"))
        if remaining <= cls._EPSILON:
            return "RELEASED"
        if consumed_amount > cls._EPSILON:
            return "PARTIALLY_CONSUMED"
        return "ACTIVE"
