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
    # R3-P1-U-D：订阅 ack 等待上限。超时 → 抛错 → 外层 run_forever 的重连逻辑会拆连接重来。
    # 和 public WS 侧 _SUBSCRIPTION_ACK_TIMEOUT_SECONDS 保持同量级，避免两侧认知不一致。
    _SUBSCRIPTION_ACK_TIMEOUT_SECONDS: float = 10.0

    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings
        self.logger = get_logger("aats.okx_private_ws")
        self._stop_event = asyncio.Event()
        self._connected = False
        self._last_message_ts: datetime | None = None
        self._last_error: str | None = None
        # R3-P1-U-D：private WS 订阅 ack 追踪。镜像 P0-M1 public WS 语义。
        # 原实现把 `{"op": "subscribe", ...}` 发出后就直接进 for-recv 循环，
        # OKX 对 balance_and_position / orders 返回的 `event="subscribe"` ack 或
        # `event="error"` 都被 _is_control_message 当作普通 control 消息跳过，
        # 上层对 "订阅成功" vs "订阅被拒" 完全没有可视度。如果比如传了非法
        # instType、或者权限不足被拒，websocket 本身还是连通的（心跳还在走），
        # 但 balance_and_position / orders 永远没有数据推来——execution 进程的
        # 账户/订单视图彻底静默失联。
        # 修复：用一个 pending set 记录期望 ack 的 (channel, instType) 二元组，
        # 收 event="subscribe" ack 从中移除，收 event="error" 时记录并告警；
        # _subscribe 在发送后用 asyncio.wait_for 等待 pending 清空，超时即抛错
        # 让 run_forever 外层 except 分支走重连。
        self._pending_subscriptions: set[tuple[str, str]] = set()
        self._subscription_errors: list[dict[str, Any]] = []
        self._subscription_sent_ts: datetime | None = None

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

                            # ── A1 · 2026-04-21 keepalive task 静默死监控 ──
                            # 每条消息后快检 task 状态；发现它已 done 就抛进外层
                            # 重连路径（见 _assert_keepalive_alive docstring）。
                            self._assert_keepalive_alive(keepalive_task)

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

    @staticmethod
    def _assert_keepalive_alive(keepalive_task: "asyncio.Task[None]") -> None:
        """2026-04-21 A1 · keepalive task 静默死检测。

        `asyncio.create_task()` 会把内部异常静默存入 task；如果没人 await，
        异常永远不冒出来。旧实现只在 finally 里 await keepalive_task，但主
        循环退出时 task 往往已经死了很久——期间 keepalive ping 停发，OKX
        30s 后 side-close 连接，我们收不到 fill / balance 更新。

        新法：主循环每条消息后调这个 helper 快检 task 状态。发现 done
        就立刻 raise 进外层 except 路径，让 reconnect 链路重起一个新
        keepalive_task 恢复心跳。

        `_keepalive_loop` 设计是无限循环到被 cancel，所以 **正常情况下
        task.done() 永远是 False**。done 必然意味着出问题（异常 or 提前
        return）——两种都视为 fatal，抛出触发重连。
        """
        if not keepalive_task.done():
            return
        # Cancelled task 的特殊形态：task.exception() 会 raise CancelledError
        # 而不是 return。单独处理避免 watchdog 自己崩掉。
        if keepalive_task.cancelled():
            raise RuntimeError(
                "keepalive_task was cancelled "
                "(should only happen on stop / reconnect)"
            )
        keepalive_exc = keepalive_task.exception()
        if keepalive_exc is not None:
            raise RuntimeError(
                f"keepalive_task died: "
                f"{type(keepalive_exc).__name__}: {keepalive_exc}"
            ) from keepalive_exc
        raise RuntimeError(
            "keepalive_task completed unexpectedly "
            "(should run until cancelled)"
        )

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
        # R3-P1-U-D：发送 subscribe 前先把期望 ack 的 (channel, instType) 集合注册进
        # pending set。OKX 对 balance_and_position 返回的 ack arg 里没有 instType，
        # 用空字符串占位；orders 的 ack arg 会带 instType。配合 _is_control_message
        # 收到 event="subscribe" 后 discard 对应 key，_keepalive_loop 负责超时兜底。
        pending: set[tuple[str, str]] = set()
        for arg in subscribe_args:
            channel = str(arg.get("channel", ""))
            inst_type = str(arg.get("instType", ""))
            if channel:
                pending.add((channel, inst_type))
        self._pending_subscriptions = pending
        self._subscription_errors = []
        self._subscription_sent_ts = utc_now()
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

    def _is_control_message(self, message: dict[str, Any]) -> bool:
        # R3-P1-U-D：除了把 event="subscribe"/"notice"/"login" 识别为控制消息以跳过
        # 下游 on_message 路由外，还负责把订阅 ack 从 _pending_subscriptions 里抹掉、
        # 把订阅 error 累积进 _subscription_errors，供 _keepalive_loop 做超时/失败
        # 兜断连。与 public WS 的 _is_control_message 语义对齐（只是 private 只有
        # 一条连接、不需要 connection_name 维度）。
        event = message.get("event")
        if not isinstance(event, str):
            return False
        if event == "subscribe":
            arg = message.get("arg") or {}
            if isinstance(arg, dict):
                key = (str(arg.get("channel", "")), str(arg.get("instType", "")))
                if key in self._pending_subscriptions:
                    self._pending_subscriptions.discard(key)
                    log_event(
                        self.logger,
                        "okx_private_ws_subscription_ack",
                        level="debug",
                        channel=key[0],
                        instType=key[1],
                        pending_count=len(self._pending_subscriptions),
                    )
                else:
                    log_event(
                        self.logger,
                        "okx_private_ws_subscription_ack_unmatched",
                        level="warning",
                        channel=key[0],
                        instType=key[1],
                    )
            return True
        if event == "notice":
            return True
        if event == "login":
            return True
        if event == "error":
            code = message.get("code", "")
            msg = message.get("msg", "")
            arg = message.get("arg") or {}
            failed_channel = str(arg.get("channel", "")) if isinstance(arg, dict) else ""
            failed_inst_type = str(arg.get("instType", "")) if isinstance(arg, dict) else ""
            log_event(
                self.logger,
                "okx_private_ws_subscription_error",
                level="error",
                code=code,
                msg=msg,
                channel=failed_channel,
                instType=failed_inst_type,
            )
            self._last_error = f"subscription_error:{code}:{msg}"
            key = (failed_channel, failed_inst_type)
            if key in self._pending_subscriptions:
                self._subscription_errors.append(
                    {"code": code, "msg": msg, "channel": failed_channel, "instType": failed_inst_type}
                )
            else:
                log_event(
                    self.logger,
                    "okx_private_ws_subscription_error_unmatched",
                    level="warning",
                    code=code,
                    msg=msg,
                    channel=failed_channel,
                    instType=failed_inst_type,
                )
            return True
        return False

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
        close_wait = 3.0
        while not self._stop_event.is_set():
            await asyncio.sleep(poll_interval)
            if self._stop_event.is_set():
                return
            # R3-P1-U-D：订阅 ack 超时/失败检测。subscribe 发送后若在
            # _SUBSCRIPTION_ACK_TIMEOUT_SECONDS 内未收齐所有 channel 的 ack，或收到
            # 任一 event="error"，立即断连重连。静默订阅失败会让 WS 连着但没有
            # balance/orders 推送，下游账户视图永久饿死。
            if self._subscription_errors:
                log_event(
                    self.logger,
                    "okx_private_ws_subscription_failed_reconnect",
                    level="error",
                    error_count=len(self._subscription_errors),
                    errors=self._subscription_errors[:5],
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        websocket.close(code=1011, reason="subscription_failed"),
                        timeout=close_wait,
                    )
                return
            if (
                self._subscription_sent_ts is not None
                and self._pending_subscriptions
                and (utc_now() - self._subscription_sent_ts).total_seconds()
                > self._SUBSCRIPTION_ACK_TIMEOUT_SECONDS
            ):
                log_event(
                    self.logger,
                    "okx_private_ws_subscription_ack_timeout",
                    level="error",
                    pending_count=len(self._pending_subscriptions),
                    pending_sample=[f"{c}:{i}" for c, i in list(self._pending_subscriptions)[:5]],
                    timeout_seconds=self._SUBSCRIPTION_ACK_TIMEOUT_SECONDS,
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        websocket.close(code=1011, reason="subscription_ack_timeout"),
                        timeout=close_wait,
                    )
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
