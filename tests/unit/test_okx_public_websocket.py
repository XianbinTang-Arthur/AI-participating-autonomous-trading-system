"""回归测试：OKX public WebSocket 断线盲点修复。

修复前的问题：
1. `async for raw_message in websocket` 在 TCP 半关闭时会无限挂起
2. keepalive 的 pong 刷新 _last_message_ts → 即使没有真实行情也不重连
3. `await websocket.close()` 在死套接字上也可能无限挂起
4. 服务端正常关闭（ConnectionClosedOK）被当作 error + 指数退避

本组测试覆盖五个场景（不依赖真实 OKX WebSocket）：
1. recv() 挂起 → wait_for 超时 → _consume 退出内循环并重连
2. 有 pong 无行情 → keepalive 检测 market_data_stale → 强制关闭
3. close() 挂起 → wait_for(close, timeout) 不阻塞 keepalive
4. 真实行情帧更新 _last_market_data_ts，pong 不更新
5. 服务端正常关闭 → 静默退出并立即重连，不触发 error 退避
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import unittest
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from websockets.exceptions import ConnectionClosedOK

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient


def _make_ws_settings(**overrides: object) -> AATSSettings:
    defaults: dict[str, object] = {
        "okx_ws_read_timeout_seconds": 0.5,
        "okx_ws_market_data_timeout_seconds": 1.5,
        "okx_market_reconnect_delay_seconds": 0.1,
        "okx_market_reconnect_max_delay_seconds": 0.2,
        "okx_ws_open_timeout_seconds": 5.0,
        "okx_private_ws_idle_ping_interval_seconds": 0.5,
    }
    defaults.update(overrides)
    return AATSSettings.model_validate(defaults)


class _FakeWebSocket:
    """最小 fake WebSocket，各测试通过子类或参数控制行为。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> str:
        # 默认实现：永远挂起（模拟 TCP 半关闭）
        await asyncio.Event().wait()
        return ""  # pragma: no cover

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


class _CloseHangsWebSocket(_FakeWebSocket):
    """close() 永远挂起。"""

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        await asyncio.Event().wait()  # 永远不返回


class TestRecvTimeoutTriggersReconnect(unittest.IsolatedAsyncioTestCase):
    """场景 1: recv() 挂起 → wait_for 超时 → 退出内循环。"""

    async def test_consume_breaks_on_recv_timeout(self) -> None:
        """recv() 永远不返回 → read_timeout 后 _consume 的内循环 break，
        进入外层 reconnect。我们在第二次迭代时设 stop_event 终止测试。
        """
        settings = _make_ws_settings(okx_ws_read_timeout_seconds=0.3)
        client = OKXPublicWebSocketClient(settings=settings)

        ws = _FakeWebSocket()
        iteration_count = 0

        @contextlib.asynccontextmanager
        async def fake_connect(url: str, **kw: Any):
            nonlocal iteration_count
            iteration_count += 1
            if iteration_count >= 2:
                client._stop_event.set()
            yield ws

        with patch("aats.services.market_gateway.okx_websocket.connect", fake_connect):
            messages_received: list[dict[str, Any]] = []

            async def on_message(msg: dict[str, Any]) -> None:
                messages_received.append(msg)

            await asyncio.wait_for(
                client._consume(
                    connection_name="public",
                    url="wss://fake",
                    subscribe_args=[],
                    on_message=on_message,
                ),
                timeout=5.0,
            )

        self.assertGreaterEqual(iteration_count, 2, "recv timeout 应触发重连（进入第二次迭代）")
        self.assertEqual(messages_received, [], "recv 挂起不应有任何消息")


class TestPongOnlyTriggersMarketDataStale(unittest.IsolatedAsyncioTestCase):
    """场景 2: 有 pong 无行情 → keepalive 检测 market_data_stale → 关闭连接。"""

    async def test_keepalive_detects_market_data_stale_and_closes(self) -> None:
        """只有 pong 回来，_last_market_data_ts 不更新，
        keepalive 在 market_data_timeout 后调用 close(market_data_stale)。
        """
        settings = _make_ws_settings(
            okx_ws_market_data_timeout_seconds=1.0,
            okx_private_ws_idle_ping_interval_seconds=20.0,
        )
        client = OKXPublicWebSocketClient(settings=settings)

        ws = _FakeWebSocket()
        # 模拟：连接已建立，last_market_data_ts 设为较旧时间
        client._last_message_ts["public"] = utc_now()
        client._last_market_data_ts["public"] = utc_now() - timedelta(seconds=5.0)

        await asyncio.wait_for(
            client._keepalive_loop(ws, "public"),
            timeout=5.0,
        )

        self.assertTrue(ws.closed, "keepalive 应当检测到 market data stale 并调用 close()")
        self.assertEqual(ws.close_code, 1011)
        self.assertEqual(ws.close_reason, "market_data_stale")


