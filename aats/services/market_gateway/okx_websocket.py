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


def _subscription_key(arg: dict[str, Any]) -> tuple[str, str]:
    # OKX subscriptions key on different identifiers by channel:
    #   instId      — per-instrument streams (tickers, candles, books)
    #   instType    — type-wide streams (liquidation-orders, mark-price-candle)
    #   instFamily  — family-wide streams (price-limit)
    # The filter is the first non-empty of instId / instFamily / instType;
    # an empty tuple slot means the arg is unroutable and will be skipped by
    # ack tracking (the ack-timeout path will then surface the silent failure).
    channel = str(arg.get("channel", "") or "")
    filter_val = str(
        arg.get("instId")
        or arg.get("instFamily")
        or arg.get("instType")
        or ""
    )
    return channel, filter_val


class OKXWebSocketConsumerBase:
    """Generic OKX public-WebSocket consumer — reconnect, keepalive, ack-timeout.

    Owns the connection-layer machinery shared by multiple OKX consumers
    (market-data client + data-platform liquidations collector):

    * N-connection asyncio.gather orchestration (one task per OKX WS endpoint)
    * Per-connection reconnect with exponential backoff
    * Subscription-ack tracking + silent-failure timeout detection
    * Application-level keepalive (idle ping, pong timeout, market-data-stale)
    * OKX control-plane message classification (subscribe / error / notice)

    Subclasses implement :meth:`_connection_specs` to declare which OKX URLs to
    connect to and which channels to subscribe on each.
    """

    _SUBSCRIPTION_ACK_TIMEOUT_SECONDS: float = 10.0

    def __init__(self, *, settings: AATSSettings, logger_name: str = "aats.okx_ws") -> None:
        self.settings = settings
        self.logger = get_logger(logger_name)
        self._stop_event = asyncio.Event()
        self._connected: dict[str, bool] = {}
        self._last_message_ts: dict[str, datetime | None] = {}
        self._last_market_data_ts: dict[str, datetime | None] = {}
        self._last_error: str | None = None
        # P0-2：订阅 ack 跟踪。_subscribe 发送后填充 pending_subscriptions[conn] =
        # set of (channel, filter) 元组；收到 event="subscribe" ack 时从中移除；
        # 收到 event="error" 时记录到 _subscription_errors[conn]。keepalive 循环
        # 在 _SUBSCRIPTION_ACK_TIMEOUT 秒后若 pending 非空，主动断开重连。
        self._pending_subscriptions: dict[str, set[tuple[str, str]]] = {}
        self._subscription_errors: dict[str, list[dict[str, Any]]] = {}
        self._subscription_sent_ts: dict[str, datetime | None] = {}

    @property
    def stop_event(self) -> asyncio.Event:
        """The event consumers can await to detect shutdown."""
        return self._stop_event

    def _register_connection(self, name: str) -> None:
        """Pre-populate per-connection state so status()/connection_status()
        can be called before :meth:`run_forever` has spun up the tasks.

        Idempotent — calling twice on the same name is safe.
        """
        self._connected.setdefault(name, False)
        self._last_message_ts.setdefault(name, None)
        self._last_market_data_ts.setdefault(name, None)
        self._pending_subscriptions.setdefault(name, set())
        self._subscription_errors.setdefault(name, [])
        self._subscription_sent_ts.setdefault(name, None)

    def connection_status(self, name: str) -> dict[str, Any]:
        """Per-connection observability snapshot. Safe before run_forever()."""
        return {
            "connected": self._connected.get(name, False),
            "last_message_ts": self._last_message_ts.get(name),
            "last_market_data_ts": self._last_market_data_ts.get(name),
            "last_error": self._last_error,
        }

    def _connection_specs(self) -> list[tuple[str, str, list[dict[str, str]]]]:
        """Return a list of ``(connection_name, ws_url, subscribe_args)`` tuples.

        Subclasses override. ``connection_name`` is the key in per-connection
        state dicts and log fields; it is opaque to the base class.
        """
        raise NotImplementedError

    async def run_forever(self, *, on_message: RawMessageHandler) -> None:
        if connect is None:
            raise RuntimeError("websockets_dependency_missing")
        specs = self._connection_specs()
        for name, _, _ in specs:
            self._register_connection(name)
        _gather_results = await asyncio.gather(
            *[
                self._consume(
                    connection_name=name,
                    url=url,
                    subscribe_args=args,
                    on_message=on_message,
                )
                for name, url, args in specs
            ],
            return_exceptions=True,
        )
        for _r in _gather_results:
            if isinstance(_r, Exception):
                self.logger.warning("gather task failed: %s", _r)

    async def stop(self) -> None:
        self._stop_event.set()

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
                            # P1-8：JSON 解析容错。畸形 payload 不应炸掉整个消费循环
                            # 触发无意义重连；记录样本 + 日志，跳过本条。
                            try:
                                message = _json_loads(raw_message)
                            except (ValueError, UnicodeDecodeError) as exc:
                                log_event(
                                    self.logger,
                                    "okx_ws_malformed_message",
                                    level="warning",
                                    connection=connection_name,
                                    error_type=type(exc).__name__,
                                    error=str(exc),
                                    raw_preview=text[:200],
                                )
                                self._last_error = f"json_parse_error:{type(exc).__name__}"
                                continue
                            if not isinstance(message, dict):
                                # R6-M2：非 dict 的 valid JSON（list / str / number
                                # 等）走 silent continue 会让 OKX schema 演进 /
                                # 共享连接消息污染无任何观测信号。此处落
                                # warning 以便下游可查。
                                log_event(
                                    self.logger,
                                    "okx_ws_non_dict_message",
                                    level="warning",
                                    connection=connection_name,
                                    message_type=type(message).__name__,
                                    raw_preview=text[:200],
                                )
                                continue
                            if self._is_control_message(message, connection_name=connection_name):
                                continue
                            # 只有真实行情数据才更新 market data 时间戳；
                            # pong / control 不计入，否则"有 pong 无行情"不会触发重连。
                            self._last_market_data_ts[connection_name] = utc_now()
                            # R2-P1-M7：收到一条真实行情即视为已恢复，把 _last_error
                            # 清掉。否则上一个偶发错误（json_parse_error / subscription_error）
                            # 会永远 stick 在 status() 里，observability 读到"一直有错误"，
                            # 但实际上服务早就恢复。清除只在成功消费时，保证错误与"最近
                            # 一次处理结果"对齐。
                            self._last_error = None
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
            # ── P0-2 订阅 ack 超时检测 ────────────────────────────────
            # subscribe 发送后若在 _SUBSCRIPTION_ACK_TIMEOUT_SECONDS 内未收到
            # 所有 channel 的 event="subscribe" ack，或收到任一 event="error"，
            # 断开重连。静默订阅失败会导致 WS 连着但没行情，下游饿死。
            sub_sent_at = self._subscription_sent_ts.get(connection_name)
            pending = self._pending_subscriptions.get(connection_name, set())
            errors = self._subscription_errors.get(connection_name, [])
            if errors:
                log_event(
                    self.logger,
                    "okx_ws_subscription_failed_reconnect",
                    level="error",
                    connection=connection_name,
                    error_count=len(errors),
                    errors=errors[:5],
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        websocket.close(code=1011, reason="subscription_failed"),
                        timeout=close_wait,
                    )
                return
            if (
                sub_sent_at is not None
                and pending
                and (utc_now() - sub_sent_at).total_seconds() > self._SUBSCRIPTION_ACK_TIMEOUT_SECONDS
            ):
                log_event(
                    self.logger,
                    "okx_ws_subscription_ack_timeout",
                    level="error",
                    connection=connection_name,
                    pending_count=len(pending),
                    pending_sample=[f"{c}:{i}" for c, i in list(pending)[:5]],
                    timeout_seconds=self._SUBSCRIPTION_ACK_TIMEOUT_SECONDS,
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        websocket.close(code=1011, reason="subscription_ack_timeout"),
                        timeout=close_wait,
                    )
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
        # P0-2：记录期望订阅的 (channel, filter) 集合，等待 OKX 返回 event="subscribe"
        # ack 逐个确认。filter 对 instId-based 频道是 instId；对 instType-based 频道
        # （如 liquidation-orders）是 instType，由 _subscription_key 统一归一化。
        # 未在 _SUBSCRIPTION_ACK_TIMEOUT_SECONDS 内确认完的视为静默失败，keepalive
        # 循环会主动断线重连。
        pending: set[tuple[str, str]] = set()
        for arg in subscribe_args:
            key = _subscription_key(arg)
            if key[0] and key[1]:
                pending.add(key)
        self._pending_subscriptions[connection_name] = pending
        self._subscription_errors[connection_name] = []
        self._subscription_sent_ts[connection_name] = utc_now()
        await websocket.send(_json_dumps({"op": "subscribe", "args": subscribe_args}))

    def _is_control_message(self, message: dict[str, Any], *, connection_name: str) -> bool:
        """Return True for OKX control-plane messages (subscribe ack, error, notice).

        Subscription errors are logged so callers can simply skip control messages.
        R3-P0-M1：显式绑定到调用方连接，只操作本连接的 pending/errors，不再跨连接
        遍历 _pending_subscriptions。消息本来就是从 connection_name 对应的 socket 读出
        的，按连接 scope 匹配最严格、也避免未来某天两条连接订阅集出现交集时错配。
        """
        event = message.get("event")
        if not isinstance(event, str):
            return False
        pending = self._pending_subscriptions.setdefault(connection_name, set())
        if event == "subscribe":
            # P0-2：订阅成功 ack。从本连接 pending 集合中移除该 (channel, filter) 组合。
            arg = message.get("arg") or {}
            if isinstance(arg, dict):
                key = _subscription_key(arg)
                if key in pending:
                    pending.discard(key)
                    log_event(
                        self.logger,
                        "okx_ws_subscription_ack",
                        level="debug",
                        connection=connection_name,
                        channel=key[0],
                        filter=key[1],
                        pending_count=len(pending),
                    )
                else:
                    log_event(
                        self.logger,
                        "okx_ws_subscription_ack_unmatched",
                        level="warning",
                        connection=connection_name,
                        channel=key[0],
                        filter=key[1],
                    )
            return True
        if event == "notice":
            return True
        if event == "error":
            code = message.get("code", "")
            msg = message.get("msg", "")
            arg = message.get("arg") or {}
            if isinstance(arg, dict):
                failed_channel, failed_filter = _subscription_key(arg)
            else:
                failed_channel, failed_filter = "", ""
            log_event(
                self.logger,
                "okx_ws_subscription_error",
                level="error",
                connection=connection_name,
                code=code,
                msg=msg,
                connId=message.get("connId", ""),
                channel=failed_channel,
                filter=failed_filter,
            )
            self._last_error = f"subscription_error:{code}:{msg}"
            # R3-P0-M1：错误一定归属本连接 —— 消息来自本连接 recv，不跨连接查 pending。
            key = (failed_channel, failed_filter)
            if key in pending:
                self._subscription_errors.setdefault(connection_name, []).append(
                    {"code": code, "msg": msg, "channel": failed_channel, "filter": failed_filter}
                )
            else:
                log_event(
                    self.logger,
                    "okx_ws_subscription_error_unmatched",
                    level="warning",
                    connection=connection_name,
                    code=code,
                    msg=msg,
                    channel=failed_channel,
                    filter=failed_filter,
                )
            return True
        return False


