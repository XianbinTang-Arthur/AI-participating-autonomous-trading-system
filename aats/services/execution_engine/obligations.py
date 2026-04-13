from __future__ import annotations

import asyncio
from decimal import Decimal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.services.accounting import (
    derivatives_initial_margin_requirement,
    remaining_obligation_amount,
    resolved_fee_currency,
    resolve_symbol_currencies,
    spot_buy_quote_requirement,
    try_fill_fee_delta_in_quote,
    unsupported_fee_currency_details,
)
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import to_decimal

if TYPE_CHECKING:
    from aats.services.execution_engine.obligation_cache import ObligationHotStateCache


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
        obligation_cache: "ObligationHotStateCache | None" = None,
    ) -> None:
        self.settings = settings
        self.obligation_repo = obligation_repo
        self.account_snapshot_loader = account_snapshot_loader
        self.price_provider = price_provider
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(settings=settings)
        self._reservation_lock = asyncio.Lock()
        # Stage 6 Slice 6.5：跨进程 obligation 缓存。每次 save_obligation 返回后
        # best-effort publish 到 cache，让 decision/gateway 进程的 active_obligations
        # 读路径不用打 Postgres。None = cache 未接线（单测 / recovery-only path），
        # 此时本 service 的行为和 6.5 之前完全一样。
        # 设计文档：docs/task/stage_6_slice_6_5_obligation_hot_state_design.md
        self._obligation_cache = obligation_cache

    async def reserve_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderObligation | None:
        async with self._reservation_lock:
            obligation, snapshot_available = await self._build_reservation_for_intent(
                intent=intent,
                client_order_id=client_order_id,
            )
            if obligation is None:
                return None
            # snapshot_available 为 None 表示 obligation 已存在（幂等返回），
            # 此时跳过事务保存直接返回。
            if snapshot_available is not None:
                saved = self.obligation_repo.reserve_obligation_transactional(
                    obligation=obligation,
                    snapshot_available_balance=snapshot_available,
                    epsilon=self._EPSILON,
                )
            else:
                saved = obligation
            # Stage 6 Slice 6.5：async path 直接 await publish（保证跨进程同步
            # 在返回调用方前至少进入 event loop scheduler）；cache 内部 best-effort
            # 失败不抛，publish 本身有 D9 idempotent 保护。
            if saved is not None and self._obligation_cache is not None:
                await self._obligation_cache.publish(saved)
            return saved

    def persist_previewed_obligation(self, obligation: OrderObligation | None) -> OrderObligation | None:
        if obligation is None:
            return None
        saved = self.obligation_repo.save_obligation(obligation)
        # Stage 6 Slice 6.5：sync path 用 fire_and_forget_publish。eager local apply
        # 保证同 stack 内 read-after-write 立即可见，Redis+NATS 走 schedule task。
        if saved is not None and self._obligation_cache is not None:
            self._obligation_cache.fire_and_forget_publish(saved)
        return saved

    async def preview_reservation_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> OrderObligation | None:
        obligation, _ = await self._build_reservation_for_intent(
            intent=intent,
            client_order_id=client_order_id,
        )
        return obligation

    async def _build_reservation_for_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
    ) -> tuple[OrderObligation | None, Decimal | None]:
        """构建 reservation obligation 并返回 (obligation, snapshot_available_balance)。

        当 obligation 已存在（幂等）或不需要 reservation 时返回 (existing/None, None)。
        """
        existing = self.obligation_repo.get_obligation(client_order_id)
        if existing is not None:
            return existing, None
        if self.account_snapshot_loader is None:
            if self._account_snapshot_required():
                raise ExecutionReservationError("local_obligation_account_snapshot_unavailable")
            return None, None
        snapshot = await self.account_snapshot_loader()
        if snapshot is None:
            if self._account_snapshot_required():
                raise ExecutionReservationError("local_obligation_account_snapshot_unavailable")
            return None, None

        reserve_currency, reserved_amount, reference_price = self._reservation_spec(intent=intent)
        if reserve_currency is None or reserved_amount <= self._EPSILON:
            return None, None

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
        return obligation, available_balance

    def _account_snapshot_required(self) -> bool:
        return self.settings.account_backend == "okx" and self.settings.account_read_enabled

    def consume_for_fill(self, fill: FillEvent) -> OrderObligation | None:
        updated = self.preview_obligation_for_fill(fill)
        if updated is None:
            return None
        saved = self.obligation_repo.save_obligation(updated)
        # Stage 6 Slice 6.5：同 persist_previewed_obligation 模板。
        if saved is not None and self._obligation_cache is not None:
            self._obligation_cache.fire_and_forget_publish(saved)
        return saved

    def preview_obligation_for_fill(self, fill: FillEvent) -> OrderObligation | None:
        obligation = self.obligation_repo.get_obligation(fill.client_order_id)
        if obligation is None:
            return None
        return self._apply_fill_to_obligation(fill, obligation)

    def _apply_fill_to_obligation(self, fill: FillEvent, obligation: OrderObligation) -> OrderObligation:
        """对给定 obligation 应用单笔 fill，返回更新后的 obligation（不写 DB）。"""
        if fill.fill_id in obligation.consumed_fill_ids:
            return obligation
        if fill.fill_id in obligation.blocked_fill_ids:
            return obligation
        consume_amount, processing_failure = self._fill_consumption_amount(fill=fill, obligation=obligation)
        if processing_failure is not None:
            return obligation.model_copy(
                update={
                    "blocked_fill_ids": [*obligation.blocked_fill_ids, fill.fill_id],
                    "processing_failure_reason": "unsupported_fee_currency",
                    "processing_failure_details": processing_failure,
                    "last_update_ts": utc_now(),
                }
            )
        if consume_amount <= self._EPSILON:
            return obligation
        consumed_amount = obligation.consumed_amount + consume_amount
        return obligation.model_copy(
            update={
                "consumed_amount": consumed_amount,
                "consumed_fill_ids": [*obligation.consumed_fill_ids, fill.fill_id],
                "processing_failure_reason": None,
                "processing_failure_details": {},
                "status": self._consumption_status(
                    reserved_amount=obligation.reserved_amount,
                    consumed_amount=consumed_amount,
                    released_amount=obligation.released_amount,
                ),
                "last_update_ts": utc_now(),
            }
        )

    def preview_chained_fill_obligations_and_finalize(
        self,
        fills: list[FillEvent],
        order_state: OrderState,
    ) -> tuple[list[OrderObligation | None], OrderObligation | None]:
        """一次性计算批量 fill 的 obligation 链式更新 + 终态 finalization。

        从 repo 只读一次 obligation，依次应用每笔 fill 的消耗（内存中链式
        累积），最后计算终态释放。返回 (per_fill_obligations, final_obligation)。
        """
        if not fills:
            return [], self.preview_obligation_for_order_state(order_state)
        obligation = self.obligation_repo.get_obligation(fills[0].client_order_id)
        per_fill: list[OrderObligation | None] = []
        for fill in fills:
            if obligation is None:
                per_fill.append(None)
                continue
            updated = self._apply_fill_to_obligation(fill, obligation)
            per_fill.append(updated)
            obligation = updated
        final = self._apply_finalization_to_obligation(order_state, obligation)
        return per_fill, final

    def _apply_finalization_to_obligation(
        self, order_state: OrderState, obligation: OrderObligation | None,
    ) -> OrderObligation | None:
        """对给定 obligation 应用终态 finalization（不写 DB）。"""
        if order_state.status not in {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}:
            return None
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
                "processing_failure_reason": obligation.processing_failure_reason,
                "processing_failure_details": dict(obligation.processing_failure_details),
                "last_update_ts": utc_now(),
            }
        )

    def finalize_for_order_state(self, order_state: OrderState) -> OrderObligation | None:
        updated = self.preview_obligation_for_order_state(order_state)
        if updated is None:
            return None
        saved = self.obligation_repo.save_obligation(updated)
        # Stage 6 Slice 6.5：同 persist_previewed_obligation 模板。
        if saved is not None and self._obligation_cache is not None:
            self._obligation_cache.fire_and_forget_publish(saved)
        return saved

    def preview_obligation_for_order_state(self, order_state: OrderState) -> OrderObligation | None:
        obligation = self.obligation_repo.get_obligation(order_state.client_order_id)
        return self._apply_finalization_to_obligation(order_state, obligation)

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
            # Fix P2-7：spot sell 时 fee 可能以 base 币种计费，需预留最坏情况手续费。
            taker_fee_bps = self.fee_resolver.taker_fee_bps_decimal(symbol=intent.symbol)
            fee_multiplier = Decimal("1") + (taker_fee_bps / Decimal("10000"))
            return base_currency, intent.quantity * fee_multiplier, reference_price
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
    ) -> tuple[Decimal, dict | None]:
        base_currency, quote_currency = resolve_symbol_currencies(fill.symbol)
        if obligation.product_type == "spot":
            if obligation.reserve_currency == quote_currency:
                fee_delta, fee_error = try_fill_fee_delta_in_quote(
                    fill,
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                )
                if fee_error is not None:
                    return (
                        Decimal("0"),
                        unsupported_fee_currency_details(
                            fill,
                            base_currency=base_currency,
                            quote_currency=quote_currency,
                            error=fee_error,
                        ),
                    )
                return max((fill.fill_qty * fill.fill_price) + (fee_delta or Decimal("0")), Decimal("0")), None
            if obligation.reserve_currency == base_currency:
                fee_currency = resolved_fee_currency(
                    fill=fill,
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                )
                fee_in_reserve = to_decimal(fill.fee_amount) if fee_currency == base_currency else Decimal("0")
                return max(fill.fill_qty + fee_in_reserve, Decimal("0")), None
            return Decimal("0"), None
        leverage = max(to_decimal(fill.target_leverage), Decimal("1"))
        return (abs(fill.fill_qty) * fill.fill_price) / to_decimal(leverage), None

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
