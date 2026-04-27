"""Pre-recovery exchange order reconciliation.

Before the main recovery flow runs, this module queries the exchange for
orders stuck in CREATED / SUBMITTING state (no venue_order_id).  If the
exchange can definitively confirm or deny the order, the local state is
updated so that downstream recovery sees clean data and avoids an
unnecessary halt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.telemetry import traced
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState

logger = get_logger("aats.recovery.exchange_reconciler")

# OKX order states that map to our terminal states.
_OKX_TERMINAL_STATES = {"filled", "canceled", "mmp_canceled"}
_OKX_LIVE_STATES = {"live", "partially_filled"}


class ExchangeOrderQuerier(Protocol):
    """Minimal interface to query a single order from the exchange."""

    async def get_order(
        self, *, symbol: str, client_order_id: str | None = None,
    ) -> dict[str, Any]: ...


class OrderStateUpdater(Protocol):
    """Minimal interface to update a persisted order row."""

    def update_order_state(
        self, *, client_order_id: str, updates: dict[str, Any],
    ) -> bool: ...


class OrderStateOutboxWriter(Protocol):
    async def persist_order_state(
        self,
        *,
        order_state: OrderState,
        key: str,
        source_component: str = "execution_engine",
        emit_execution_error_summary: bool = True,
        sync_execution_order_truth: bool = False,
        history_reason_code: str = "execution_outbox_state_sync",
    ) -> OrderState: ...


@traced("recovery.reconcile_stuck_orders")
async def reconcile_stuck_orders(
    *,
    open_orders: list[dict[str, Any]],
    exchange_client: ExchangeOrderQuerier | None,
    order_repo: Any | None,
    order_state_repo: Any | None = None,
    execution_outbox_publisher: OrderStateOutboxWriter | None = None,
) -> tuple[int, int, list[str]]:
    """Attempt to resolve stuck CREATED/SUBMITTING orders via exchange query.

    Returns:
        (resolved_count, unreachable_count, notes)
    """
    if exchange_client is None:
        return 0, 0, ["exchange_reconciler_skipped_no_client"]

    update_fn = None
    if execution_outbox_publisher is None:
        update_fn = getattr(order_repo, "update_order_state", None)
        if not callable(update_fn):
            return 0, 0, ["exchange_reconciler_skipped_no_writer"]

    stuck_orders = _collect_stuck_orders(open_orders)
    if not stuck_orders:
        return 0, 0, []

    resolved = 0
    unreachable = 0
    notes: list[str] = []

    for row in stuck_orders:
        client_order_id = str(row.get("client_order_id") or row.get("order_id") or "")
        symbol = str(row.get("symbol") or "")
        if not client_order_id or not symbol:
            continue

        try:
            response = await exchange_client.get_order(
                symbol=symbol, client_order_id=client_order_id,
            )
        except Exception as exc:
            unreachable += 1
            log_event(
                logger,
                "exchange_order_query_failed",
                level="warning",
                client_order_id=client_order_id,
                symbol=symbol,
                error=str(exc),
            )
            continue

        new_state = _interpret_exchange_response(response, client_order_id=client_order_id)
        if new_state is None:
            unreachable += 1
            continue

        try:
            if execution_outbox_publisher is not None:
                current = _current_order_state(row=row, order_state_repo=order_state_repo)
                if current is None:
                    unreachable += 1
                    log_event(
                        logger,
                        "exchange_order_reconcile_missing_order_state",
                        level="warning",
                        client_order_id=client_order_id,
                        symbol=symbol,
                    )
                    continue
                persisted = _resolved_order_state(current=current, update=new_state)
                await execution_outbox_publisher.persist_order_state(
                    order_state=persisted,
                    key=symbol,
                    source_component="recovery_exchange_reconciler",
                    sync_execution_order_truth=True,
                    history_reason_code="exchange_order_reconcile",
                )
            else:
                assert update_fn is not None
                update_fn(client_order_id=client_order_id, updates=new_state)
            resolved += 1
            log_event(
                logger,
                "exchange_order_reconciled",
                client_order_id=client_order_id,
                symbol=symbol,
                resolved_status=new_state.get("state"),
            )
        except Exception as exc:
            unreachable += 1
            log_event(
                logger,
                "exchange_order_update_failed",
                level="warning",
                client_order_id=client_order_id,
                error=str(exc),
            )

    if resolved:
        notes.append(f"exchange_reconciled_stuck_orders:{resolved}")
    if unreachable:
        notes.append(f"exchange_unreachable_stuck_orders:{unreachable}")
    return resolved, unreachable, notes


def _collect_stuck_orders(open_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter for orders in CREATED/SUBMITTING with no venue_order_id."""
    stuck = []
    for row in open_orders:
        state = str(row.get("state") or "").upper()
        if state not in {"CREATED", "SUBMITTING"}:
            continue
        if row.get("venue_order_id"):
            continue
        stuck.append(row)
    return stuck


