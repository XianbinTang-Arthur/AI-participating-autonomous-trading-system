from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.exchange import (
    ExchangeAccountSnapshot,
    ExchangeAccountConfiguration,
    ExchangeAccountRiskSnapshot,
    ExchangeBalance,
    ExchangeFeeSchedule,
    ExchangeFill,
    ExchangeOpenOrder,
    ExchangePosition,
    ExchangeSystemStatusItem,
    InstrumentMetadata,
)
from aats.services.execution_engine.okx_private_websocket import OKXPrivateWebSocketClient
from aats.services.execution_engine.okx_bills import enrich_okx_bill_category
from aats.services.execution_engine.okx_rest import OKXRequestError, OKXRESTClient, infer_okx_derivatives_inst_type
from aats.services.execution_engine.quantity_rules import internal_quantity_from_exchange
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.strategy_engines.smart_arbitrage.pair_registry import load_pair_definitions


def configured_derivatives_position_mode(*, settings: AATSSettings) -> str | None:
    if settings.trading_product_type != "derivatives":
        return None
    value = str(getattr(settings, "derivatives_position_mode", "net") or "net").strip().lower()
    if value not in {"net", "hedge"}:
        return "net"
    return value


def required_exchange_position_mode(*, settings: AATSSettings) -> str | None:
    configured = configured_derivatives_position_mode(settings=settings)
    if configured == "hedge":
        return "long_short_mode"
    if configured == "net":
        return "net_mode"
    return None