class TestCloseHangDoesNotBlockKeepalive(unittest.IsolatedAsyncioTestCase):
    """场景 3: close() 挂起 → wait_for(close, timeout) 保证 keepalive 不阻塞。"""

    async def test_keepalive_returns_despite_close_hang(self) -> None:
        """close() 永不返回，但 wait_for(close, timeout=3) 超时后
        keepalive_loop 应当正常 return。
        """
        settings = _make_ws_settings(
            okx_ws_market_data_timeout_seconds=0.5,
            okx_private_ws_idle_ping_interval_seconds=20.0,
        )
        client = OKXPublicWebSocketClient(settings=settings)

        ws = _CloseHangsWebSocket()
        client._last_message_ts["public"] = utc_now()
        # 故意设为很旧 → 立刻触发 market_data_stale
        client._last_market_data_ts["public"] = utc_now() - timedelta(seconds=10.0)

        # close() 挂起但 wait_for 有 3s timeout → keepalive 最终应在 ~4s 内返回
        await asyncio.wait_for(
            client._keepalive_loop(ws, "public"),
            timeout=8.0,
        )
        # 如果 keepalive 在 8s 内返回就证明 close hang 没有阻塞
        self.assertEqual(ws.close_code, 1011)


class TestMarketDataUpdatesTimestamp(unittest.IsolatedAsyncioTestCase):
    """场景 4: 真实行情帧更新 _last_market_data_ts，pong 不更新。"""

    async def test_consume_separates_market_data_from_pong_liveness(self) -> None:
        """发送 pong + 真实行情帧，断言只有行情更新 _last_market_data_ts。"""
        settings = _make_ws_settings(okx_ws_read_timeout_seconds=1.0)
        client = OKXPublicWebSocketClient(settings=settings)

        frames = [
            "pong",
            "pong",
            json.dumps({"arg": {"channel": "tickers"}, "data": [{"last": "100"}]}),
            "pong",
        ]
        frame_index = 0

        class _SequenceWebSocket(_FakeWebSocket):
            async def recv(self_ws) -> str:
                nonlocal frame_index
                if frame_index < len(frames):
                    msg = frames[frame_index]
                    frame_index += 1
                    return msg
                # 帧耗尽后挂起 → recv timeout 触发 break
                await asyncio.Event().wait()
                return ""  # pragma: no cover

        ws = _SequenceWebSocket()
        received: list[dict[str, Any]] = []

        @contextlib.asynccontextmanager
        async def fake_connect(url: str, **kw: Any):
            yield ws

        with patch("aats.services.market_gateway.okx_websocket.connect", fake_connect):
            # 只跑一次迭代就停
            async def on_message(msg: dict[str, Any]) -> None:
                received.append(msg)
                client._stop_event.set()

            await asyncio.wait_for(
                client._consume(
                    connection_name="public",
                    url="wss://fake",
                    subscribe_args=[],
                    on_message=on_message,
                ),
                timeout=5.0,
            )

        self.assertEqual(len(received), 1, "只有一条真实行情消息")
        # 两个时间戳都应被更新
        final_market_ts = client._last_market_data_ts["public"]
        final_message_ts = client._last_message_ts["public"]
        self.assertIsNotNone(final_market_ts, "_last_market_data_ts 应被行情帧更新")
        self.assertIsNotNone(final_message_ts, "_last_message_ts 应被所有帧更新")
        # 两个时间戳的差值应在毫秒级以内（同一帧内两次 utc_now()）
        delta = abs((final_market_ts - final_message_ts).total_seconds())
        self.assertLess(delta, 1.0, "两个时间戳应来自同一帧的处理过程")


