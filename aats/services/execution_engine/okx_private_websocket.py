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
        inst_type = "SWAP" if self.settings.trading_product_type == "derivatives" else "SPOT"
        subscribe_args = [{"channel": "balance_and_position"}, {"channel": "orders", "instType": inst_type}]
        reconnect_delay = self.settings.okx_market_reconnect_delay_seconds
        while not self._stop_event.is_set():
            try:
                async with connect(
                    self._resolved_private_ws_url(),
                    ping_interval=self.settings.okx_ws_ping_interval_seconds,
                    ping_timeout=self.settings.okx_ws_ping_timeout_seconds,
                    open_timeout=self.settings.okx_ws_open_timeout_seconds,
                    close_timeout=5,
                ) as websocket:
                    keepalive_task = asyncio.create_task(self._keepalive_loop(websocket))
                    try:
                        await self._login(websocket)
                        await self._subscribe(websocket, subscribe_args)
                        self._connected = True
                        reconnect_delay = self.settings.okx_market_reconnect_delay_seconds
                        log_event(self.logger, "okx_private_ws_connected", url=self._resolved_private_ws_url())
                        async for raw_message in websocket:
                            if self._stop_event.is_set():
                                break
                            if self._is_pong_message(raw_message):
                                self._last_message_ts = utc_now()
                                continue
                            message = _json_loads(raw_message)
                            if not isinstance(message, dict):
                                continue
                            self._last_message_ts = utc_now()
                            if self._is_control_message(message):
                                continue
                            await on_message(message)
                    finally:
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
        interval = max(5.0, float(self.settings.okx_private_ws_idle_ping_interval_seconds))
        while not self._stop_event.is_set():
            await asyncio.sleep(interval)
            if self._stop_event.is_set():
                return
            await websocket.send("ping")
