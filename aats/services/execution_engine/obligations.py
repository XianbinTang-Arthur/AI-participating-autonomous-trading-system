from __future__ import annotations

from collections.abc import Awaitable, Callable

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot


class ExecutionReservationError(RuntimeError):
    pass


class ExecutionObligationService:
    _EPSILON = 1e-12

    def __init__(
        self,
        *,
        settings: AATSSettings,
        obligation_repo,
        account_snapshot_loader: Callable[[], Awaitable[ExchangeAccountSnapshot | None]] | None = None,
        price_provider: Callable[[str], float] | None = None,
    ) -> None:
        self.settings = settings
        self.obligation_repo = obligation_repo
        self.account_snapshot_loader = account_snapshot_loader
        self.price_provider = price_provider

    async def reserve_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderObligation | None:
        existing = self.obligation_repo.get_obligation(client_order_id)
        if existing is not None:
            return existing
        if self.account_snapshot_loader is None:
            return None
        snapshot = await self.account_snapshot_loader()
        if snapshot is None:
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
                f"{reserve_currency}:{reserved_amount:.12f}>{available_after_obligations:.12f}"
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
            reference_price=reference_price,
            last_update_ts=utc_now(),
        )
        return self.obligation_repo.save_obligation(obligation)

    def consume_for_fill(self, fill: FillEvent) -> OrderObligation | None:
        obligation = self.obligation_repo.get_obligation(fill.client_order_id)
        if obligation is None:
            return None
        consume_amount = self._fill_consumption_amount(fill=fill, obligation=obligation)
        if consume_amount <= self._EPSILON:
            return obligation
        consumed_amount = obligation.consumed_amount + consume_amount
        updated = obligation.model_copy(
            update={
                "consumed_amount": consumed_amount,
                "status": self._consumption_status(
                    reserved_amount=obligation.reserved_amount,
                    consumed_amount=consumed_amount,
                    released_amount=obligation.released_amount,
                ),
                "last_update_ts": utc_now(),
            }
        )
        return self.obligation_repo.save_obligation(updated)

    def finalize_for_order_state(self, order_state: OrderState) -> OrderObligation | None:
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
        updated = obligation.model_copy(
            update={
                "released_amount": obligation.released_amount + max(remaining_amount, 0.0),
                "status": terminal_status,
                "last_update_ts": utc_now(),
            }
        )
        return self.obligation_repo.save_obligation(updated)

    @classmethod
    def remaining_amount(cls, obligation: OrderObligation) -> float:
        return max(
            obligation.reserved_amount - obligation.consumed_amount - obligation.released_amount,
            0.0,
        )

    def _reservation_spec(self, *, intent: OrderIntent) -> tuple[str | None, float, float | None]:
        base_currency, quote_currency = self._symbol_currencies(intent.symbol)
        reference_price = intent.limit_price if intent.limit_price is not None else self._reference_price(intent.symbol)
        if intent.product_type == "spot":
            if intent.side == "buy":
                if quote_currency is None or reference_price is None or reference_price <= 0.0:
                    return None, 0.0, reference_price
                return quote_currency, intent.quantity * reference_price, reference_price
            if base_currency is None:
                return None, 0.0, reference_price
            return base_currency, intent.quantity, reference_price
        if quote_currency is None or reference_price is None or reference_price <= 0.0:
            return None, 0.0, reference_price
        leverage = max(intent.target_leverage, 1.0)
        return quote_currency, (abs(intent.quantity) * reference_price) / leverage, reference_price

    def _fill_consumption_amount(
        self,
        *,
        fill: FillEvent,
        obligation: OrderObligation,
    ) -> float:
        base_currency, quote_currency = self._symbol_currencies(fill.symbol)
        if obligation.product_type == "spot":
            if obligation.reserve_currency == quote_currency:
                return fill.fill_qty * fill.fill_price
            if obligation.reserve_currency == base_currency:
                return fill.fill_qty
            return 0.0
        leverage = max(fill.target_leverage, 1.0)
        return (abs(fill.fill_qty) * fill.fill_price) / leverage

    def _reference_price(self, symbol: str) -> float | None:
        if self.price_provider is None:
            return None
        price = self.price_provider(symbol)
        return price if price > 0.0 else None

    @staticmethod
    def _snapshot_available_balance(
        *,
        snapshot: ExchangeAccountSnapshot,
        currency: str,
    ) -> float:
        for balance in snapshot.balances:
            if balance.currency == currency:
                return balance.available
        return 0.0

    @classmethod
    def _consumption_status(
        cls,
        *,
        reserved_amount: float,
        consumed_amount: float,
        released_amount: float,
    ) -> str:
        remaining = max(reserved_amount - consumed_amount - released_amount, 0.0)
        if remaining <= cls._EPSILON:
            return "RELEASED"
        if consumed_amount > cls._EPSILON:
            return "PARTIALLY_CONSUMED"
        return "ACTIVE"

    @staticmethod
    def _symbol_currencies(symbol: str) -> tuple[str | None, str | None]:
        if "-" not in symbol:
            return symbol or None, None
        parts = symbol.split("-")
        if len(parts) >= 2:
            return parts[0] or None, parts[1] or None
        return None, None
