from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.exchange import (
    ExchangeAccountSnapshot,
    ExchangeBalance,
    ExchangeFill,
    ExchangeOpenOrder,
    ExchangePosition,
    InstrumentMetadata,
)
from aats.services.execution_engine.okx_rest import OKXRESTClient


class OKXAccountService:
    def __init__(self, *, settings: AATSSettings, client: OKXRESTClient) -> None:
        self.settings = settings
        self.client = client
        self.logger = get_logger("aats.okx_account")
        self._latest_snapshot: ExchangeAccountSnapshot | None = None
        self._last_refresh_error: str | None = None
        self._lock = asyncio.Lock()

    async def refresh(self, *, force: bool = False) -> ExchangeAccountSnapshot | None:
        if not self.settings.account_read_enabled or self.settings.account_backend != "okx":
            return self._latest_snapshot
        if not self.settings.okx_credentials_configured:
            self._last_refresh_error = "credentials_missing"
            return self._latest_snapshot

        async with self._lock:
            if not force and self._latest_snapshot is not None:
                age_seconds = (utc_now() - self._latest_snapshot.fetched_at).total_seconds()
                if age_seconds < self.settings.okx_account_refresh_interval_seconds:
                    return self._latest_snapshot

            try:
                balance_payload = await self.client.get_balance()
                open_orders_payload = await self.client.get_open_orders(symbol=self.settings.default_symbol)
                fills_payload = await self.client.get_fills(
                    symbol=self.settings.default_symbol,
                    limit=self.settings.okx_fill_fetch_limit,
                )
                instruments_payload = await self.client.get_instruments()
                account_config_payload = await self.client.get_account_config()
                if self.settings.trading_product_type == "derivatives":
                    positions_payload = await self.client.get_positions()
                else:
                    # OKX spot accounts expose holdings through balances. The positions
                    # endpoint is not consistently available for spot and can return 400s,
                    # which would otherwise spam the refresh loop logs.
                    positions_payload = {"data": []}

                snapshot = ExchangeAccountSnapshot(
                    account_source="okx",
                    fetched_at=utc_now(),
                    balances=self._parse_balances(balance_payload),
                    positions=self._parse_positions(positions_payload),
                    open_orders=self._parse_open_orders(open_orders_payload),
                    fills=self._parse_fills(fills_payload),
                    instruments=self._parse_instruments(instruments_payload),
                    account_mode=self._parse_account_mode(account_config_payload),
                    raw={
                        "balance": balance_payload,
                        "positions": positions_payload,
                        "open_orders": open_orders_payload,
                        "fills": fills_payload,
                        "instruments": instruments_payload,
                        "account_config": account_config_payload,
                    },
                )
                self._latest_snapshot = snapshot
                self._last_refresh_error = None
                log_event(
                    self.logger,
                    "okx_account_refreshed",
                    balance_count=len(snapshot.balances),
                    position_count=len(snapshot.positions),
                    open_order_count=len(snapshot.open_orders),
                    fill_count=len(snapshot.fills),
                    instrument_count=len(snapshot.instruments),
                )
                return snapshot
            except Exception as exc:
                self._last_refresh_error = str(exc)
                log_event(
                    self.logger,
                    "okx_account_refresh_failed",
                    level="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if self._latest_snapshot is None:
                    raise
                return self._latest_snapshot

    def latest_snapshot(self) -> ExchangeAccountSnapshot | None:
        return self._latest_snapshot

    def instrument_metadata(self, symbol: str) -> InstrumentMetadata | None:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return None
        for instrument in snapshot.instruments:
            if instrument.symbol == symbol:
                return instrument
        return None

    def open_order_count(self, symbol: str | None = None) -> int:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return 0
        if symbol is None:
            return len(snapshot.open_orders)
        return sum(1 for order in snapshot.open_orders if order.instrument_id == symbol)

    def status(self) -> dict[str, Any]:
        if self.settings.account_backend != "okx" or not self.settings.account_read_enabled:
            return {
                "backend": "disabled",
                "enabled": False,
                "credentials_configured": self.settings.okx_credentials_configured,
                "connected": True,
                "fresh": True,
                "last_update_ts": None,
                "last_error": None,
                "ready": True,
                "detail": "account_read_disabled",
                "blockers": [],
            }
        snapshot = self._latest_snapshot
        fresh = False
        blockers: list[str] = []
        if snapshot is not None:
            fresh = (utc_now() - snapshot.fetched_at).total_seconds() <= self.settings.account_state_stale_after_seconds
        if not self.settings.okx_credentials_configured:
            blockers.append("account_credentials_missing")
        if snapshot is None:
            blockers.append("account_snapshot_missing")
        elif not fresh:
            blockers.append("account_state_stale")
        return {
            "backend": "okx" if self.settings.account_backend == "okx" else "disabled",
            "enabled": self.settings.account_read_enabled,
            "credentials_configured": self.settings.okx_credentials_configured,
            "connected": snapshot is not None and self._last_refresh_error is None,
            "fresh": fresh,
            "last_update_ts": snapshot.fetched_at if snapshot is not None else None,
            "last_error": self._last_refresh_error,
            "ready": snapshot is not None and self._last_refresh_error is None and fresh,
            "detail": "okx_account_snapshot",
            "blockers": blockers,
        }

    def recent_fills(self, symbol: str | None = None) -> list[ExchangeFill]:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return []
        if symbol is None:
            return list(snapshot.fills)
        return [fill for fill in snapshot.fills if fill.symbol == symbol]

    @staticmethod
    def _parse_balances(payload: dict[str, Any]) -> list[ExchangeBalance]:
        rows: list[ExchangeBalance] = []
        for account in payload.get("data", []):
            for detail in account.get("details", []):
                total = OKXAccountService._balance_value(detail, "eq", "cashBal")
                # For spot accounts OKX can report `availEq=0` while `availBal`
                # still carries the real spendable quantity. Prefer the explicit
                # cash balance field when present so simulated submit does not
                # treat funded spot accounts as fully frozen.
                available = OKXAccountService._balance_value(detail, "availBal", "availEq", default=total)
                frozen = max(total - available, 0.0)
                rows.append(
                    ExchangeBalance(
                        currency=str(detail.get("ccy")),
                        total=total,
                        available=available,
                        frozen=frozen,
                    )
                )
        return rows

    @staticmethod
    def _balance_value(detail: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            value = detail.get(key)
            if value in {None, ""}:
                continue
            return float(value or 0.0)
        return default

    @staticmethod
    def _parse_positions(payload: dict[str, Any]) -> list[ExchangePosition]:
        positions: list[ExchangePosition] = []
        for row in payload.get("data", []):
            positions.append(
                ExchangePosition(
                    instrument_id=str(row.get("instId")),
                    symbol=str(row.get("instId")),
                    quantity=float(row.get("pos", 0.0) or 0.0),
                    average_entry_price=(
                        float(row.get("avgPx")) if row.get("avgPx") not in {None, ""} else None
                    ),
                    mark_price=(float(row.get("markPx")) if row.get("markPx") not in {None, ""} else None),
                    notional_usd=(float(row.get("notionalUsd")) if row.get("notionalUsd") not in {None, ""} else None),
                    side=str(row.get("posSide", "net")),
                )
            )
        return positions

    @staticmethod
    def _parse_open_orders(payload: dict[str, Any]) -> list[ExchangeOpenOrder]:
        rows: list[ExchangeOpenOrder] = []
        for row in payload.get("data", []):
            created_ts = row.get("cTime")
            updated_ts = row.get("uTime")
            rows.append(
                ExchangeOpenOrder(
                    instrument_id=str(row.get("instId")),
                    client_order_id=str(row.get("clOrdId")) if row.get("clOrdId") else None,
                    exchange_order_id=str(row.get("ordId")),
                    side=str(row.get("side")),
                    order_type=str(row.get("ordType")),
                    status=str(row.get("state", "")).upper(),
                    quantity=float(row.get("sz", 0.0) or 0.0),
                    filled_quantity=float(row.get("accFillSz", 0.0) or 0.0),
                    price=(float(row.get("px")) if row.get("px") not in {None, ""} else None),
                    created_ts=utc_now() if not created_ts else datetime_from_ms(str(created_ts)),
                    updated_ts=utc_now() if not updated_ts else datetime_from_ms(str(updated_ts)),
                )
            )
        return rows

    @staticmethod
    def _parse_fills(payload: dict[str, Any]) -> list[ExchangeFill]:
        rows: list[ExchangeFill] = []
        for row in payload.get("data", []):
            fill_ts = row.get("fillTime") or row.get("ts")
            fill_id = str(row.get("tradeId") or row.get("billId") or row.get("fillId") or "")
            if not fill_id:
                fill_id = f"{row.get('ordId', 'unknown')}-{fill_ts or 'unknown'}"
            rows.append(
                ExchangeFill(
                    fill_id=fill_id,
                    exchange_order_id=str(row.get("ordId") or ""),
                    client_order_id=str(row.get("clOrdId")) if row.get("clOrdId") else None,
                    instrument_id=str(row.get("instId")),
                    symbol=str(row.get("instId")),
                    side=str(row.get("side")),
                    fill_qty=float(row.get("fillSz", row.get("sz", 0.0)) or 0.0),
                    fill_price=float(row.get("fillPx", row.get("px", 0.0)) or 0.0),
                    fee_amount=abs(float(row.get("fee", 0.0) or 0.0)),
                    fee_currency=str(row.get("feeCcy")) if row.get("feeCcy") else None,
                    fill_ts=datetime_from_ms(str(fill_ts)) if fill_ts not in {None, ""} else None,
                )
            )
        return rows

    @staticmethod
    def _parse_instruments(payload: dict[str, Any]) -> list[InstrumentMetadata]:
        instruments: list[InstrumentMetadata] = []
        for row in payload.get("data", []):
            instruments.append(
                InstrumentMetadata(
                    instrument_id=str(row.get("instId")),
                    symbol=str(row.get("instId")),
                    base_currency=str(row.get("baseCcy")),
                    quote_currency=str(row.get("quoteCcy")),
                    lot_size=float(Decimal(str(row.get("lotSz", "0.00000001")))),
                    tick_size=float(Decimal(str(row.get("tickSz", "0.00000001")))),
                    min_size=float(Decimal(str(row.get("minSz", row.get("lotSz", "0.0"))))),
                    state=str(row.get("state", "")),
                    raw=dict(row),
                )
            )
        return instruments

    @staticmethod
    def _parse_account_mode(payload: dict[str, Any]) -> str | None:
        data = payload.get("data", [])
        if not data:
            return None
        return str(data[0].get("acctLv")) if data[0].get("acctLv") is not None else None


def datetime_from_ms(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
