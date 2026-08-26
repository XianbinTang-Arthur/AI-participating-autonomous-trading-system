"""P1-D Phase 1A Stage 2 单元测试 — MicrostructureBronzeBuffer + 限流 + flush。

覆盖 §9 Day 2/4 验收点:
  1. Buffer add 在达到 flush_max_rows 时返回 True (触发 flush)
  2. Buffer drain swap-and-release: 锁内只交换,DB I/O 在锁外
  3. Buffer 定时 flush (_periodic_flush_for 超时触发)
  4. Buffer hard-cap 防 OOM: 超 5000 条自动丢最旧一半 + critical log
  5. Throttle: bbo 1Hz 抑制 >1 sample/s, books5 2Hz 抑制 >2 sample/s
  6. Collector _handle_message 按 channel 分派到对应 buffer
  7. Flush 错误不上抛 (drop batch, 继续消费)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.data_platform.collectors.microstructure_ws_collector import (
    BboRow,
    Books5Row,
    MicrostructureBronzeBuffer,
    MicrostructureCollector,
    OiFundingMarkRow,
    TradeRow,
    _BUFFER_HARD_CAP,
    _continuity_scopes,
)


def _ws_settings(**overrides: object) -> AATSSettings:
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


_TS = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def _make_trade_row(tid: str = "T-1") -> TradeRow:
    return TradeRow(
        symbol="BTC-USDT-SWAP",
        ts=_TS,
        trade_id=tid,
        px=Decimal("95000"),
        sz=Decimal("0.1"),
        side="buy",
        raw_payload={"tradeId": tid},
    )


def _make_bbo_row() -> BboRow:
    return BboRow(
        symbol="BTC-USDT-SWAP",
        ts=_TS,
        source_ts=_TS,
        bid_px=Decimal("95000"), bid_sz=Decimal("1"),
        ask_px=Decimal("95010"), ask_sz=Decimal("2"),
    )


def _make_books5_row() -> Books5Row:
    return Books5Row(
        symbol="BTC-USDT-SWAP",
        ts=_TS, source_ts=_TS,
        bid_px_1=Decimal("95000"), bid_sz_1=Decimal("1"),
        bid_px_2=None, bid_sz_2=None,
        bid_px_3=None, bid_sz_3=None,
        bid_px_4=None, bid_sz_4=None,
        bid_px_5=None, bid_sz_5=None,
        ask_px_1=Decimal("95010"), ask_sz_1=Decimal("2"),
        ask_px_2=None, ask_sz_2=None,
        ask_px_3=None, ask_sz_3=None,
        ask_px_4=None, ask_sz_4=None,
        ask_px_5=None, ask_sz_5=None,
    )


def _make_oif_row(tick_type: str = "mark") -> OiFundingMarkRow:
    return OiFundingMarkRow(
        ts=_TS,
        symbol="BTC-USDT-SWAP",
        tick_type=tick_type,
        mark_px=Decimal("95000") if tick_type == "mark" else None,
    )


# =====================================================================
# Case 1: Buffer add threshold
# =====================================================================


class TestBufferThreshold(unittest.IsolatedAsyncioTestCase):
    async def test_add_returns_true_at_max_rows(self) -> None:
        buf = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=3,
            flush_max_seconds=60.0,
        )
        self.assertFalse(await buf.add(_make_trade_row("T-1")))
        self.assertFalse(await buf.add(_make_trade_row("T-2")))
        # 第 3 行刚好达到阈值
        self.assertTrue(await buf.add(_make_trade_row("T-3")))

    async def test_add_many_returns_true_when_crossing_threshold(self) -> None:
        buf = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=5,
            flush_max_seconds=60.0,
        )
        self.assertFalse(await buf.add_many([_make_trade_row(f"T-{i}") for i in range(3)]))
        self.assertTrue(await buf.add_many([_make_trade_row(f"T-{i}") for i in range(3, 6)]))

    async def test_oversized_batch_cannot_exceed_hard_cap(self) -> None:
        buf = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=_BUFFER_HARD_CAP + 1,
            flush_max_seconds=60.0,
        )
        await buf.add_many(
            [_make_trade_row(f"T-{index}") for index in range(_BUFFER_HARD_CAP + 1000)]
        )

        self.assertLessEqual(buf.buffered(), _BUFFER_HARD_CAP)
        self.assertGreater(buf.rows_dropped_total, 0)


def test_oif_continuity_scopes_only_include_actual_tick_types() -> None:
    scopes = _continuity_scopes(
        "staging.market_oi_funding_ticks",
        [_make_oif_row("mark"), _make_oif_row("oi")],
    )

    assert scopes == (
        ("mark-price", ("BTC-USDT-SWAP",)),
        ("open-interest", ("BTC-USDT-SWAP",)),
    )

    async def test_drain_returns_rows_and_empties_buffer(self) -> None:
        buf = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=100,
            flush_max_seconds=60.0,
        )
        for i in range(3):
            await buf.add(_make_trade_row(f"T-{i}"))
        drained = await buf.drain()
        self.assertEqual(len(drained), 3)
        self.assertEqual(buf.buffered(), 0)
        # 第二次 drain 在空 buffer 上应返回空 list
        self.assertEqual(await buf.drain(), [])


# =====================================================================
# Case 2: Buffer rejects bad params
# =====================================================================


class TestBufferValidation(unittest.TestCase):
    def test_zero_flush_max_rows_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MicrostructureBronzeBuffer(
                table="x", flush_max_rows=0, flush_max_seconds=1.0,
            )

    def test_non_positive_flush_seconds_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MicrostructureBronzeBuffer(
                table="x", flush_max_rows=10, flush_max_seconds=0.0,
            )


# =====================================================================
# Case 3: Hard cap 防 OOM
# =====================================================================


class TestBufferHardCap(unittest.IsolatedAsyncioTestCase):
    async def test_hard_cap_drops_oldest_half(self) -> None:
        """单次 add 触发 hard cap 时,丢最老一半再追加新行。"""
        buf = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=_BUFFER_HARD_CAP * 2,   # 调大让 flush_max_rows 不先触发
            flush_max_seconds=60.0,
        )
        # 先塞 _BUFFER_HARD_CAP 行
        for i in range(_BUFFER_HARD_CAP):
            await buf.add(_make_trade_row(f"T-{i}"))
        self.assertEqual(buf.buffered(), _BUFFER_HARD_CAP)
        # 第 _BUFFER_HARD_CAP+1 行触发 hard cap,应丢一半后再追加
        with self.assertLogs(
            "aats.data_platform.collectors.microstructure_ws_collector",
            level=logging.CRITICAL,
        ):
            await buf.add(_make_trade_row("T-over"))
        expected = _BUFFER_HARD_CAP - (_BUFFER_HARD_CAP // 2) + 1
        self.assertEqual(buf.buffered(), expected)

    async def test_add_many_hard_cap(self) -> None:
        buf = MicrostructureBronzeBuffer(
            table="bronze.market_trades",
            flush_max_rows=_BUFFER_HARD_CAP * 2,
            flush_max_seconds=60.0,
        )
        for i in range(_BUFFER_HARD_CAP - 10):
            await buf.add(_make_trade_row(f"T-{i}"))
        # bulk add 20 条跨越 hard cap
        with self.assertLogs(
            "aats.data_platform.collectors.microstructure_ws_collector",
            level=logging.CRITICAL,
        ):
            await buf.add_many([_make_trade_row(f"N-{i}") for i in range(20)])
        self.assertLess(buf.buffered(), _BUFFER_HARD_CAP + 20)


# =====================================================================
# Case 4: Throttle — bbo 1Hz 采样
# =====================================================================


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _CapturingSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, stmt, params):  # noqa: ANN001
        sql = str(stmt).strip()
        batch = list(params) if not isinstance(params, dict) else [params]
        self.executed.append((sql, batch))
        return _FakeResult(rowcount=len(batch))

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class TestThrottleBbo(unittest.IsolatedAsyncioTestCase):
    """bbo 1 Hz 客户端限流: 同一秒内多条 bbo push 应只采样 1 条。"""

    async def test_bbo_throttle_suppresses_rapid_samples(self) -> None:
        collector = MicrostructureCollector(
            settings=_ws_settings(),
            bbo_min_interval_seconds=10.0,   # 夸张值避开 wall-clock 抖动
            books5_min_interval_seconds=0.0,
        )
        rows = [_make_bbo_row() for _ in range(5)]
        throttled = collector._throttle_bbo(rows)
        # 第 1 条通过,后续在 10s 窗口内被压制
        self.assertEqual(len(throttled), 1)

    async def test_bbo_throttle_disabled_with_zero_interval(self) -> None:
        """interval=0 等价于关闭限流,全部 row 通过。"""
        collector = MicrostructureCollector(
            settings=_ws_settings(),
            bbo_min_interval_seconds=0.0,
        )
        rows = [_make_bbo_row() for _ in range(3)]
        throttled = collector._throttle_bbo(rows)
        self.assertEqual(len(throttled), 3)

    async def test_bbo_throttle_per_symbol(self) -> None:
        """限流按 symbol 分别计算,多 symbol 不相互压制。"""
        collector = MicrostructureCollector(
            settings=_ws_settings(),
            symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
            bbo_min_interval_seconds=10.0,
            books5_min_interval_seconds=0.0,
        )
        btc = BboRow(
            symbol="BTC-USDT-SWAP", ts=_TS, source_ts=_TS,
            bid_px=Decimal("95000"), bid_sz=Decimal("1"),
            ask_px=Decimal("95010"), ask_sz=Decimal("2"),
        )
        eth = BboRow(
            symbol="ETH-USDT-SWAP", ts=_TS, source_ts=_TS,
            bid_px=Decimal("3000"), bid_sz=Decimal("1"),
            ask_px=Decimal("3010"), ask_sz=Decimal("2"),
        )
        out = collector._throttle_bbo([btc, eth, btc, eth])
        # 每个 symbol 各通过 1 条
        self.assertEqual(len(out), 2)
        self.assertEqual({r.symbol for r in out}, {"BTC-USDT-SWAP", "ETH-USDT-SWAP"})


class TestThrottleBooks5(unittest.IsolatedAsyncioTestCase):
    async def test_books5_throttle_suppresses_rapid_samples(self) -> None:
        collector = MicrostructureCollector(
            settings=_ws_settings(),
            books5_min_interval_seconds=10.0,
            bbo_min_interval_seconds=0.0,
        )
        rows = [_make_books5_row() for _ in range(5)]
        throttled = collector._throttle_books5(rows)
        self.assertEqual(len(throttled), 1)

    async def test_books5_ts_overwritten_with_client_instant(self) -> None:
        """限流后 ts 字段是客户端采样时刻, source_ts 保留 OKX 推送原值。"""
        collector = MicrostructureCollector(
            settings=_ws_settings(),
            books5_min_interval_seconds=0.5,
            bbo_min_interval_seconds=0.0,
        )
        rows = [_make_books5_row()]
        throttled = collector._throttle_books5(rows)
        self.assertEqual(len(throttled), 1)
        # source_ts 保留不变
        self.assertEqual(throttled[0].source_ts, _TS)
        # ts 被替换为 utc_now(),不等于 source_ts
        self.assertNotEqual(throttled[0].ts, _TS)


# =====================================================================
# Case 5: Collector flush — 端到端路径经 mock session
# =====================================================================


class TestCollectorFlush(unittest.IsolatedAsyncioTestCase):
    async def test_flush_trades_writes_via_get_session(self) -> None:
        captured = _CapturingSession()

        @contextlib.contextmanager
        def _fake_session():
            yield captured

        collector = MicrostructureCollector(settings=_ws_settings())
        collector._ingest_run_id = "00000000-0000-0000-0000-000000000001"

        # 手动填 buffer
        await collector._buf_trades.add_many([_make_trade_row(f"T-{i}") for i in range(3)])

        with patch(
            "aats.data_platform.collectors.microstructure_ws_collector.get_session",
            _fake_session,
        ):
            result = await collector._flush_trades(reason="manual")

        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.written, 3)
        self.assertIsNone(result.error)
        self.assertEqual(collector._buf_trades.buffered(), 0)
        self.assertEqual(collector._written_counts["bronze.market_trades"], 3)
        # 验证 SQL 片段
        raw_writes = [
            item
            for item in captured.executed
            if "INSERT INTO bronze.market_trades" in item[0]
        ]
        continuity_writes = [
            item
            for item in captured.executed
            if "INSERT INTO meta.collector_continuity_events" in item[0]
        ]
        self.assertEqual(len(raw_writes), 1)
        self.assertEqual(len(continuity_writes), 1)
        sql, batch = raw_writes[0]
        self.assertIn("INSERT INTO bronze.market_trades", sql)
        self.assertIn("ON CONFLICT", sql.upper())
        self.assertEqual(len(batch), 3)

    async def test_flush_oif_does_not_include_ingest_run_id(self) -> None:
        """staging.market_oi_funding_ticks 没有 ingest_run_id 列 (Stage 1 design)。
        write_oif_batch 不应引用该列。"""
        captured = _CapturingSession()

        @contextlib.contextmanager
        def _fake_session():
            yield captured

        collector = MicrostructureCollector(settings=_ws_settings())
        collector._ingest_run_id = "00000000-0000-0000-0000-000000000002"

        await collector._buf_oif.add(_make_oif_row("mark"))

        with patch(
            "aats.data_platform.collectors.microstructure_ws_collector.get_session",
            _fake_session,
        ):
            result = await collector._flush_oif(reason="manual")

        self.assertEqual(result.written, 1)
        sql, _batch = captured.executed[0]
        self.assertIn("INSERT INTO staging.market_oi_funding_ticks", sql)
        self.assertNotIn("ingest_run_id", sql.lower())

    async def test_flush_swallows_db_errors(self) -> None:
        """DB 错误应 drop batch 并打日志,但不上抛 (保证 WS 消费循环不被打断)。"""

        class _FailingSession:
            def execute(self, *_args, **_kwargs):
                raise OperationalError("stmt", {}, Exception("simulated"))

            def commit(self) -> None:
                pass

            def rollback(self) -> None:
                pass

            def close(self) -> None:
                pass

        @contextlib.contextmanager
        def _fake_session():
            yield _FailingSession()

        collector = MicrostructureCollector(settings=_ws_settings())
        collector._ingest_run_id = "00000000-0000-0000-0000-000000000003"
        await collector._buf_trades.add(_make_trade_row("T-fail"))

        with patch(
            "aats.data_platform.collectors.microstructure_ws_collector.get_session",
            _fake_session,
        ):
            result = await collector._flush_trades(reason="manual")

        self.assertEqual(result.written, 0)
        self.assertEqual(result.attempted, 1)
        self.assertIsNotNone(result.error)
        # buffer 已清空,row 被 drop (接受 §10 data loss trade-off)
        self.assertEqual(collector._buf_trades.buffered(), 0)


# =====================================================================
# Case 6: Collector _handle_message 分派
# =====================================================================


class TestCollectorDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_handle_trades_message_routes_to_trades_buffer(self) -> None:
        collector = MicrostructureCollector(settings=_ws_settings())
        push = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instId": "BTC-USDT-SWAP", "tradeId": "T-1",
                "px": "95000", "sz": "0.1", "side": "buy",
                "ts": "1745000000000",
            }],
        }
        await collector._handle_message(push)
        self.assertEqual(collector._buf_trades.buffered(), 1)
        self.assertEqual(collector._buf_bbo.buffered(), 0)
        self.assertEqual(collector._buf_books5.buffered(), 0)
        self.assertEqual(collector._buf_oif.buffered(), 0)

    async def test_handle_mark_price_routes_to_oif_buffer(self) -> None:
        collector = MicrostructureCollector(settings=_ws_settings())
        push = {
            "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "markPx": "95000",
                "ts": "1745000000000",
            }],
        }
        await collector._handle_message(push)
        self.assertEqual(collector._buf_oif.buffered(), 1)
        self.assertEqual(collector._buf_trades.buffered(), 0)

    async def test_unknown_channel_silently_ignored(self) -> None:
        """未知 channel 不应抛错,静默 drop。"""
        collector = MicrostructureCollector(settings=_ws_settings())
        push = {
            "arg": {"channel": "some-future-channel", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP"}],
        }
        # no raise
        await collector._handle_message(push)
        self.assertEqual(collector._buf_trades.buffered(), 0)
        self.assertEqual(collector._buf_oif.buffered(), 0)

    async def test_metrics_counted_when_registry_provided(self) -> None:
        """启用 MetricsRegistry 时各 counter 正确累加。"""
        registry = MetricsRegistry()
        collector = MicrostructureCollector(
            settings=_ws_settings(),
            metrics_registry=registry,
        )
        push_trades = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instId": "BTC-USDT-SWAP", "tradeId": "T-1",
                "px": "95000", "sz": "0.1", "side": "buy",
                "ts": "1745000000000",
            }],
        }
        await collector._handle_message(push_trades)
        snapshot = registry.snapshot()
        self.assertEqual(snapshot.get("microstructure_ws_messages_total"), 1)


# =====================================================================
# Case 7: Periodic flush 定时触发
# =====================================================================


class TestPeriodicFlush(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_flush_fires_after_timeout(self) -> None:
        """flush_max_seconds 超时后 periodic loop 触发 _flush_xxx,
        直到 stop_event 被 set 才退出。"""
        captured = _CapturingSession()

        @contextlib.contextmanager
        def _fake_session():
            yield captured

        collector = MicrostructureCollector(
            settings=_ws_settings(),
            flush_trades_max_rows=10000,
            flush_trades_max_seconds=0.05,     # 50 ms
            flush_bbo_max_rows=10000,
            flush_bbo_max_seconds=60.0,
            flush_books5_max_rows=10000,
            flush_books5_max_seconds=60.0,
            flush_oif_max_rows=10000,
            flush_oif_max_seconds=60.0,
        )
        collector._ingest_run_id = "00000000-0000-0000-0000-000000000004"
        await collector._buf_trades.add(_make_trade_row("T-periodic"))

        with patch(
            "aats.data_platform.collectors.microstructure_ws_collector.get_session",
            _fake_session,
        ):
            flush_task = asyncio.create_task(
                collector._periodic_flush_for(collector._buf_trades, collector._flush_trades)
            )
            await asyncio.sleep(0.15)
            collector.client.stop_event.set()
            await asyncio.wait_for(flush_task, timeout=1.0)

        # 定时 flush 至少执行了 1 次 INSERT
        self.assertGreaterEqual(len(captured.executed), 1)


if __name__ == "__main__":
    unittest.main()