def _interpret_exchange_response(
    response: dict[str, Any],
    *,
    client_order_id: str,
) -> dict[str, Any] | None:
    """Map exchange API response to a local state update dict.

    Returns None if the response is ambiguous (exchange error, network issue).
    """
    code = str(response.get("code", ""))
    data = response.get("data")

    # OKX returns code "0" for success.
    if code == "0" and isinstance(data, list) and data:
        order_data = data[0]
        okx_state = str(order_data.get("state", "")).lower()
        venue_order_id = str(order_data.get("ordId", "")) or None

        if okx_state in _OKX_LIVE_STATES:
            return {
                "state": "SUBMITTED",
                "venue_order_id": venue_order_id,
                "exchange_status": okx_state,
                "last_exchange_update_ts": _okx_ts(order_data.get("uTime")),
            }
        if okx_state in _OKX_TERMINAL_STATES:
            mapped = "FILLED" if okx_state == "filled" else "CANCELED"
            return {
                "state": mapped,
                "venue_order_id": venue_order_id,
                "exchange_status": okx_state,
                "last_exchange_update_ts": _okx_ts(order_data.get("uTime")),
                "filled_qty": _decimal_or_none(order_data.get("accFillSz")),
                "average_fill_price": _decimal_or_none(order_data.get("avgPx")),
                "fees": -(_decimal_or_none(order_data.get("fee")) or Decimal("0")),
            }
        # Unknown exchange state — don't touch.
        log_event(
            logger,
            "exchange_order_unknown_state",
            level="warning",
            client_order_id=client_order_id,
            okx_state=okx_state,
        )
        return None

    # OKX code "51603" = "Order does not exist" — order never reached exchange.
    if code == "51603" or (code != "0" and "does not exist" in str(response).lower()):
        return {
            "state": "FAILED",
            "execution_error": "recovery_exchange_reconciler_order_not_found",
        }

    # Any other error — ambiguous, don't resolve.
    log_event(
        logger,
        "exchange_order_ambiguous_response",
        level="warning",
        client_order_id=client_order_id,
        response_code=code,
    )
    return None


def _current_order_state(
    *,
    row: dict[str, Any],
    order_state_repo: Any | None,
) -> OrderState | None:
    client_order_id = str(row.get("client_order_id") or row.get("order_id") or "")
    if client_order_id and order_state_repo is not None:
        get_order_state = getattr(order_state_repo, "get_order_state", None)
        if callable(get_order_state):
            current = get_order_state(client_order_id)
            if isinstance(current, OrderState):
                return current
    raw_payload = row.get("raw_payload")
    if isinstance(raw_payload, dict):
        nested = raw_payload.get("order_state")
        for candidate in (nested, raw_payload):
            if isinstance(candidate, dict):
                try:
                    return OrderState.model_validate(candidate)
                except Exception:
                    continue
    return None


def _resolved_order_state(*, current: OrderState, update: dict[str, Any]) -> OrderState:
    now = utc_now()
    status = str(update.get("state") or current.status).upper()
    last_exchange_update_ts = update.get("last_exchange_update_ts")
    if not isinstance(last_exchange_update_ts, datetime):
        last_exchange_update_ts = now
    update_payload: dict[str, Any] = {
        "status": status,
        "exchange_order_id": update.get("venue_order_id") or current.exchange_order_id,
        "exchange_status": update.get("exchange_status") or current.exchange_status,
        "last_update_ts": now,
        "last_exchange_update_ts": last_exchange_update_ts,
    }
    if update_payload["exchange_status"]:
        history = list(current.exchange_status_history)
        if update_payload["exchange_status"] not in history:
            history.append(update_payload["exchange_status"])
        update_payload["exchange_status_history"] = history
    if update.get("execution_error"):
        update_payload["execution_error"] = str(update["execution_error"])
    if status == "FILLED":
        filled_qty = update.get("filled_qty")
        if not isinstance(filled_qty, Decimal) or filled_qty <= Decimal("0"):
            filled_qty = current.requested_qty
        update_payload["filled_qty"] = max(current.filled_qty, filled_qty)
        update_payload["remaining_qty"] = Decimal("0")
        if update.get("average_fill_price") is not None:
            update_payload["average_fill_price"] = update["average_fill_price"]
        if update.get("fees") is not None:
            update_payload["fees"] = update["fees"]
    return current.model_copy(update=update_payload)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _okx_ts(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
