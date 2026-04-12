from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
import json
from typing import Any

try:
    import orjson
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly in environments without orjson.
    orjson = None
try:
    from websockets.asyncio.client import ClientConnection, connect
    from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly in environments without websockets.
    ClientConnection = Any
    connect = None
    ConnectionClosed = Exception
    ConnectionClosedOK = Exception

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


class OKXPublicWebSocketClient:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self.logger = get_logger("aats.okx_market_ws")
        self._stop_event = asyncio.Event()
        self._connected: dict[str, bool] = {"public": False, "business": False}
        self._last_message_ts: dict[str, datetime | None] = {"public": None, "business": None}
        self._last_market_data_ts: dict[str, datetime | None] = {"public": None, "business": None}
        self._last_error: str | None = None

    async def run_forever(self, *, on_message: RawMessageHandler) -> None:
        if connect is None:
            raise RuntimeError("websockets_dependency_missing")
        public_args, business_args = self._subscription_args()
        _gather_results = await asyncio.gather(
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
            return_exceptions=True,
        )
        for _r in _gather_results:
            if isinstance(_r, Exception):
                self.logger.warning("gather task failed: %s", _r)

    async def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict[str, Any]:
        last_message_ts = [
            item
            for item in self._last_message_ts.values()
            if item is not None
        ]
        freshest = max(last_message_ts) if last_message_ts else None
        last_market = [
            item
            for item in self._last_market_data_ts.values()
            if item is not None
        ]
        freshest_market = max(last_market) if last_market else None
        return {
            "connected_public": self._connected["public"],
            "connected_business": self._connected["business"],
            "connected": all(self._connected.values()),
            "last_message_ts": freshest,
            "last_market_data_ts": freshest_market,
            "last_error": self._last_error,
            "subscribed_symbols": list(self._subscribed_symbols()),
        }

    def _subscription_args(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        public_args = [{"channel": "tickers", "instId": symbol} for symbol in self._subscribed_symbols()]
        business_args: list[dict[str, str]] = []
        for symbol in self._subscribed_symbols():
            business_args.extend(
                (
                    {"channel": "candle15m", "instId": symbol},
                    {"channel": "candle1H", "instId": symbol},
                )
            )
        return public_args, business_args

    def _subscribed_symbols(self) -> tuple[str, ...]:
        symbols = tuple(dict.fromkeys(symbol for symbol in self.settings.expanded_allowed_symbols() if symbol))
        return symbols or (self.settings.default_symbol,)

    async def _consume(
        self,
        *,
        connection_name: str,
        url: str,
        subscribe_args: list[dict[str, str]],
        on_message: RawMessageHandler,
    ) -> None:
        reconnect_delay = self.settings.okx_market_reconnect_delay_seconds
        while not self._stop_event.is_set():
            try:
                # Disable the websockets library's protocol-level PING frames. OKX documents
                # application-level text "ping"/"pong" as the supported keepalive; the
                # protocol-level PING has been observed to false-positive with a 1011
                # "keepalive ping timeout" despite a healthy channel. We rely on our own
                # idle-triggered keepalive below instead.
                async with connect(
                    url,
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=self.settings.okx_ws_open_timeout_seconds,
                    close_timeout=5,
                ) as websocket:
                    await self._subscribe(websocket, connection_name, subscribe_args)
                    self._connected[connection_name] = True
                    # Reset the liveness timestamps so the keepalive loop does not fire
                    # immediately based on stale values from the previous connection.
                    now = utc_now()
                    self._last_message_ts[connection_name] = now
                    self._last_market_data_ts[connection_name] = now
                    reconnect_delay = self.settings.okx_market_reconnect_delay_seconds
                    read_timeout = self.settings.okx_ws_read_timeout_seconds
                    log_event(
                        self.logger,
                        "okx_ws_connected",
                        connection=connection_name,
                        url=url,
                    )
                    keepalive_task = asyncio.create_task(
                        self._keepalive_loop(websocket, connection_name)
                    )
                    try:
                        # 不使用 `async for raw_message in websocket`：当 TCP 半关闭时，
                        # async for 会无限挂起。显式 wait_for(recv()) 加硬超时，保证
                        # 即使底层连接死亡也能在 read_timeout 内退出并重连。
                        while not self._stop_event.is_set():
                            try:
                                raw_message = await asyncio.wait_for(
                                    websocket.recv(),
                                    timeout=read_timeout,
                                )
                            except TimeoutError:
                                log_event(
                                    self.logger,
                                    "okx_ws_read_timeout",
                                    level="warning",
                                    connection=connection_name,
                                    timeout=read_timeout,
                                )
                                break
                            except ConnectionClosedOK:
                                # 服务端正常关闭（维护、idle 超时等），
                                # 静默退出内循环，外层立即重连。
                                log_event(
                                    self.logger,
                                    "okx_ws_closed_ok",
                                    level="info",
                                    connection=connection_name,
                                )
                                break
                            except ConnectionClosed as exc:
                                # 协议错误 / 网络故障 / keepalive 驱动的关闭。
                                # 退出内循环走外层重连，但记一条 warning。
                                log_event(
                                    self.logger,
                                    "okx_ws_closed_error",
                                    level="warning",
                                    connection=connection_name,
                                    error=str(exc),
                                )
                                break
                            text = raw_message if isinstance(raw_message, str) else raw_message.decode("utf-8", errors="replace")
                            # Any inbound frame (including "pong" and control messages)
                            # counts as channel liveness for the keepalive loop.
                            self._last_message_ts[connection_name] = utc_now()
                            if text.strip().lower() == "pong":
                                continue
                            message = _json_loads(raw_message)
                            if not isinstance(message, dict):
                                continue
                            if self._is_control_message(message):
                                continue
                            # 只有真实行情数据才更新 market data 时间戳；
                            # pong / control 不计入，否则"有 pong 无行情"不会触发重连。
                            self._last_market_data_ts[connection_name] = utc_now()
                            await on_message(message)
                    finally:
                        keepalive_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await keepalive_task
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
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    max(reconnect_delay * 2.0, self.settings.okx_market_reconnect_delay_seconds),
                    self.settings.okx_market_reconnect_max_delay_seconds,
                )
            finally:
                self._connected[connection_name] = False

    async def _keepalive_loop(self, websocket: ClientConnection, connection_name: str) -> None:
        # Idle-triggered keepalive per OKX spec. OKX enforces a 30s no-data close on
        # idle channels, so idle_threshold is capped at 15s and poll_interval is
        # fixed at 1s to guarantee a comfortable safety margin regardless of how the
        # user has tuned the idle-ping setting. See the private WS client for the
        # full rationale; both connections share the same hard constraint.
        configured_idle = float(self.settings.okx_private_ws_idle_ping_interval_seconds)
        idle_threshold = min(max(5.0, configured_idle), 15.0)
        pong_timeout = 10.0
        poll_interval = 1.0
        market_data_timeout = float(self.settings.okx_ws_market_data_timeout_seconds)
        close_wait = 3.0
        while not self._stop_event.is_set():
            await asyncio.sleep(poll_interval)
            if self._stop_event.is_set():
                return
            # ── 行情数据过期检测 ──────────────────────────────────────
            # 即使 pong 持续回来（channel 活着），如果没有真实行情数据也应重连：
            # OKX 可能静默丢失订阅或 server-side 故障只保持 keepalive。
            last_market = self._last_market_data_ts.get(connection_name)
            if last_market is not None:
                market_idle = (utc_now() - last_market).total_seconds()
                if market_idle >= market_data_timeout:
                    log_event(
                        self.logger,
                        "okx_ws_market_data_stale",
                        level="warning",
                        connection=connection_name,
                        market_idle_seconds=market_idle,
                        market_data_timeout=market_data_timeout,
                    )
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            websocket.close(code=1011, reason="market_data_stale"),
                            timeout=close_wait,
                        )
                    return
            last_ts = self._last_message_ts.get(connection_name)
            if last_ts is not None:
                idle = (utc_now() - last_ts).total_seconds()
                if idle < idle_threshold:
                    continue
            ping_sent_at = utc_now()
            try:
                await websocket.send("ping")
            except Exception:
                return
            # Wait up to pong_timeout for any inbound traffic (pong or market data).
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
                latest = self._last_message_ts.get(connection_name)
                if latest is not None and latest > ping_sent_at:
                    break
                if (utc_now() - ping_sent_at).total_seconds() >= pong_timeout:
                    log_event(
                        self.logger,
                        "okx_ws_pong_timeout",
                        level="warning",
                        connection=connection_name,
                        idle_threshold=idle_threshold,
                        pong_timeout=pong_timeout,
                    )
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            websocket.close(code=1011, reason="application_ping_timeout"),
                            timeout=close_wait,
                        )
                    return

    async def _subscribe(
        self,
        websocket: ClientConnection,
        connection_name: str,
        subscribe_args: list[dict[str, str]],
    ) -> None:
        await websocket.send(_json_dumps({"op": "subscribe", "args": subscribe_args}))

    def _is_control_message(self, message: dict[str, Any]) -> bool:
        """Return True for OKX control-plane messages (subscribe ack, error, notice).

        Subscription errors are logged so callers can simply skip control messages.
        """
        event = message.get("event")
        if not isinstance(event, str):
            return False
        if event in {"subscribe", "notice"}:
            return True
        if event == "error":
            code = message.get("code", "")
            msg = message.get("msg", "")
            log_event(
                self.logger,
                "okx_ws_subscription_error",
                level="error",
                code=code,
                msg=msg,
                connId=message.get("connId", ""),
            )
            self._last_error = f"subscription_error:{code}:{msg}"
            return True
        return False