class OKXPublicWebSocketClient(OKXWebSocketConsumerBase):
    """Market-data client — public (tickers) + business (candles) dual subscription.

    Preserves the historical 2-connection behaviour: one socket on the public WS
    URL for tickers, one on the business WS URL for candle streams. All
    connection-layer concerns (reconnect, keepalive, ack tracking) are inherited
    from :class:`OKXWebSocketConsumerBase`.
    """

    def __init__(self, *, settings: AATSSettings) -> None:
        super().__init__(settings=settings, logger_name="aats.okx_market_ws")
        self._register_connection("public")
        self._register_connection("business")

    def _connection_specs(self) -> list[tuple[str, str, list[dict[str, str]]]]:
        public_args, business_args = self._subscription_args()
        return [
            ("public", self.settings.okx_public_ws_url, public_args),
            ("business", self.settings.okx_business_ws_url, business_args),
        ]

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
        symbols = self._subscribed_symbols()
        public_args: list[dict[str, str]] = []
        for symbol in symbols:
            public_args.append({"channel": "tickers", "instId": symbol})
            # P1.4 Mark price basis 信号 — 仅衍生品（SWAP / FUTURES）有 mark-price.
            # 现货订阅 mark-price 会被 OKX 以 channel 错误拒，触发 subscription_error
            # 路径 → keepalive 断线重连死循环；必须提前按 instId 过滤。
            if self._is_derivative_symbol(symbol):
                public_args.append({"channel": "mark-price", "instId": symbol})
        business_args: list[dict[str, str]] = []
        for symbol in symbols:
            business_args.extend(
                (
                    {"channel": "candle15m", "instId": symbol},
                    {"channel": "candle1H", "instId": symbol},
                )
            )
        return public_args, business_args

    @staticmethod
    def _is_derivative_symbol(symbol: str) -> bool:
        """Return True for OKX derivatives (SWAP perpetual / dated FUTURES).

        SWAP: BASE-QUOTE-SWAP
        FUTURES: BASE-QUOTE-YYMMDD (6 位数字后缀)
        现货 BASE-QUOTE 不具备 mark-price，避免发起无效订阅.
        """
        upper = symbol.upper()
        if upper.endswith("-SWAP"):
            return True
        tail = upper.rsplit("-", 1)[-1]
        return len(tail) == 6 and tail.isdigit()

    def _subscribed_symbols(self) -> tuple[str, ...]:
        symbols = tuple(dict.fromkeys(symbol for symbol in self.settings.expanded_allowed_symbols() if symbol))
        return symbols or (self.settings.default_symbol,)