def exchange_position_mode_from_snapshot(snapshot: ExchangeAccountSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    account_configuration = snapshot.account_configuration
    value = (
        account_configuration.position_mode
        if account_configuration is not None and account_configuration.position_mode not in {None, ""}
        else snapshot.position_mode
    )
    if value in {None, ""}:
        return None
    return str(value)


def derivatives_position_mode_contract(
    *,
    settings: AATSSettings,
    snapshot: ExchangeAccountSnapshot | None,
) -> dict[str, Any]:
    configured_mode = configured_derivatives_position_mode(settings=settings)
    required_mode = required_exchange_position_mode(settings=settings)
    exchange_mode = exchange_position_mode_from_snapshot(snapshot)
    match_required = bool(
        settings.trading_product_type == "derivatives"
        and settings.derivatives_require_exchange_pos_mode_match
    )
    matches = (
        None
        if required_mode in {None, ""} or exchange_mode in {None, ""}
        else exchange_mode == required_mode
    )
    blocker_code = None
    startup_error_code = None
    if match_required and settings.trading_product_type == "derivatives":
        if exchange_mode in {None, ""}:
            blocker_code = "okx_position_mode_missing"
            startup_error_code = "derivatives_exchange_runtime_requires_exchange_position_mode"
        elif required_mode is not None and exchange_mode != required_mode:
            blocker_code = "okx_position_mode_mismatch"
            startup_error_code = "derivatives_exchange_runtime_position_mode_mismatch"
    return {
        "configured_derivatives_position_mode": configured_mode,
        "required_exchange_position_mode": required_mode,
        "exchange_position_mode": exchange_mode,
        "exchange_position_mode_label": OKXAccountService._position_mode_label(exchange_mode),
        "exchange_position_mode_matches_configured": matches,
        "position_mode_match_required": match_required,
        "blocker_code": blocker_code,
        "startup_error_code": startup_error_code,
    }


class OKXAccountService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        client: OKXRESTClient,
        private_ws_client: OKXPrivateWebSocketClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.private_ws_client = private_ws_client
        self.logger = get_logger("aats.okx_account")
        self._latest_snapshot: ExchangeAccountSnapshot | None = None
        self._last_refresh_error: str | None = None
        self._lock = asyncio.Lock()
        self._latest_ws_balances: list[ExchangeBalance] | None = None
        self._latest_ws_balances_ts: datetime | None = None
        self._latest_balance_view_ts: datetime | None = None
        self._latest_ws_positions: list[ExchangePosition] | None = None
        self._latest_ws_positions_ts: datetime | None = None
        self._latest_position_view_ts: datetime | None = None
        self._latest_ws_order_rows: dict[str, dict[str, Any]] = {}
        self._latest_ws_fill_rows: dict[str, ExchangeFill] = {}
        self._latest_ws_orders_ts: datetime | None = None
        self._latest_orders_view_ts: datetime | None = None
        self._latest_ws_update_ts: datetime | None = None
        self._latest_recent_bills: list[dict[str, Any]] = []
        self._last_bills_error: str | None = None
        self._aux_payload_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._aux_payload_backoff_until: dict[str, datetime] = {}

    async def refresh(
        self,
        *,
        force: bool = False,
        force_account_state: bool = False,
    ) -> ExchangeAccountSnapshot | None:
        if not self.settings.account_read_enabled or self.settings.account_backend != "okx":
            return self._latest_snapshot
        if not self.settings.okx_credentials_configured:
            self._last_refresh_error = "credentials_missing"
            return None if force or force_account_state else self._latest_snapshot

        async with self._lock:
            state_force = force or force_account_state
            if not state_force and self._latest_snapshot is not None:
                age_seconds = (utc_now() - self._latest_snapshot.fetched_at).total_seconds()
                if age_seconds < self.settings.okx_account_refresh_interval_seconds:
                    return self._latest_snapshot

            try:
                tracked_symbols = self._tracked_symbols()
                _gather_results_1 = await asyncio.gather(
                    self.client.get_balance(),
                    self._cached_aux_payload(
                        "instruments",
                        refresh_interval_seconds=self.settings.okx_instruments_refresh_interval_seconds,
                        force=force,
                        fetcher=lambda: self._get_instruments_payload(tracked_symbols),
                    ),
                    return_exceptions=True,
                )
                for _r in _gather_results_1:
                    if isinstance(_r, Exception):
                        self.logger.warning("gather task failed: %s", _r)
                if state_force:
                    self._raise_account_state_refresh_error(
                        context="balance",
                        result=_gather_results_1[0],
                    )
                balance_payload = _gather_results_1[0] if not isinstance(_gather_results_1[0], Exception) else {}
                instruments_payload = _gather_results_1[1] if not isinstance(_gather_results_1[1], Exception) else {}
                instruments = self._parse_instruments(instruments_payload)
                instrument_map = {instrument.symbol: instrument for instrument in instruments}
                _gather_results_2 = await asyncio.gather(
                    asyncio.gather(*[
                        self.client.get_open_orders(symbol=symbol)
                        for symbol in tracked_symbols
                    ], return_exceptions=True),
                    asyncio.gather(*[
                        self.client.get_fills(
                            symbol=symbol,
                            limit=self.settings.okx_fill_fetch_limit,
                        )
                        for symbol in tracked_symbols
                    ], return_exceptions=True),
                    self._positions_payload(),
                    return_exceptions=True,
                )
                for _r in _gather_results_2:
                    if isinstance(_r, Exception):
                        self.logger.warning("gather task failed: %s", _r)
                if state_force:
                    self._raise_account_state_refresh_error(
                        context="open_orders",
                        result=_gather_results_2[0],
                    )
                    self._raise_account_state_refresh_error(
                        context="fills",
                        result=_gather_results_2[1],
                    )
                    self._raise_account_state_refresh_error(
                        context="positions",
                        result=_gather_results_2[2],
                    )
                open_orders_payloads = _gather_results_2[0] if not isinstance(_gather_results_2[0], Exception) else []
                fills_payloads = _gather_results_2[1] if not isinstance(_gather_results_2[1], Exception) else []
                positions_payload = _gather_results_2[2] if not isinstance(_gather_results_2[2], Exception) else {}
                # Log errors from nested gather results
                for _nested in (open_orders_payloads, fills_payloads):
                    if isinstance(_nested, list):
                        for _r in _nested:
                            if isinstance(_r, Exception):
                                self.logger.warning("gather task failed: %s", _r)
                _gather_results_3 = await asyncio.gather(
                    self._cached_aux_payload_optional(
                        "account_config",
                        refresh_interval_seconds=self.settings.okx_account_config_refresh_interval_seconds,
                        force=force,
                        fetcher=self.client.get_account_config,
                        fallback=self._raw_snapshot_value("account_config"),
                    ),
                    self._cached_aux_payload_optional(
                        "trade_fee",
                        refresh_interval_seconds=self.settings.okx_trade_fee_refresh_interval_seconds,
                        force=force,
                        fetcher=lambda: self._optional_client_call(
                            "get_trade_fee",
                            symbol=self.settings.default_symbol,
                            underlying=(
                                self._fee_underlying(self.settings.default_symbol, instrument_map=instrument_map)
                                if self.settings.trading_product_type == "derivatives"
                                else None
                            ),
                            instrument_family=(
                                self._fee_instrument_family(
                                    self.settings.default_symbol,
                                    instrument_map=instrument_map,
                                )
                                if self.settings.trading_product_type == "derivatives"
                                else None
                            ),
                        ),
                        fallback=self._raw_snapshot_value("trade_fee", default={"code": "0", "data": []}),
                    ),
                    self._cached_aux_payload_optional(
                        "account_position_risk",
                        refresh_interval_seconds=self.settings.okx_account_position_risk_refresh_interval_seconds,
                        force=state_force,
                        fetcher=lambda: self._optional_client_call("get_account_position_risk"),
                        fallback=self._raw_snapshot_value("account_position_risk", default={"code": "0", "data": []}),
                    ),
                    self._cached_aux_payload_optional(
                        "system_status",
                        refresh_interval_seconds=self.settings.okx_system_status_refresh_interval_seconds,
                        force=force,
                        fetcher=lambda: self._optional_client_call("get_system_status"),
                        fallback=self._raw_snapshot_value("system_status", default={"code": "0", "data": []}),
                    ),
                    self._cached_aux_payload_optional(
                        "recent_bills",
                        refresh_interval_seconds=self.settings.okx_bills_refresh_interval_seconds,
                        force=force,
                        fetcher=lambda: self._optional_client_call(
                            "get_bills_details",
                            limit=self.settings.okx_bills_fetch_limit,
                        ),
                        fallback=self._raw_snapshot_value("recent_bills", default={"code": "0", "data": []}),
                    ),
                    self._funding_rate_payloads(
                        tracked_symbols=tracked_symbols,
                        force=force,
                    ),
                    return_exceptions=True,
                )
                for _r in _gather_results_3:
                    if isinstance(_r, Exception):
                        self.logger.warning("gather task failed: %s", _r)
                account_config_payload = _gather_results_3[0] if not isinstance(_gather_results_3[0], Exception) else {}
                trade_fee_payload = _gather_results_3[1] if not isinstance(_gather_results_3[1], Exception) else {"code": "0", "data": []}
                account_risk_payload = _gather_results_3[2] if not isinstance(_gather_results_3[2], Exception) else {"code": "0", "data": []}
                system_status_payload = _gather_results_3[3] if not isinstance(_gather_results_3[3], Exception) else {"code": "0", "data": []}
                bills_payload = _gather_results_3[4] if not isinstance(_gather_results_3[4], Exception) else {"code": "0", "data": []}
                funding_rate_payloads = _gather_results_3[5] if not isinstance(_gather_results_3[5], Exception) else {}
                self._latest_recent_bills = [
                    dict(row) for row in bills_payload.get("data", []) if isinstance(row, dict)
                ]
                self._last_bills_error = None

                snapshot = ExchangeAccountSnapshot(
                    account_source="okx",
                    fetched_at=utc_now(),
                    balances=self._parse_balances(balance_payload),
                    positions=self._parse_positions(positions_payload, instrument_map=instrument_map),
                    open_orders=self._dedupe_open_orders(
                        self._parse_open_orders(
                            self._merge_payloads(open_orders_payloads, context="open_orders"),
                            instrument_map=instrument_map,
                        )
                    ),
                    fills=self._dedupe_fills(
                        self._parse_fills(
                            self._merge_payloads(fills_payloads, context="fills"),
                            instrument_map=instrument_map,
                        )
                    ),
                    instruments=instruments,
                    account_mode=self._parse_account_mode(account_config_payload),
                    position_mode=self._parse_position_mode(account_config_payload),
                    account_configuration=self._parse_account_configuration(account_config_payload),
                    fee_rates=self._parse_fee_rates(trade_fee_payload),
                    fee_schedule=self._parse_fee_schedule(trade_fee_payload),
                    account_risk=self._first_data_row(account_risk_payload),
                    risk_snapshot=self._parse_account_risk_snapshot(account_risk_payload),
                    system_status=self._parse_system_status(system_status_payload),
                    system_status_items=self._parse_system_status_items(system_status_payload),
                    raw={
                        "balance": balance_payload,
                        "positions": positions_payload,
                        "open_orders": self._merge_payloads(open_orders_payloads, context="raw_open_orders"),
                        "fills": self._merge_payloads(fills_payloads, context="raw_fills"),
                        "instruments": instruments_payload,
                        "account_config": account_config_payload,
                        "trade_fee": trade_fee_payload,
                        "account_position_risk": account_risk_payload,
                        "system_status": system_status_payload,
                        "recent_bills": bills_payload,
                        "funding_rate_by_symbol": funding_rate_payloads,
                    },
                )
                rest_fetched_at = snapshot.fetched_at
                snapshot = self._merge_private_ws_state(snapshot)
                self._latest_snapshot = snapshot
                self._latest_balance_view_ts = (
                    self._latest_ws_balances_ts
                    if self._latest_ws_balances is not None
                    and self._latest_ws_balances_ts is not None
                    and self._latest_ws_balances_ts >= rest_fetched_at
                    else rest_fetched_at
                )
                self._latest_position_view_ts = (
                    self._latest_ws_positions_ts
                    if self._latest_ws_positions is not None
                    and self._latest_ws_positions_ts is not None
                    and self._latest_ws_positions_ts >= rest_fetched_at
                    else rest_fetched_at
                )
                self._latest_orders_view_ts = (
                    self._latest_ws_orders_ts
                    if self._latest_ws_orders_ts is not None and self._latest_ws_orders_ts >= rest_fetched_at
                    else rest_fetched_at
                )
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
                if state_force:
                    return None
                return self._latest_snapshot

    @staticmethod
    def _raise_account_state_refresh_error(
        *,
        context: str,
        result: Any,
    ) -> None:
        if isinstance(result, Exception):
            raise RuntimeError(f"okx_account_state_refresh_failed:{context}") from result
        if isinstance(result, list):
            for item in result:
                if isinstance(item, Exception):
                    raise RuntimeError(f"okx_account_state_refresh_failed:{context}") from item

    async def _positions_payload(self) -> dict[str, Any]:
        if self.settings.trading_product_type != "derivatives":
            # OKX spot accounts expose holdings through balances. The positions
            # endpoint is not consistently available for spot and can return 400s,
            # which would otherwise spam the refresh loop logs.
            return {"data": []}
        return await self.client.get_positions()

    async def _cached_aux_payload(
        self,
        cache_key: str,
        *,
        refresh_interval_seconds: float,
        force: bool = False,
        fetcher,
    ) -> dict[str, Any]:
        now = utc_now()
        cached = self._aux_payload_cache.get(cache_key)
        backoff_until = self._aux_payload_backoff_until.get(cache_key)
        if cached is not None and backoff_until is not None:
            if now < backoff_until:
                return cached[1]
            self._aux_payload_backoff_until.pop(cache_key, None)
        if cached is not None and not force:
            fetched_at, payload = cached
            if (now - fetched_at).total_seconds() < refresh_interval_seconds:
                return payload
        payload = await fetcher()
        self._aux_payload_backoff_until.pop(cache_key, None)
        self._aux_payload_cache[cache_key] = (utc_now(), payload)
        return payload

    async def _cached_aux_payload_optional(
        self,
        cache_key: str,
        *,
        refresh_interval_seconds: float,
        force: bool = False,
        fetcher,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._cached_aux_payload(
                cache_key,
                refresh_interval_seconds=refresh_interval_seconds,
                force=force,
                fetcher=fetcher,
            )
        except OKXRequestError as exc:
            if exc.classification == "rate_limited":
                # OKX 限流: 把该 cache_key 的 fetched_at 推到未来, 使
                # 后续刷新周期 (refresh_account_loop 每 15s) 在 backoff
                # 窗口内直接命中缓存, 不再访问 OKX. Retry-After 优先,
                # 否则默认 300s (≈5 个 system_status 60s 周期).
                backoff_seconds = (
                    float(exc.retry_after_seconds)
                    if exc.retry_after_seconds and exc.retry_after_seconds > 0
                    else 300.0
                )
                cached = self._aux_payload_cache.get(cache_key)
                if cached is not None:
                    cached_payload = cached[1]
                elif fallback is not None:
                    cached_payload = fallback
                else:
                    cached_payload = {"code": "0", "data": []}
                # new_fetched_at = now + (backoff - refresh_interval) 让
                # _cached_aux_payload 在 (now + backoff) 之前把缓存视为新鲜.
                now = utc_now()
                self._aux_payload_backoff_until[cache_key] = now + timedelta(seconds=backoff_seconds)
                new_fetched_at = now + timedelta(
                    seconds=backoff_seconds - refresh_interval_seconds
                )
                self._aux_payload_cache[cache_key] = (new_fetched_at, cached_payload)
                log_event(
                    self.logger,
                    "okx_rate_limited_backoff",
                    level="warning",
                    cache_key=cache_key,
                    backoff_seconds=backoff_seconds,
                    path=getattr(exc, "path", None),
                    code=getattr(exc, "code", None),
                    retry_after_seconds=exc.retry_after_seconds,
                )
                return cached_payload
            cached = self._aux_payload_cache.get(cache_key)
            if cached is not None:
                return cached[1]
            if fallback is not None:
                return fallback
            raise
        except Exception:
            cached = self._aux_payload_cache.get(cache_key)
            if cached is not None:
                return cached[1]
            if fallback is not None:
                return fallback
            raise

    def _raw_snapshot_value(self, key: str, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._latest_snapshot is None:
            return default or {"code": "0", "data": []}
        raw = self._latest_snapshot.raw.get(key)
        if isinstance(raw, dict):
            return raw
        return default or {"code": "0", "data": []}

    def latest_snapshot(self) -> ExchangeAccountSnapshot | None:
        return self._latest_snapshot

    async def _funding_rate_payloads(
        self,
        *,
        tracked_symbols: tuple[str, ...],
        force: bool,
    ) -> dict[str, dict[str, Any]]:
        funding_symbols = self._tracked_funding_symbols(tracked_symbols)
        if not funding_symbols:
            return {}
        payloads = await asyncio.gather(
            *[
                self._cached_aux_payload_optional(
                    f"funding_rate:{symbol}",
                    refresh_interval_seconds=self.settings.okx_funding_rate_refresh_interval_seconds,
                    force=force,
                    fetcher=lambda symbol=symbol: self._optional_client_call("get_funding_rate", symbol=symbol),
                    fallback=self._raw_funding_rate_payload(symbol),
                )
                for symbol in funding_symbols
            ],
            return_exceptions=True,
        )
        for _r in payloads:
            if isinstance(_r, Exception):
                self.logger.warning("gather task failed: %s", _r)
        return {
            symbol: payload
            for symbol, payload in zip(funding_symbols, payloads, strict=False)
            if isinstance(payload, dict)
        }

    async def run_private_ws_forever(self) -> None:
        if self.private_ws_client is None or not self.settings.okx_private_balance_position_ws_enabled:
            return
        await self.private_ws_client.run_forever(on_message=self.handle_private_ws_message)

    async def stop_private_ws(self) -> None:
        if self.private_ws_client is not None:
            await self.private_ws_client.stop()

    async def handle_private_ws_message(self, message: dict[str, Any]) -> None:
        arg = message.get("arg")
        if not isinstance(arg, dict):
            return
        channel = str(arg.get("channel") or "")
        if channel == "balance_and_position":
            balances, positions, update_ts = self._parse_balance_and_position_ws(message)
            if balances is None and positions is None:
                return
            effective_update_ts = update_ts or utc_now()
            accepted_balances = False
            accepted_positions = False
            if balances is not None:
                balance_cutoff = max(
                    (
                        item
                        for item in (self._latest_ws_balances_ts, self._latest_balance_view_ts)
                        if item is not None
                    ),
                    default=None,
                )
                if balance_cutoff is not None and effective_update_ts < balance_cutoff:
                    balances = None
                else:
                    self._latest_ws_balances = balances
                    self._latest_ws_balances_ts = effective_update_ts
                    accepted_balances = True
            if positions is not None:
                position_cutoff = max(
                    (
                        item
                        for item in (self._latest_ws_positions_ts, self._latest_position_view_ts)
                        if item is not None
                    ),
                    default=None,
                )
                if position_cutoff is not None and effective_update_ts < position_cutoff:
                    positions = None
                else:
                    self._latest_ws_positions = positions
                    self._latest_ws_positions_ts = effective_update_ts
                    accepted_positions = True
            if not accepted_balances and not accepted_positions:
                return
            self._latest_ws_update_ts = self._current_private_ws_update_ts()
            latest_effective_ts = self._latest_ws_update_ts or effective_update_ts
            if self._latest_snapshot is not None:
                updates: dict[str, Any] = {"fetched_at": max(self._latest_snapshot.fetched_at, latest_effective_ts)}
                if balances is not None:
                    updates["balances"] = self._merge_balances(
                        base=self._latest_snapshot.balances,
                        updates=balances,
                    )
                    self._latest_balance_view_ts = effective_update_ts
                if positions is not None:
                    updates["positions"] = self._merge_positions(
                        base=self._latest_snapshot.positions,
                        updates=positions,
                    )
                    self._latest_position_view_ts = effective_update_ts
                raw = dict(self._latest_snapshot.raw)
                raw["balance_and_position_ws"] = message
                updates["raw"] = raw
                self._latest_snapshot = self._latest_snapshot.model_copy(update=updates)
            return
        if channel != "orders":
            return
        order_rows, fills, update_ts = self._parse_orders_ws(message)
        if not order_rows and not fills:
            return
        effective_update_ts = update_ts or utc_now()
        orders_cutoff = max(
            (
                item
                for item in (
                    self._latest_ws_orders_ts,
                    self._latest_orders_view_ts,
                )
                if item is not None
            ),
            default=None,
        )
        if orders_cutoff is not None and effective_update_ts < orders_cutoff:
            return
        self._latest_ws_orders_ts = effective_update_ts
        self._latest_ws_update_ts = self._current_private_ws_update_ts()
        for row in order_rows:
            key = self._order_row_key(row)
            if not key:
                continue
            existing = self._latest_ws_order_rows.get(key)
            if existing is None or self._row_update_ts(row) >= self._row_update_ts(existing):
                self._latest_ws_order_rows[key] = row
        for fill in fills:
            self._latest_ws_fill_rows[fill.fill_id] = fill
        if self._latest_snapshot is not None:
            raw = dict(self._latest_snapshot.raw)
            raw["orders_ws"] = message
            self._latest_orders_view_ts = effective_update_ts
            self._latest_snapshot = self._latest_snapshot.model_copy(
                update={
                    "fetched_at": max(self._latest_snapshot.fetched_at, self._latest_ws_update_ts or utc_now()),
                    "open_orders": self._current_private_ws_open_orders(
                        instrument_map={item.symbol: item for item in self._latest_snapshot.instruments}
                    ),
                    "fills": self._dedupe_fills([*self._latest_snapshot.fills, *self._latest_ws_fill_rows.values()]),
                    "raw": raw,
                }
            )

    async def recent_bills(self, *, symbol: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        if self.settings.account_backend != "okx" or not self.settings.account_read_enabled:
            return list(self._latest_recent_bills)
        if not self.settings.okx_credentials_configured:
            self._last_bills_error = "credentials_missing"
            return list(self._latest_recent_bills)
        try:
            payload = await self.client.get_bills_details(
                symbol=symbol,
                limit=limit or self.settings.okx_bills_fetch_limit,
            )
            rows = payload.get("data", [])
            self._latest_recent_bills = [dict(row) for row in rows if isinstance(row, dict)]
            self._last_bills_error = None
        except Exception as exc:
            self._last_bills_error = str(exc)
        return list(self._latest_recent_bills)

    def latest_recent_bills(self) -> list[dict[str, Any]]:
        return list(self._latest_recent_bills)

    def recent_bills_summary(self) -> dict[str, Any]:
        rows = list(self._latest_recent_bills)
        return self._recent_bills_summary_from_rows(rows)

    def recent_bills_summary_since(self, *, since_ts: datetime | None = None) -> dict[str, Any]:
        rows = list(self._latest_recent_bills)
        if since_ts is not None:
            rows = [
                row
                for row in rows
                if (bill_ts := self._bill_row_timestamp(row)) is not None and bill_ts > since_ts
            ]
        return self._recent_bills_summary_from_rows(rows)

    def _recent_bills_summary_from_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        category_counts: dict[tuple[str, str, str], int] = {}
        latest_ts: datetime | None = None
        latest_bill_id: str | None = None
        for row in rows:
            bill_type = str(row.get("type") or "unknown")
            sub_type = str(row.get("subType") or "unknown")
            currency = str(row.get("ccy") or "unknown")
            key = (bill_type, sub_type, currency)
            category_counts[key] = category_counts.get(key, 0) + 1
            candidate_ts = self._bill_row_timestamp(row)
            if candidate_ts is not None and (latest_ts is None or candidate_ts > latest_ts):
                latest_ts = candidate_ts
                latest_bill_id = str(row.get("billId") or latest_bill_id or "")
        top_categories = [
            enrich_okx_bill_category(
                bill_type=bill_type,
                sub_type=sub_type,
                currency=currency,
                count=count,
            )
            for (bill_type, sub_type, currency), count in sorted(
                category_counts.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1], item[0][2]),
            )[:5]
        ]
        currencies = sorted({str(row.get("ccy")) for row in rows if row.get("ccy") not in {None, ""}})
        return {
            "available": bool(rows),
            "count": len(rows),
            "latest_bill_id": latest_bill_id,
            "latest_bill_ts": latest_ts,
            "currencies": currencies,
            "top_categories": top_categories,
            "funding_fee_summary": self._recent_funding_fee_summary_from_rows(rows),
            "last_error": self._last_bills_error,
        }

    def recent_funding_fee_summary(self, *, symbol: str | None = None) -> dict[str, Any]:
        rows = [
            row
            for row in self._latest_recent_bills
            if self._is_funding_fee_bill(row) and (symbol is None or str(row.get("instId") or "") == symbol)
        ]
        return self._recent_funding_fee_summary_from_rows(rows, symbol=symbol)

    def funding_schedule(self, *, symbol: str | None = None) -> dict[str, Any]:
        schedule_symbol = str(symbol or self.settings.default_symbol or "").upper()
        row = self._funding_rate_row(schedule_symbol)
        if row is None:
            return {
                "available": False,
                "symbol": schedule_symbol,
                "funding_time": None,
                "next_funding_time": None,
                "funding_interval_hours": None,
                "updated_at": None,
                "source": "unavailable",
            }
        funding_time = self._funding_rate_timestamp(row.get("fundingTime"))
        next_funding_time = self._funding_rate_timestamp(row.get("nextFundingTime"))
        updated_at = self._funding_rate_timestamp(row.get("ts"))
        funding_interval_hours = None
        if funding_time is not None and next_funding_time is not None and next_funding_time > funding_time:
            funding_interval_hours = (
                Decimal(str((next_funding_time - funding_time).total_seconds())) / Decimal("3600")
            )
        return {
            "available": True,
            "symbol": schedule_symbol,
            "funding_time": funding_time,
            "next_funding_time": next_funding_time,
            "funding_interval_hours": funding_interval_hours,
            "updated_at": updated_at,
            "source": "okx_public_funding_rate",
        }

    def _recent_funding_fee_summary_from_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        rows = [
            row
            for row in rows
            if self._is_funding_fee_bill(row) and (symbol is None or str(row.get("instId") or "") == symbol)
        ]
        if not rows:
            return {
                "available": False,
                "count": 0,
                "latest_bill_ts": None,
                "currencies": [],
                "net_total_by_currency": {},
                "absolute_total_by_currency": {},
                "current_position_notional_usd": self._symbol_position_notional_usd(symbol),
                "funding_fee_bps_proxy": None,
                "funding_fee_bps_proxy_per_event": None,
            }
        latest_ts = max((self._bill_row_timestamp(row) for row in rows if self._bill_row_timestamp(row) is not None), default=None)
        net_total_by_currency: dict[str, Decimal] = {}
        absolute_total_by_currency: dict[str, Decimal] = {}
        for row in rows:
            currency = str(row.get("ccy") or "unknown")
            amount = self._bill_row_amount(row)
            net_total_by_currency[currency] = net_total_by_currency.get(currency, Decimal("0")) + amount
            absolute_total_by_currency[currency] = absolute_total_by_currency.get(currency, Decimal("0")) + abs(amount)
        current_position_notional_usd = self._symbol_position_notional_usd(symbol)
        funding_fee_bps_proxy = None
        funding_fee_bps_proxy_per_event = None
        if current_position_notional_usd is not None and current_position_notional_usd > Decimal("0"):
            absolute_total = sum(absolute_total_by_currency.values(), start=Decimal("0"))
            funding_fee_bps_proxy = (absolute_total / current_position_notional_usd) * Decimal("10000")
            if rows:
                funding_fee_bps_proxy_per_event = funding_fee_bps_proxy / Decimal(len(rows))
        return {
            "available": True,
            "count": len(rows),
            "latest_bill_ts": latest_ts,
            "currencies": sorted(net_total_by_currency.keys()),
            "net_total_by_currency": {key: str(value) for key, value in net_total_by_currency.items()},
            "absolute_total_by_currency": {key: str(value) for key, value in absolute_total_by_currency.items()},
            "current_position_notional_usd": current_position_notional_usd,
            "funding_fee_bps_proxy": funding_fee_bps_proxy,
            "funding_fee_bps_proxy_per_event": funding_fee_bps_proxy_per_event,
        }

    def latest_private_order_row(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        for row in self._latest_ws_order_rows.values():
            if str(row.get("instId") or "") != symbol:
                continue
            if order_id is not None and str(row.get("ordId") or "") == str(order_id):
                return dict(row)
            if client_order_id is not None and str(row.get("clOrdId") or "") == str(client_order_id):
                return dict(row)
        return None

    def latest_private_order_fills(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> list[ExchangeFill]:
        rows: list[ExchangeFill] = []
        for fill in self._latest_ws_fill_rows.values():
            if fill.symbol != symbol:
                continue
            if order_id is not None and fill.exchange_order_id == str(order_id):
                rows.append(fill)
                continue
            if client_order_id is not None and fill.client_order_id == str(client_order_id):
                rows.append(fill)
        return sorted(rows, key=lambda item: (item.fill_ts or utc_now(), item.fill_id))

    def instrument_metadata(self, symbol: str) -> InstrumentMetadata | None:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return None
        for instrument in snapshot.instruments:
            if instrument.symbol == symbol:
                return instrument
        return None

    def account_configuration(self) -> ExchangeAccountConfiguration | None:
        snapshot = self._latest_snapshot
        return None if snapshot is None else snapshot.account_configuration

    def fee_schedule(self) -> ExchangeFeeSchedule | None:
        snapshot = self._latest_snapshot
        return None if snapshot is None else snapshot.fee_schedule

    def risk_snapshot(self) -> ExchangeAccountRiskSnapshot | None:
        snapshot = self._latest_snapshot
        return None if snapshot is None else snapshot.risk_snapshot

    def _fee_underlying(self, symbol: str, *, instrument_map: dict[str, InstrumentMetadata]) -> str | None:
        instrument = instrument_map.get(symbol)
        if instrument is not None:
            underlying = str(instrument.raw.get("uly") or "").strip()
            if underlying:
                return underlying
        parts = [part for part in str(symbol or "").split("-") if part]
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
        return None

    def _fee_instrument_family(self, symbol: str, *, instrument_map: dict[str, InstrumentMetadata]) -> str | None:
        instrument = instrument_map.get(symbol)
        if instrument is not None:
            inst_family = str(instrument.raw.get("instFamily") or "").strip()
            if inst_family:
                return inst_family
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
        position_mode_contract = derivatives_position_mode_contract(
            settings=self.settings,
            snapshot=snapshot,
        )
        if snapshot is not None:
            fresh = (utc_now() - snapshot.fetched_at).total_seconds() <= self.settings.account_state_stale_after_seconds
        if not self.settings.okx_credentials_configured:
            blockers.append("account_credentials_missing")
        if snapshot is None:
            blockers.append("account_snapshot_missing")
        elif not fresh:
            blockers.append("account_state_stale")
        elif self.settings.okx_account_config_validation_enabled:
            blockers.extend(self._account_config_blockers(snapshot))
        else:
            blocker_code = position_mode_contract.get("blocker_code")
            if blocker_code not in {None, ""}:
                blockers.append(str(blocker_code))
        if snapshot is not None and self.settings.okx_system_status_gate_enabled:
            blockers.extend(self._system_status_blockers(snapshot))
        blockers = list(dict.fromkeys(blockers))
        fee_schedule = snapshot.fee_schedule if snapshot is not None else None
        account_configuration = snapshot.account_configuration if snapshot is not None else None
        risk_snapshot = snapshot.risk_snapshot if snapshot is not None else None
        private_ws_status = self.private_ws_client.status() if self.private_ws_client is not None else {}
        return {
            "backend": "okx" if self.settings.account_backend == "okx" else "disabled",
            "enabled": self.settings.account_read_enabled,
            "credentials_configured": self.settings.okx_credentials_configured,
            "connected": snapshot is not None and self._last_refresh_error is None,
            "fresh": fresh,
            "last_update_ts": snapshot.fetched_at if snapshot is not None else None,
            "last_error": self._last_refresh_error,
            "ready": snapshot is not None and self._last_refresh_error is None and fresh and not blockers,
            "detail": "okx_account_snapshot",
            "blockers": blockers,
            "configured_derivatives_position_mode": position_mode_contract.get("configured_derivatives_position_mode"),
            "required_exchange_position_mode": position_mode_contract.get("required_exchange_position_mode"),
            "exchange_position_mode": position_mode_contract.get("exchange_position_mode"),
            "exchange_position_mode_label": position_mode_contract.get("exchange_position_mode_label"),
            "exchange_position_mode_matches_configured": position_mode_contract.get(
                "exchange_position_mode_matches_configured"
            ),
            "position_mode_match_required": position_mode_contract.get("position_mode_match_required"),
            "position_mode_contract": position_mode_contract,
            "account_mode": None if snapshot is None else snapshot.account_mode,
            "position_mode": None if snapshot is None else snapshot.position_mode,
            "account_configuration": (
                account_configuration.model_dump(mode="json") if account_configuration is not None else None
            ),
            "risk_snapshot": risk_snapshot.model_dump(mode="json") if risk_snapshot is not None else None,
            "maker_fee_rate": fee_schedule.maker if fee_schedule is not None else None,
            "taker_fee_rate": fee_schedule.taker if fee_schedule is not None else None,
            "fee_rates_source": fee_schedule.source if fee_schedule is not None else None,
            "fee_schedule": fee_schedule.model_dump(mode="json") if fee_schedule is not None else None,
            "system_status_ok": not self._system_status_blockers(snapshot) if snapshot is not None else False,
            "system_status_items": (
                [item.model_dump(mode="json") for item in snapshot.system_status_items]
                if snapshot is not None
                else []
            ),
            "private_ws_connected": bool(private_ws_status.get("connected", False)),
            "private_ws_last_message_ts": private_ws_status.get("last_message_ts"),
            "private_ws_last_error": private_ws_status.get("last_error"),
            "private_ws_fresh": (
                self._latest_ws_update_ts is not None
                and (utc_now() - self._latest_ws_update_ts).total_seconds() <= self.settings.account_state_stale_after_seconds
            ),
            "private_ws_open_order_count": len(
                self._current_private_ws_open_orders(
                    instrument_map={
                        item.symbol: item
                        for item in (snapshot.instruments if snapshot is not None else [])
                    }
                )
            ),
            "private_ws_fill_count": len(self._latest_ws_fill_rows),
            "recent_bills_count": len(self._latest_recent_bills),
            "last_bills_error": self._last_bills_error,
        }

    def effective_taker_fee_bps(self, symbol: str | None = None) -> Decimal | None:
        _ = symbol
        snapshot = self._latest_snapshot
        if snapshot is None:
            return None
        taker = snapshot.fee_schedule.taker if snapshot.fee_schedule is not None else snapshot.fee_rates.get("taker")
        if taker in {None, ""}:
            return None
        return abs(to_decimal(taker)) * Decimal("10000")

    def effective_maker_fee_bps(self, symbol: str | None = None) -> Decimal | None:
        _ = symbol
        snapshot = self._latest_snapshot
        if snapshot is None:
            return None
        maker = snapshot.fee_schedule.maker if snapshot.fee_schedule is not None else snapshot.fee_rates.get("maker")
        if maker in {None, ""}:
            return None
        return to_decimal(maker) * Decimal("10000")

    def funding_fee_bps_proxy(self, symbol: str | None = None) -> Decimal | None:
        summary = self.recent_funding_fee_summary(symbol=symbol)
        proxy = summary.get("funding_fee_bps_proxy")
        if proxy in {None, ""}:
            return None
        return to_decimal(proxy)

    def funding_fee_bps_proxy_per_event(self, symbol: str | None = None) -> Decimal | None:
        summary = self.recent_funding_fee_summary(symbol=symbol)
        proxy = summary.get("funding_fee_bps_proxy_per_event")
        if proxy in {None, ""}:
            return None
        return to_decimal(proxy)

    def next_funding_time(self, symbol: str | None = None) -> datetime | None:
        schedule = self.funding_schedule(symbol=symbol)
        next_funding_time = schedule.get("next_funding_time")
        return next_funding_time if isinstance(next_funding_time, datetime) else None

    def funding_interval_hours(self, symbol: str | None = None) -> Decimal | None:
        schedule = self.funding_schedule(symbol=symbol)
        interval = schedule.get("funding_interval_hours")
        if interval in {None, ""}:
            return None
        return to_decimal(interval)

    def recent_fills(self, symbol: str | None = None) -> list[ExchangeFill]:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return []
        if symbol is None:
            return list(snapshot.fills)
        return [fill for fill in snapshot.fills if fill.symbol == symbol]

    def _tracked_symbols(self) -> tuple[str, ...]:
        tracked = list(self.settings.expanded_allowed_symbols())
        if not tracked:
            tracked = [self.settings.default_symbol]
        if self.settings.trading_product_type == "derivatives" and self.settings.smart_arbitrage_enabled:
            for pair in load_pair_definitions(
                settings=self.settings,
                primary_symbol=self.settings.default_symbol,
            ):
                for symbol in (pair.spot_symbol, pair.hedge_symbol):
                    if symbol not in tracked:
                        tracked.append(symbol)
        return tuple(tracked)

    @staticmethod
    def _tracked_funding_symbols(tracked_symbols: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                symbol
                for symbol in tracked_symbols
                if infer_okx_derivatives_inst_type(symbol) == "SWAP"
            )
        )

    def _raw_funding_rate_payload(self, symbol: str) -> dict[str, Any]:
        if self._latest_snapshot is None:
            return {"code": "0", "data": []}
        raw = self._latest_snapshot.raw.get("funding_rate_by_symbol")
        if isinstance(raw, dict):
            payload = raw.get(symbol)
            if isinstance(payload, dict):
                return payload
        return {"code": "0", "data": []}

    def _funding_rate_row(self, symbol: str) -> dict[str, Any] | None:
        payload = self._raw_funding_rate_payload(symbol)
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and str(row.get("instId") or "").upper() == symbol:
                return row
        return None

    @staticmethod
    def _funding_rate_timestamp(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        try:
            return datetime_from_ms(str(value))
        except (TypeError, ValueError):
            return None

    async def _get_instruments_payload(self, tracked_symbols: tuple[str, ...]) -> dict[str, Any]:
        try:
            return await self.client.get_instruments(symbols=tracked_symbols)
        except TypeError:
            return await self.client.get_instruments()

    def _symbol_position_notional_usd(self, symbol: str | None) -> Decimal | None:
        snapshot = self._latest_snapshot
        if snapshot is None:
            return None
        positions = snapshot.positions if symbol is None else [row for row in snapshot.positions if row.symbol == symbol]
        total = sum((abs(to_decimal(row.notional_usd or 0)) for row in positions), start=Decimal("0"))
        return total if total > Decimal("0") else None

    def _merge_private_ws_state(self, snapshot: ExchangeAccountSnapshot) -> ExchangeAccountSnapshot:
        if self._latest_ws_update_ts is None:
            return snapshot
        updates: dict[str, Any] = {"fetched_at": max(snapshot.fetched_at, self._latest_ws_update_ts)}
        if self._latest_ws_balances is not None and self._latest_ws_balances_ts is not None and self._latest_ws_balances_ts >= snapshot.fetched_at:
            updates["balances"] = self._merge_balances(
                base=snapshot.balances,
                updates=self._latest_ws_balances,
            )
        if self._latest_ws_positions is not None and self._latest_ws_positions_ts is not None and self._latest_ws_positions_ts >= snapshot.fetched_at:
            updates["positions"] = self._merge_positions(
                base=snapshot.positions,
                updates=self._latest_ws_positions,
            )
        if self._latest_ws_order_rows and self._latest_ws_orders_ts is not None and self._latest_ws_orders_ts >= snapshot.fetched_at:
            updates["open_orders"] = self._current_private_ws_open_orders(
                instrument_map={item.symbol: item for item in snapshot.instruments}
            )
        if self._latest_ws_fill_rows and self._latest_ws_orders_ts is not None and self._latest_ws_orders_ts >= snapshot.fetched_at:
            updates["fills"] = self._dedupe_fills([*snapshot.fills, *self._latest_ws_fill_rows.values()])
        return snapshot.model_copy(update=updates)

    @staticmethod
    def _merge_balances(
        *,
        base: list[ExchangeBalance],
        updates: list[ExchangeBalance],
    ) -> list[ExchangeBalance]:
        if not updates:
            return list(base)
        merged: dict[str, ExchangeBalance] = {item.currency: item for item in base}
        for item in updates:
            if item.total == Decimal("0") and item.available == Decimal("0") and item.frozen == Decimal("0"):
                merged.pop(item.currency, None)
                continue
            merged[item.currency] = item
        return list(merged.values())

    @staticmethod
    def _merge_positions(
        *,
        base: list[ExchangePosition],
        updates: list[ExchangePosition],
    ) -> list[ExchangePosition]:
        if not updates:
            return list(base)
        merged: dict[tuple[str, str, str | None], ExchangePosition] = {
            (item.symbol, item.side, item.margin_mode): item for item in base
        }
        for item in updates:
            key = (item.symbol, item.side, item.margin_mode)
            if item.margin_mode in {None, ""}:
                matching_keys = [existing_key for existing_key in merged if existing_key[:2] == (item.symbol, item.side)]
                if len(matching_keys) == 1:
                    key = matching_keys[0]
            if item.quantity == Decimal("0"):
                merged.pop(key, None)
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                continue
            merged[key] = existing.model_copy(
                update={
                    "instrument_id": item.instrument_id or existing.instrument_id,
                    "symbol": item.symbol or existing.symbol,
                    "side": item.side or existing.side,
                    "quantity": item.quantity,
                    "average_entry_price": (
                        item.average_entry_price
                        if item.average_entry_price is not None
                        else existing.average_entry_price
                    ),
                    "mark_price": item.mark_price if item.mark_price is not None else existing.mark_price,
                    "notional_usd": item.notional_usd if item.notional_usd is not None else existing.notional_usd,
                    "margin_mode": item.margin_mode if item.margin_mode is not None else existing.margin_mode,
                    "margin_currency": (
                        item.margin_currency if item.margin_currency is not None else existing.margin_currency
                    ),
                    "leverage": item.leverage if item.leverage is not None else existing.leverage,
                    "margin_allocated": (
                        item.margin_allocated
                        if item.margin_allocated is not None
                        else existing.margin_allocated
                    ),
                    "maintenance_margin": (
                        item.maintenance_margin
                        if item.maintenance_margin is not None
                        else existing.maintenance_margin
                    ),
                    "margin_ratio": item.margin_ratio if item.margin_ratio is not None else existing.margin_ratio,
                    "liquidation_price": (
                        item.liquidation_price
                        if item.liquidation_price is not None
                        else existing.liquidation_price
                    ),
                    "unrealized_pnl": (
                        item.unrealized_pnl
                        if item.unrealized_pnl is not None
                        else existing.unrealized_pnl
                    ),
                    "instrument_family": (
                        item.instrument_family
                        if item.instrument_family is not None
                        else existing.instrument_family
                    ),
                    "settle_currency": (
                        item.settle_currency
                        if item.settle_currency is not None
                        else existing.settle_currency
                    ),
                }
            )
        return list(merged.values())

    def _current_private_ws_update_ts(self) -> datetime | None:
        candidates = [
            self._latest_ws_balances_ts,
            self._latest_ws_positions_ts,
            self._latest_ws_orders_ts,
        ]
        return max((item for item in candidates if item is not None), default=None)

    async def _optional_client_call(self, method_name: str, **kwargs: Any) -> dict[str, Any]:
        method = getattr(self.client, method_name, None)
        if method is None:
            return {"code": "0", "data": []}
        try:
            payload = await method(**kwargs)
        except OKXRequestError as exc:
            # rate_limited 上抛给 _cached_aux_payload_optional 触发动态退避,
            # 避免每个刷新周期都打 OKX 公共端点 → 持续 429.
            # 其它 OKX 错误 (business_error / network_error 等) 保持原降级语义.
            if exc.classification == "rate_limited":
                raise
            log_event(
                self.logger,
                "optional_client_call_failed",
                level="warning",
                method=method_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {"code": "0", "data": []}
        except Exception as exc:
            log_event(
                self.logger,
                "optional_client_call_failed",
                level="warning",
                method=method_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {"code": "0", "data": []}
        return payload if isinstance(payload, dict) else {"code": "0", "data": []}

    @staticmethod
    def _merge_payloads(
        payloads: list[dict[str, Any]],
        *,
        context: str = "unknown",
    ) -> dict[str, Any]:
        """Merge OKX paged payloads into {code:"0", data:[...]}.

        2026-04-20 code review A-H1: 之前版本对非 dict 条目 silent skip,
        若 gather 所有 sub-task 都 Exception → 返回 {"code":"0","data":[]},
        下游 reconciliation 把 open_orders 当空列表 → 误判"无未成交单".
        现在 skip 时 log_event (warning), 并通过 `context` 定位调用点.
        """
        merged_data: list[Any] = []
        skipped_non_dict = 0
        for payload in payloads:
            # 防御性: gather(return_exceptions=True) 可能让 payloads 里混入
            # OKXRequestError 等异常对象 (如 OKX 401 / rate limit 时). 这些
            # 对象没有 .get() 方法, 直接跳过避免 AttributeError 让整个
            # build_runtime 崩溃 → execution 进程重启循环.
            # 参见 Phase 1A deploy retrospect (2026-04-20): OKX 401 曾把
            # execution 卡在 crash loop, 新 key 注入前无法优雅降级.
            if not isinstance(payload, dict):
                skipped_non_dict += 1
                continue
            rows = payload.get("data", [])
            if isinstance(rows, list):
                merged_data.extend(rows)

        # A-H1 fix (2026-04-20 code review): 若有 skip, 显式 log 供运维追踪.
        # 避免 "所有 sub-task 都 Exception → merge 返回空 data" 被下游当空列表.
        if skipped_non_dict > 0:
            _module_logger = get_logger("aats.okx_account.merge_payloads")
            log_event(
                _module_logger,
                "okx_merge_payloads_skipped_exception",
                level="warning",
                context=context,
                skipped_non_dict=skipped_non_dict,
                total_payloads=len(payloads),
                merged_rows=len(merged_data),
                hint=(
                    "下游 reconciliation 若看到空 data, 请结合本条 warning 与 "
                    "上游 gather 报错确认是 OKX 失败还是真无数据."
                ),
            )
        return {"code": "0", "data": merged_data}

    @staticmethod
    def _dedupe_open_orders(rows: list[ExchangeOpenOrder]) -> list[ExchangeOpenOrder]:
        deduped: dict[tuple[str, str | None], ExchangeOpenOrder] = {}
        for row in rows:
            deduped[(row.exchange_order_id, row.client_order_id)] = row
        return list(deduped.values())

    @staticmethod
    def _dedupe_fills(rows: list[ExchangeFill]) -> list[ExchangeFill]:
        deduped: dict[str, ExchangeFill] = {}
        for row in rows:
            deduped[row.fill_id] = row
        return list(deduped.values())

    def _parse_balances(self, payload: dict[str, Any]) -> list[ExchangeBalance]:
        rows: list[ExchangeBalance] = []
        for account in payload.get("data", []):
            for detail in account.get("details", []):
                if self.settings.trading_product_type == "derivatives":
                    total = OKXAccountService._balance_value(detail, "cashBal", "eq")
                else:
                    total = OKXAccountService._balance_value(detail, "eq", "cashBal")
                # For spot accounts OKX can report `availEq=0` while `availBal`
                # still carries the real spendable quantity. Prefer the explicit
                # cash balance field when present so simulated submit does not
                # treat funded spot accounts as fully frozen.
                available = OKXAccountService._balance_value(detail, "availBal", "availEq", default=total)
                frozen = max(total - available, Decimal("0"))
                rows.append(
                    ExchangeBalance(
                        currency=str(detail.get("ccy")),
                        total=total,
                        available=available,
                        frozen=frozen,
                    )
                )
        return rows

    def _parse_orders_ws(
        self,
        message: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[ExchangeFill], datetime | None]:
        data = message.get("data", [])
        if not isinstance(data, list):
            return [], [], None
        rows = [dict(item) for item in data if isinstance(item, dict)]
        instrument_map = {
            item.symbol: item
            for item in (self._latest_snapshot.instruments if self._latest_snapshot is not None else [])
        }
        fills = self._parse_fills({"data": rows}, instrument_map=instrument_map)
        update_ts = max((self._row_update_ts(row) for row in rows), default=None)
        return rows, fills, update_ts

    def _current_private_ws_open_orders(
        self,
        *,
        instrument_map: dict[str, InstrumentMetadata],
    ) -> list[ExchangeOpenOrder]:
        live_rows = [
            row
            for row in self._latest_ws_order_rows.values()
            if str(row.get("state") or "").lower() in {"live", "partially_filled"}
        ]
        return self._parse_open_orders({"data": live_rows}, instrument_map=instrument_map)

    @staticmethod
    def _order_row_key(row: dict[str, Any]) -> str | None:
        order_id = row.get("ordId")
        if order_id not in {None, ""}:
            return f"ord:{order_id}"
        client_order_id = row.get("clOrdId")
        if client_order_id not in {None, ""}:
            return f"cl:{client_order_id}"
        return None

    @staticmethod
    def _row_update_ts(row: dict[str, Any]) -> datetime:
        for key in ("uTime", "fillTime", "cTime", "pTime"):
            timestamp = row.get(key)
            if timestamp in {None, ""}:
                continue
            return datetime_from_ms(str(timestamp))
        return utc_now()

    @staticmethod
    def _bill_row_timestamp(row: dict[str, Any]) -> datetime | None:
        for key in ("ts", "billTs", "fillTime"):
            timestamp = row.get(key)
            if timestamp in {None, ""}:
                continue
            return datetime_from_ms(str(timestamp))
        return None

    @staticmethod
    def _bill_row_amount(row: dict[str, Any]) -> Decimal:
        for field in ("balChg", "sz", "amount", "amt", "pnl"):
            value = row.get(field)
            if value not in {None, ""}:
                return to_decimal(value)
        return Decimal("0")

    @staticmethod
    def _is_funding_fee_bill(row: dict[str, Any]) -> bool:
        bill_type = str(row.get("type") or "")
        sub_type = str(row.get("subType") or row.get("sub_type") or "")
        return bill_type == "8" or sub_type in {"173", "174"}

    @staticmethod
    def _balance_value(
        detail: dict[str, Any],
        *keys: str,
        default: Decimal = Decimal("0"),
    ) -> Decimal:
        for key in keys:
            value = detail.get(key)
            if value in {None, ""}:
                continue
            return to_decimal(value)
        return default

    @staticmethod
    def _parse_positions(
        payload: dict[str, Any],
        *,
        instrument_map: dict[str, InstrumentMetadata],
    ) -> list[ExchangePosition]:
        positions: list[ExchangePosition] = []
        for row in payload.get("data", []):
            symbol = str(row.get("instId"))
            instrument = instrument_map.get(symbol)
            inst_type = str(row.get("instType") or "").upper()
            if inst_type == "MARGIN":
                margin_position = OKXAccountService._parse_margin_position_row(row=row, instrument=instrument)
                if margin_position is not None:
                    positions.append(margin_position)
                continue
            positions.append(
                ExchangePosition(
                    instrument_id=symbol,
                    symbol=symbol,
                    quantity=OKXAccountService._exchange_quantity_to_internal(
                        symbol=symbol,
                        quantity=to_decimal(row.get("pos", "0")),
                        instrument_map=instrument_map,
                    ),
                    average_entry_price=(
                        to_decimal(row.get("avgPx")) if row.get("avgPx") not in {None, ""} else None
                    ),
                    mark_price=(to_decimal(row.get("markPx")) if row.get("markPx") not in {None, ""} else None),
                    notional_usd=(to_decimal(row.get("notionalUsd")) if row.get("notionalUsd") not in {None, ""} else None),
                    side=str(row.get("posSide", "net")),
                    margin_mode=OKXAccountService._text_value(row, "mgnMode"),
                    margin_currency=OKXAccountService._text_value(row, "ccy") or (
                        None if instrument is None else instrument.settle_currency
                    ),
                    leverage=OKXAccountService._decimal_value(row, "lever"),
                    margin_allocated=OKXAccountService._decimal_value(row, "margin", "imr"),
                    maintenance_margin=OKXAccountService._decimal_value(row, "mmr"),
                    margin_ratio=OKXAccountService._decimal_value(row, "mgnRatio"),
                    liquidation_price=OKXAccountService._decimal_value(row, "liqPx"),
                    unrealized_pnl=OKXAccountService._decimal_value(row, "upl"),
                    instrument_family=None if instrument is None else instrument.instrument_family,
                    settle_currency=None if instrument is None else instrument.settle_currency,
                )
            )
        return positions

    @staticmethod
    def _parse_margin_position_row(
        *,
        row: dict[str, Any],
        instrument: InstrumentMetadata | None,
    ) -> ExchangePosition | None:
        symbol = str(row.get("instId"))
        base_currency = (
            str(getattr(instrument, "base_currency", "") or "").upper()
            or str(symbol.split("-", 1)[0] if "-" in symbol else "").upper()
        )
        quote_currency = (
            str(getattr(instrument, "quote_currency", "") or "").upper()
            or str(symbol.split("-", 1)[1] if "-" in symbol else "").upper()
        )
        liability = abs(to_decimal(row.get("liab") or row.get("liabAmt") or "0"))
        liability_currency = str(row.get("liabCcy") or "").upper()
        raw_position = to_decimal(row.get("pos") or row.get("baseBal") or row.get("availPos") or "0")
        average_entry_price = to_decimal(row.get("avgPx")) if row.get("avgPx") not in {None, ""} else None
        mark_price = to_decimal(row.get("markPx")) if row.get("markPx") not in {None, ""} else None
        side = "net"
        quantity = Decimal("0")
        if liability > 0 and liability_currency and liability_currency == base_currency:
            side = "short"
            quantity = liability
        elif abs(raw_position) > Decimal("0"):
            quantity = abs(raw_position)
            side = "long" if raw_position > 0 else "short"
        elif liability > 0 and liability_currency and liability_currency == quote_currency and average_entry_price not in {None, Decimal("0")}:
            quantity = liability / average_entry_price
            side = "long"
        if quantity <= Decimal("0"):
            return None
        return ExchangePosition(
            instrument_id=symbol,
            symbol=symbol,
            quantity=quantity,
            average_entry_price=average_entry_price,
            mark_price=mark_price,
            notional_usd=(to_decimal(row.get("notionalUsd")) if row.get("notionalUsd") not in {None, ""} else None),
            side=side,
            margin_mode=OKXAccountService._text_value(row, "mgnMode"),
            margin_currency=OKXAccountService._text_value(row, "ccy", "liabCcy"),
            leverage=OKXAccountService._decimal_value(row, "lever"),
            margin_allocated=OKXAccountService._decimal_value(row, "margin", "imr"),
            maintenance_margin=OKXAccountService._decimal_value(row, "mmr"),
            margin_ratio=OKXAccountService._decimal_value(row, "mgnRatio"),
            liquidation_price=OKXAccountService._decimal_value(row, "liqPx"),
            unrealized_pnl=OKXAccountService._decimal_value(row, "upl"),
            instrument_family=None if instrument is None else instrument.instrument_family,
            settle_currency=None if instrument is None else instrument.settle_currency,
        )

    @staticmethod
    def _parse_open_orders(
        payload: dict[str, Any],
        *,
        instrument_map: dict[str, InstrumentMetadata],
    ) -> list[ExchangeOpenOrder]:
        rows: list[ExchangeOpenOrder] = []
        for row in payload.get("data", []):
            created_ts = row.get("cTime")
            updated_ts = row.get("uTime")
            symbol = str(row.get("instId"))
            rows.append(
                ExchangeOpenOrder(
                    instrument_id=symbol,
                    client_order_id=str(row.get("clOrdId")) if row.get("clOrdId") else None,
                    exchange_order_id=str(row.get("ordId")),
                    side=str(row.get("side")),
                    order_type=str(row.get("ordType")),
                    status=str(row.get("state", "")).upper(),
                    quantity=OKXAccountService._exchange_quantity_to_internal(
                        symbol=symbol,
                        quantity=to_decimal(row.get("sz", "0")),
                        instrument_map=instrument_map,
                    ),
                    filled_quantity=OKXAccountService._exchange_quantity_to_internal(
                        symbol=symbol,
                        quantity=to_decimal(row.get("accFillSz", "0")),
                        instrument_map=instrument_map,
                    ),
                    price=(to_decimal(row.get("px")) if row.get("px") not in {None, ""} else None),
                    created_ts=utc_now() if not created_ts else datetime_from_ms(str(created_ts)),
                    updated_ts=utc_now() if not updated_ts else datetime_from_ms(str(updated_ts)),
                )
            )
        return rows

    @staticmethod
    def _parse_fills(
        payload: dict[str, Any],
        *,
        instrument_map: dict[str, InstrumentMetadata],
    ) -> list[ExchangeFill]:
        rows: list[ExchangeFill] = []
        for row in payload.get("data", []):
            if not OKXAccountService._row_contains_fill(row):
                continue
            fill_ts = row.get("fillTime") or row.get("ts")
            fill_id = str(row.get("tradeId") or row.get("billId") or row.get("fillId") or "")
            if not fill_id:
                fill_id = f"{row.get('ordId', 'unknown')}-{fill_ts or 'unknown'}"
            symbol = str(row.get("instId"))
            fill_qty_value = row.get("fillSz", row.get("sz", "0"))
            fill_price_value = row.get("fillPx", row.get("px", "0"))
            if fill_qty_value in {None, ""} or fill_price_value in {None, ""}:
                continue
            rows.append(
                ExchangeFill(
                    fill_id=fill_id,
                    exchange_order_id=str(row.get("ordId") or ""),
                    client_order_id=str(row.get("clOrdId")) if row.get("clOrdId") else None,
                    instrument_id=symbol,
                    symbol=symbol,
                    side=str(row.get("side")),
                    fill_qty=OKXAccountService._exchange_quantity_to_internal(
                        symbol=symbol,
                        quantity=to_decimal(fill_qty_value),
                        instrument_map=instrument_map,
                    ),
                    fill_price=to_decimal(fill_price_value),
                    fee_amount=-to_decimal(row.get("fillFee", row.get("fee", "0"))),
                    fee_currency=str(row.get("fillFeeCcy") or row.get("feeCcy")) if (row.get("fillFeeCcy") or row.get("feeCcy")) else None,
                    fill_ts=datetime_from_ms(str(fill_ts)) if fill_ts not in {None, ""} else None,
                )
            )
        return rows

    @staticmethod
    def _parse_instruments(payload: dict[str, Any]) -> list[InstrumentMetadata]:
        instruments: list[InstrumentMetadata] = []
        for row in payload.get("data", []):
            base_currency, quote_currency = OKXAccountService._instrument_currencies(row)
            contract_value_raw = row.get("ctVal")
            instruments.append(
                InstrumentMetadata(
                    instrument_id=str(row.get("instId")),
                    symbol=str(row.get("instId")),
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    lot_size=to_decimal(row.get("lotSz", "0.00000001")),
                    tick_size=to_decimal(row.get("tickSz", "0.00000001")),
                    min_size=to_decimal(row.get("minSz", row.get("lotSz", "0"))),
                    contract_value=to_decimal(contract_value_raw if contract_value_raw not in {None, ""} else "1"),
                    instrument_type=OKXAccountService._text_value(row, "instType"),
                    instrument_family=OKXAccountService._text_value(row, "instFamily"),
                    underlying=OKXAccountService._text_value(row, "uly"),
                    settle_currency=OKXAccountService._text_value(row, "settleCcy"),
                    contract_value_currency=OKXAccountService._text_value(row, "ctValCcy"),
                    max_leverage=OKXAccountService._decimal_value(row, "lever"),
                    max_market_size=OKXAccountService._decimal_value(row, "maxMktSz"),
                    max_limit_size=OKXAccountService._decimal_value(row, "maxLmtSz"),
                    list_ts=OKXAccountService._timestamp_from_ms_optional(row.get("listTime")),
                    expiry_ts=OKXAccountService._timestamp_from_ms_optional(row.get("expTime")),
                    state=str(row.get("state", "")),
                    raw=dict(row),
                )
            )
        return instruments

    @staticmethod
    def _exchange_quantity_to_internal(
        *,
        symbol: str,
        quantity: Decimal,
        instrument_map: dict[str, InstrumentMetadata],
    ) -> Decimal:
        return internal_quantity_from_exchange(
            symbol=symbol,
            quantity=quantity,
            instrument=instrument_map.get(symbol),
        )

    @staticmethod
    def _instrument_currencies(row: dict[str, Any]) -> tuple[str, str]:
        base_currency = str(row.get("baseCcy") or "").strip()
        quote_currency = str(row.get("quoteCcy") or "").strip()
        underlying = str(row.get("uly") or "").strip()
        settle_currency = str(row.get("settleCcy") or "").strip()
        contract_value_currency = str(row.get("ctValCcy") or "").strip()
        instrument_id = str(row.get("instId") or "").strip()

        if underlying and "-" in underlying:
            underlying_parts = underlying.split("-")
            if len(underlying_parts) >= 2:
                if not base_currency:
                    base_currency = underlying_parts[0]
                if not quote_currency:
                    quote_currency = underlying_parts[1]

        if not base_currency and contract_value_currency:
            base_currency = contract_value_currency
        if not quote_currency and settle_currency:
            quote_currency = settle_currency

        if instrument_id and "-" in instrument_id:
            symbol_parts = instrument_id.split("-")
            if len(symbol_parts) >= 2:
                if not base_currency:
                    base_currency = symbol_parts[0]
                if not quote_currency:
                    quote_currency = symbol_parts[1]

        return base_currency, quote_currency

    @staticmethod
    def _parse_account_mode(payload: dict[str, Any]) -> str | None:
        configuration = OKXAccountService._parse_account_configuration(payload)
        if configuration is None:
            return None
        return configuration.account_level_code

    @staticmethod
    def _parse_position_mode(payload: dict[str, Any]) -> str | None:
        configuration = OKXAccountService._parse_account_configuration(payload)
        if configuration is None:
            return None
        return configuration.position_mode

    @staticmethod
    def _parse_account_configuration(payload: dict[str, Any]) -> ExchangeAccountConfiguration | None:
        row = OKXAccountService._first_data_row(payload)
        if not row:
            return None
        account_level_code = OKXAccountService._text_value(row, "acctLv")
        position_mode = OKXAccountService._text_value(row, "posMode")
        return ExchangeAccountConfiguration(
            account_level_code=account_level_code,
            account_level_label=OKXAccountService._account_level_label(account_level_code),
            position_mode=position_mode,
            position_mode_label=OKXAccountService._position_mode_label(position_mode),
            auto_loan_enabled=OKXAccountService._boolish(row.get("autoLoan")),
            greeks_type=OKXAccountService._text_value(row, "greeksType"),
            isolated_margin_mode=OKXAccountService._text_value(row, "ctIsoMode", "mgnIsoMode"),
            raw=row,
        )

    @staticmethod
    def _first_data_row(payload: dict[str, Any]) -> dict[str, Any]:
        rows = payload.get("data", [])
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return dict(rows[0])
        return {}

    @staticmethod
    def _parse_fee_rates(payload: dict[str, Any]) -> dict[str, Any]:
        row = OKXAccountService._first_data_row(payload)
        return {
            "maker": row.get("maker"),
            "taker": row.get("taker"),
            "delivery": row.get("delivery"),
            "exercise": row.get("exercise"),
            "source": "okx_trade_fee" if row else "unavailable",
        }

    @staticmethod
    def _parse_fee_schedule(payload: dict[str, Any]) -> ExchangeFeeSchedule | None:
        row = OKXAccountService._first_data_row(payload)
        if not row:
            return None
        return ExchangeFeeSchedule(
            maker=OKXAccountService._decimal_value(row, "maker"),
            taker=OKXAccountService._decimal_value(row, "taker"),
            delivery=OKXAccountService._decimal_value(row, "delivery"),
            exercise=OKXAccountService._decimal_value(row, "exercise"),
            source="okx_trade_fee",
            raw=row,
        )

    @staticmethod
    def _parse_account_risk_snapshot(payload: dict[str, Any]) -> ExchangeAccountRiskSnapshot | None:
        row = OKXAccountService._first_data_row(payload)
        if not row:
            return None
        balance_rows = row.get("balData", [])
        if not isinstance(balance_rows, list):
            balance_rows = []
        position_rows = row.get("posData", [])
        if not isinstance(position_rows, list):
            position_rows = []

        def _sum_rows(rows: list[Any], *keys: str) -> Decimal | None:
            total = Decimal("0")
            seen = False
            for item in rows:
                if not isinstance(item, dict):
                    continue
                value = OKXAccountService._decimal_value(item, *keys)
                if value is None:
                    continue
                total += value
                seen = True
            return total if seen else None

        def _preferred_equity_from_balances(rows: list[Any]) -> Decimal | None:
            discounted = _sum_rows(rows, "disEq")
            if discounted is not None and discounted > Decimal("0"):
                return discounted
            stable_total = Decimal("0")
            stable_seen = False
            for item in rows:
                if not isinstance(item, dict):
                    continue
                currency = str(item.get("ccy", "")).upper()
                if currency not in {"USDT", "USDC", "USD"}:
                    continue
                value = OKXAccountService._decimal_value(item, "eq")
                if value is None:
                    continue
                stable_total += value
                stable_seen = True
            if stable_seen:
                return stable_total
            return _sum_rows(rows, "eq")

        return ExchangeAccountRiskSnapshot(
            adjusted_equity=(
                OKXAccountService._decimal_value(row, "adjEq")
                or _preferred_equity_from_balances(balance_rows)
            ),
            total_equity=(
                OKXAccountService._decimal_value(row, "eq", "totalEq")
                or _preferred_equity_from_balances(balance_rows)
            ),
            available_equity=(
                OKXAccountService._decimal_value(row, "availEq", "availBal")
                or _sum_rows(balance_rows, "availEq", "availBal")
            ),
            initial_margin_requirement=(
                OKXAccountService._decimal_value(row, "imr")
                or _sum_rows(position_rows, "imr", "margin")
            ),
            maintenance_margin_requirement=OKXAccountService._decimal_value(row, "mmr"),
            margin_ratio=OKXAccountService._decimal_value(row, "mgnRatio"),
            notional_usd=(
                OKXAccountService._decimal_value(
                    row,
                    "notionalUsd",
                    "notionalUsdForSwap",
                    "notionalUsdForFutures",
                )
                or _sum_rows(position_rows, "notionalUsd", "notionalUsdForSwap", "notionalUsdForFutures")
            ),
            raw=row,
        )

    @staticmethod
    def _parse_system_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _parse_system_status_items(payload: dict[str, Any]) -> list[ExchangeSystemStatusItem]:
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return []
        items: list[ExchangeSystemStatusItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            items.append(
                ExchangeSystemStatusItem(
                    state=OKXAccountService._text_value(row, "state") or "",
                    service_type=OKXAccountService._text_value(row, "serviceType"),
                    title=OKXAccountService._text_value(row, "title", "serviceName"),
                    description=OKXAccountService._text_value(row, "description", "msg"),
                    begin_ts=OKXAccountService._timestamp_from_ms_optional(row.get("begin")),
                    end_ts=OKXAccountService._timestamp_from_ms_optional(row.get("end")),
                    raw=dict(row),
                )
            )
        return items

    @staticmethod
    def _row_contains_fill(row: dict[str, Any]) -> bool:
        return any(
            row.get(key) not in {None, ""}
            for key in ("tradeId", "billId", "fillId", "fillTime", "fillSz", "fillPx")
        )

    def _parse_balance_and_position_ws(
        self,
        message: dict[str, Any],
    ) -> tuple[list[ExchangeBalance] | None, list[ExchangePosition] | None, datetime | None]:
        rows = message.get("data", [])
        if not isinstance(rows, list) or not rows:
            return None, None, None
        latest = rows[-1] if isinstance(rows[-1], dict) else None
        if latest is None:
            return None, None, None
        balance_rows = latest.get("balData")
        position_rows = latest.get("posData")
        balances = None
        positions = None
        if isinstance(balance_rows, list):
            balances = self._parse_balances({"data": [{"details": balance_rows}]})
        if isinstance(position_rows, list):
            instrument_map = {
                instrument.symbol: instrument
                for instrument in ((self._latest_snapshot.instruments if self._latest_snapshot is not None else []) or [])
            }
            positions = self._parse_positions({"data": position_rows}, instrument_map=instrument_map)
        ts_value = latest.get("pTime") or latest.get("uTime") or latest.get("ts")
        update_ts = datetime_from_ms(str(ts_value)) if ts_value not in {None, ""} else utc_now()
        return balances, positions, update_ts

    def _account_config_blockers(self, snapshot: ExchangeAccountSnapshot) -> list[str]:
        blockers: list[str] = []
        account_level_code = (
            snapshot.account_configuration.account_level_code
            if snapshot.account_configuration is not None
            else snapshot.account_mode
        )
        position_mode = (
            snapshot.account_configuration.position_mode
            if snapshot.account_configuration is not None
            else snapshot.position_mode
        )
        if self.settings.trading_product_type == "derivatives" and account_level_code == "1":
            blockers.append("okx_account_mode_incompatible_with_derivatives")
        if self.settings.trading_product_type == "derivatives" and position_mode in {None, ""}:
            blockers.append("okx_position_mode_missing")
        position_mode_contract = derivatives_position_mode_contract(
            settings=self.settings,
            snapshot=snapshot,
        )
        blocker_code = position_mode_contract.get("blocker_code")
        if blocker_code not in {None, ""}:
            blockers.append(str(blocker_code))
        if self.settings.trading_product_type == "derivatives":
            blockers.extend(self._position_margin_blockers(snapshot))
        return blockers

    def _position_margin_blockers(self, snapshot: ExchangeAccountSnapshot) -> list[str]:
        expected_margin_mode = str(self.settings.margin_mode or "").strip().lower()
        if expected_margin_mode not in {"cross", "isolated"}:
            return []
        for position in snapshot.positions:
            margin_mode = str(getattr(position, "margin_mode", "") or "").strip().lower()
            if not margin_mode:
                continue
            if margin_mode != expected_margin_mode:
                return ["okx_position_margin_mode_conflicts_with_runtime_margin_mode"]
        return []

    @staticmethod
    def _system_status_blockers(snapshot: ExchangeAccountSnapshot) -> list[str]:
        blockers: list[str] = []
        rows = snapshot.system_status_items or [
            ExchangeSystemStatusItem(state=str(row.get("state") or ""), raw=dict(row))
            for row in snapshot.system_status
        ]
        for row in rows:
            state = str(row.state or "").strip().lower()
            if state in {"scheduled", "ongoing"}:
                blockers.append("okx_system_status_incident")
                break
        return blockers

    @staticmethod
    def _decimal_value(row: dict[str, Any], *keys: str) -> Decimal | None:
        for key in keys:
            value = row.get(key)
            if value in {None, ""}:
                continue
            return to_decimal(value)
        return None

    @staticmethod
    def _text_value(row: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = row.get(key)
            if value in {None, ""}:
                continue
            return str(value)
        return None

    @staticmethod
    def _boolish(value: Any) -> bool | None:
        if value in {None, ""}:
            return None
        text = str(value).strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
        return None

    @staticmethod
    def _timestamp_from_ms_optional(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        return datetime_from_ms(str(value))

    @staticmethod
    def _account_level_label(value: str | None) -> str | None:
        mapping = {
            "1": "simple",
            "2": "single_currency_margin",
            "3": "multi_currency_margin",
            "4": "portfolio_margin",
        }
        if value is None:
            return None
        return mapping.get(value, value)

    @staticmethod
    def _position_mode_label(value: str | None) -> str | None:
        mapping = {
            "net_mode": "net",
            "long_short_mode": "long_short",
        }
        if value is None:
            return None
        return mapping.get(value, value)


def datetime_from_ms(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
