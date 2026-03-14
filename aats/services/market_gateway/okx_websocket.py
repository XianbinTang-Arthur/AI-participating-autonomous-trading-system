from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import orjson
from websockets.asyncio.client import ClientConnection, connect

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now


RawMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OKXPublicWebSocketClient:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self.logger = get_logger("aats.okx_market_ws")
        self._stop_event = asyncio.Event()
        self._connected: dict[str, bool] = {"public": False, "business": False}
        self._last_message_ts: dict[str, datetime | None] = {"public": None, "business": None}
        self._last_error: str | None = None

    async def run_forever(self, *, on_message: RawMessageHandler) -> None:
        public_args = [{"channel": "tickers", "instId": self.settings.default_symbol}]
        business_args = [
            {"channel": "candle15m", "instId": self.settings.default_symbol},
            {"channel": "candle1H", "instId": self.settings.default_symbol},
        ]
        await asyncio.gather(
            self._consume(
                connection_name="public",
                url=self.settings.okx_public_ws_url,
                subscribe_args=public_args,
                on_message=on_message,
            ),
            self._consume(
                connection_name="business",
                url=self.settings.okx_business_ws_url,
                subscribe_args=business_args,
                on_message=on_message,
            ),
        )

    async def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict[str, Any]:
        last_message_ts = [
            item
            for item in self._last_message_ts.values()
            if item is not None
        ]
        freshest = max(last_message_ts) if last_message_ts else None
        return {
            "connected_public": self._connected["public"],
            "connected_business": self._connected["business"],
            "connected": all(self._connected.values()),
            "last_message_ts": freshest,
            "last_error": self._last_error,
        }

    async def _consume(
        self,
        *,
        connection_name: str,
        url: str,
        subscribe_args: list[dict[str, str]],
        on_message: RawMessageHandler,
    ) -> None:
        while not self._stop_event.is_set():
            try:
                async with connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as websocket:
                    await self._subscribe(websocket, subscribe_args)
                    self._connected[connection_name] = True
                    log_event(
                        self.logger,
                        "okx_ws_connected",
                        connection=connection_name,
                        url=url,
                    )
                    async for raw_message in websocket:
                        if self._stop_event.is_set():
                            break
                        message = orjson.loads(raw_message)
                        if not isinstance(message, dict):
                            continue
                        self._last_message_ts[connection_name] = utc_now()
                        if self._is_subscription_ack(message):
                            continue
                        await on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._connected[connection_name] = False
                log_event(
                    self.logger,
                    "okx_ws_error",
                    level="error",
                    connection=connection_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                await asyncio.sleep(self.settings.okx_market_reconnect_delay_seconds)
            finally:
                self._connected[connection_name] = False

    async def _subscribe(
        self,
        websocket: ClientConnection,
        subscribe_args: list[dict[str, str]],
    ) -> None:
        await websocket.send(orjson.dumps({"op": "subscribe", "args": subscribe_args}).decode("utf-8"))

    @staticmethod
    def _is_subscription_ack(message: dict[str, Any]) -> bool:
        event = message.get("event")
        return isinstance(event, str) and event in {"subscribe", "notice"}
