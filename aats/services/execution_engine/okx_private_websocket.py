from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
from asyncio import TimeoutError as AsyncTimeoutError
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

try:
    import orjson
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly in environments without orjson.
    orjson = None
try:
    from websockets.asyncio.client import ClientConnection, connect
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly in environments without websockets.
    ClientConnection = Any
    connect = None

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now


RawMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


def _json_loads(raw_message: str | bytes) -> Any:
    if orjson is not None:
        return orjson.loads(raw_message)
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    return json.loads(raw_message)


def _json_dumps(payload: dict[str, Any]) -> str:
    if orjson is not None:
        return orjson.dumps(payload).decode("utf-8")
    return json.dumps(payload, separators=(",", ":"))


class OKXPrivateWebSocketClient:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self.logger = get_logger("aats.okx_private_ws")
        self._stop_event = asyncio.Event()
        self._connected = False
        self._last_message_ts: datetime | None = None
        self._last_error: str | None = None

    async def run_forever(self, *, on_message: RawMessageHandler) -> None:
        if connect is None:
            raise RuntimeError("websockets_dependency_missing")
        subscribe_args = self._subscription_args()
        reconnect_delay = self.settings.okx_market_reconnect_delay_seconds
        while not self._stop_event.is_set():
            keepalive_task: asyncio.Task[None] | None = None
            try:
                # Disable the websockets library's protocol-level PING frames. OKX
                # documents application-level text "ping"/"pong" as the supported
                # keepalive, and the library's PING has been observed to interact badly
                # with OKX (producing 1011 false timeouts on the public WS and failing
                # to prevent OKX's own 4004 "no data in 30s" timeout on the private WS).
                # We use an idle-triggered text ping with response-timeout watchdog below.
                async with connect(
                    self._resolved_private_ws_url(),
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=self.settings.okx_ws_open_timeout_seconds,
                    close_timeout=5,
                ) as websocket:
                    try:
                        await self._login(websocket)
                        await self._subscribe(websocket, subscribe_args)
                        self._connected = True
                        # Reset liveness timestamp so the keepalive does not fire based
                        # on a stale value from the previous connection.
                        self._last_message_ts = utc_now()
                        reconnect_delay = self.settings.okx_market_reconnect_delay_seconds
                        log_event(self.logger, "okx_private_ws_connected", url=self._resolved_private_ws_url())
                        # Start the keepalive task only after login + subscribe so it does
                        # not race with the login handshake.
                        keepalive_task = asyncio.create_task(self._keepalive_loop(websocket))
                        async for raw_message in websocket:
                            if self._stop_event.is_set():
                                break
                            # Any inbound frame (pong, control, or payload) counts as
                            # channel liveness for the keepalive loop.
                            self._last_message_ts = utc_now()
                            if self._is_pong_message(raw_message):
                                continue
                            message = _json_loads(raw_message)
                            if not isinstance(message, dict):
                                continue
                            if self._is_control_message(message):
                                continue
                            await on_message(message)
                    finally:
                        if keepalive_task is not None:
                            keepalive_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await keepalive_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._connected = False
                log_event(
                    self.logger,
                    "okx_private_ws_error",
                    level="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    max(reconnect_delay * 2.0, self.settings.okx_market_reconnect_delay_seconds),
                    self.settings.okx_market_reconnect_max_delay_seconds,
                )
            finally:
                self._connected = False

    async def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "last_message_ts": self._last_message_ts,
            "last_error": self._last_error,
        }

    async def _login(self, websocket: ClientConnection) -> None:
        if not self.settings.okx_credentials_configured:
            raise RuntimeError("OKX credentials are not configured")
        timestamp = str(int(utc_now().timestamp()))
        prehash = f"{timestamp}GET/users/self/verify"
        digest = hmac.new(
            (self.settings.okx_api_secret or "").encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        await websocket.send(
            _json_dumps(
                {
                    "op": "login",
                    "args": [
                        {
                            "apiKey": self.settings.okx_api_key,
                            "passphrase": self.settings.okx_api_passphrase,
                            "timestamp": timestamp,
                            "sign": signature,
                        }
                    ],
                }
            )
        )
        await self._await_login_ack(websocket)

    async def _subscribe(self, websocket: ClientConnection, subscribe_args: list[dict[str, str]]) -> None:
        await websocket.send(_json_dumps({"op": "subscribe", "args": subscribe_args}))

    def _subscription_args(self) -> list[dict[str, str]]:
        return [{"channel": "balance_and_position"}, *self._orders_subscription_args()]

    def _orders_subscription_args(self) -> list[dict[str, str]]:
        inst_types: list[str] = []
        for symbol in self.settings.expanded_allowed_symbols():
            normalized = str(symbol or "").upper()
            if not normalized:
                continue
            if normalized.endswith("-SWAP"):
                if "SWAP" not in inst_types:
                    inst_types.append("SWAP")
                continue
            tail = normalized.rsplit("-", 1)[-1]
            if tail.isdigit():
                if "FUTURES" not in inst_types:
                    inst_types.append("FUTURES")
                continue
            if "SPOT" not in inst_types:
                inst_types.append("SPOT")
        if self.settings.trading_product_type == "derivatives":
            has_derivatives = any(item in {"SWAP", "FUTURES"} for item in inst_types)
            if self.settings.smart_arbitrage_enabled and "SPOT" not in inst_types:
                inst_types.append("SPOT")
            if not has_derivatives:
                inst_types = [item for item in inst_types if item == "SPOT" and self.settings.smart_arbitrage_enabled]
                for item in ("SWAP", "FUTURES"):
                    if item not in inst_types:
                        inst_types.append(item)
        elif not inst_types:
            inst_types = ["SPOT"]
        return [{"channel": "orders", "instType": inst_type} for inst_type in inst_types]

    def _resolved_private_ws_url(self) -> str:
        url = self.settings.okx_private_ws_url
        if not self.settings.okx_simulated_trading:
            return url
        if "wspap.okx.com" in url:
            return url
        return url.replace("ws.okx.com", "wspap.okx.com")

    async def _await_login_ack(self, websocket: ClientConnection) -> None:
        try:
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=max(self.settings.okx_timeout_seconds, 5.0))
        except AsyncTimeoutError as exc:
            raise RuntimeError("okx_private_ws_login_timeout") from exc
        message = _json_loads(raw_message)
        if not isinstance(message, dict):
            raise RuntimeError("okx_private_ws_login_invalid_response")
        if str(message.get("event") or "") != "login":
            if str(message.get("event") or "") == "error":
                raise RuntimeError(
                    f"okx_private_ws_login_error code={message.get('code')} msg={message.get('msg')}"
                )
            raise RuntimeError(f"okx_private_ws_login_unexpected_event {message.get('event')}")
        if str(message.get("code") or "0") != "0":
            raise RuntimeError(
                f"okx_private_ws_login_error code={message.get('code')} msg={message.get('msg')}"
            )
        self._last_message_ts = utc_now()

    @staticmethod
    def _is_control_message(message: dict[str, Any]) -> bool:
        event = message.get("event")
        return isinstance(event, str) and event in {"subscribe", "notice", "login"}

    @staticmethod
    def _is_pong_message(raw_message: str | bytes) -> bool:
        if isinstance(raw_message, bytes):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return False
        return str(raw_message).strip().lower() == "pong"

    async def _keepalive_loop(self, websocket: ClientConnection) -> None:
        # Idle-triggered keepalive per OKX spec. OKX enforces a 30s no-data close
        # (code 4004) on the private channel, so the steady-state gap between our
        # outbound frames must stay well below 30s. We cap idle_threshold at 15s
        # (so even a user misconfiguration cannot push it into the danger zone) and
        # use a 1s poll interval to minimize jitter from event-loop stalls. The
        # worst-case gap between pings is roughly idle_threshold + poll_interval
        # (~16s), leaving ~14s of safety margin against OKX's 30s deadline.
        configured_idle = float(self.settings.okx_private_ws_idle_ping_interval_seconds)
        idle_threshold = min(max(5.0, configured_idle), 15.0)
        pong_timeout = 10.0
        poll_interval = 1.0
        while not self._stop_event.is_set():
            await asyncio.sleep(poll_interval)
            if self._stop_event.is_set():
                return
            last_ts = self._last_message_ts
            if last_ts is not None:
                idle = (utc_now() - last_ts).total_seconds()
                if idle < idle_threshold:
                    continue
            ping_sent_at = utc_now()
            try:
                await websocket.send("ping")
            except Exception:
                return
            # Wait up to pong_timeout for any inbound traffic (pong or push data).
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
                latest = self._last_message_ts
                if latest is not None and latest > ping_sent_at:
                    break
                if (utc_now() - ping_sent_at).total_seconds() >= pong_timeout:
                    log_event(
                        self.logger,
                        "okx_private_ws_pong_timeout",
                        level="warning",
                        idle_threshold=idle_threshold,
                        pong_timeout=pong_timeout,
                    )
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1011, reason="application_ping_timeout")
                    return