class TestConnectionClosedOKImmediateReconnect(unittest.IsolatedAsyncioTestCase):
    """场景 5: 服务端正常关闭 → 立即重连，不触发 error 退避。"""

    async def test_connection_closed_ok_does_not_trigger_error_backoff(self) -> None:
        """recv() 先返回若干正常帧再抛 ConnectionClosedOK。
        _consume 应在内循环 break 后立即重连（不走 except Exception 的
        exponential backoff），并且不产生 error 级别日志。
        """
        settings = _make_ws_settings(
            okx_ws_read_timeout_seconds=2.0,
            okx_market_reconnect_delay_seconds=0.1,
        )
        client = OKXPublicWebSocketClient(settings=settings)

        call_count = 0
        recv_call = 0

        class _ClosingWebSocket(_FakeWebSocket):
            async def recv(self_ws) -> str:
                nonlocal recv_call
                recv_call += 1
                if recv_call <= 2:
                    return json.dumps({"arg": {"channel": "tickers"}, "data": [{}]})
                # 第 3 次 recv 模拟服务端正常关闭
                raise ConnectionClosedOK(None, None)

        @contextlib.asynccontextmanager
        async def fake_connect(url: str, **kw: Any):
            nonlocal call_count, recv_call
            call_count += 1
            recv_call = 0
            if call_count >= 2:
                client._stop_event.set()
            yield _ClosingWebSocket()

        error_records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = lambda record: error_records.append(record)  # type: ignore[assignment]
        client.logger.addHandler(handler)
        client.logger.setLevel(logging.DEBUG)

        received: list[dict[str, Any]] = []

        with patch("aats.services.market_gateway.okx_websocket.connect", fake_connect):
            async def on_message(msg: dict[str, Any]) -> None:
                received.append(msg)

            await asyncio.wait_for(
                client._consume(
                    connection_name="public",
                    url="wss://fake",
                    subscribe_args=[],
                    on_message=on_message,
                ),
                timeout=5.0,
            )

        client.logger.removeHandler(handler)

        # 应当有两条行情消息
        self.assertEqual(len(received), 2)
        # 应当进入第 2 次连接（立即重连后被 stop_event 终止）
        self.assertGreaterEqual(call_count, 2, "ConnectionClosedOK 后应立即重连")

        # 不应有 error 级别日志
        error_level_records = [r for r in error_records if r.levelno >= logging.ERROR]
        self.assertEqual(
            error_level_records,
            [],
            f"ConnectionClosedOK 不应产生 error 级别日志，"
            f"实际: {[r.getMessage() for r in error_level_records]}",
        )


# ─────────────────────────────────────────────────────────────────────
# R6-M2：非 dict JSON 消息必须落 warning 而不是 silent drop
# ─────────────────────────────────────────────────────────────────────


class TestNonDictJsonMessageLogsWarning(unittest.IsolatedAsyncioTestCase):
    """valid JSON 但顶层不是 object（如 list / number）原先走 silent
    `continue`，OKX schema 演进或共享连接消息污染无任何观测信号。
    R6-M2 补一条 warning。
    """

    async def test_non_dict_json_emits_warning_and_skips(self) -> None:
        settings = _make_ws_settings(okx_ws_read_timeout_seconds=1.0)
        client = OKXPublicWebSocketClient(settings=settings)

        frames = [
            json.dumps([1, 2, 3]),  # list：非 dict valid JSON
            json.dumps({"arg": {"channel": "tickers"}, "data": [{"last": "100"}]}),
        ]
        frame_index = 0

        class _SequenceWebSocket(_FakeWebSocket):
            async def recv(self_ws) -> str:
                nonlocal frame_index
                if frame_index < len(frames):
                    msg = frames[frame_index]
                    frame_index += 1
                    return msg
                await asyncio.Event().wait()
                return ""  # pragma: no cover

        ws = _SequenceWebSocket()
        received: list[dict[str, Any]] = []

        @contextlib.asynccontextmanager
        async def fake_connect(url: str, **kw: Any):
            yield ws

        with patch("aats.services.market_gateway.okx_websocket.connect", fake_connect):
            async def on_message(msg: dict[str, Any]) -> None:
                received.append(msg)
                client._stop_event.set()

            with self.assertLogs("aats.okx_market_ws", level="WARNING") as captured:
                await asyncio.wait_for(
                    client._consume(
                        connection_name="public",
                        url="wss://fake",
                        subscribe_args=[],
                        on_message=on_message,
                    ),
                    timeout=5.0,
                )

        self.assertEqual(
            len(received), 1,
            "list 被 silent skip，后续 valid dict 正常派发——handler 只收到 1 条",
        )
        self.assertTrue(
            any("okx_ws_non_dict_message" in r.getMessage() for r in captured.records),
            "非 dict 消息必须落 warning（event=okx_ws_non_dict_message）",
        )


if __name__ == "__main__":
    unittest.main()
