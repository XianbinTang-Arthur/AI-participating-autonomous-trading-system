from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.events.envelopes import parse_envelope
from aats.schemas.common import utc_now
from aats.schemas.market import MarketSnapshot
from aats.services.market_gateway.demo_provider import DemoMarketDataProvider
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.okx_normalizer import (
    CandleGap,
    OKXInstrumentMarketState,
    OKXMarketSnapshotNormalizer,
)
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient
from aats.services.market_gateway.publisher import MarketSnapshotPublisher
from aats.services.execution_engine.okx_rest import OKXRESTClient


class MarketDataGateway:

    def __init__(
        self,
        *,
        settings: AATSSettings,
        normalizer: MarketSnapshotNormalizer,
        publisher: MarketSnapshotPublisher,
        okx_normalizer: OKXMarketSnapshotNormalizer | None = None,
        okx_ws_client: OKXPublicWebSocketClient | None = None,
        okx_rest_client: OKXRESTClient | None = None,
        is_producer: bool = True,
    ) -> None:
        self.settings = settings
        self.normalizer = normalizer
        self.publisher = publisher
        self.okx_normalizer = okx_normalizer or OKXMarketSnapshotNormalizer(exchange_name="OKX")
        self.okx_ws_client = okx_ws_client
        self.okx_rest_client = okx_rest_client
        # is_producer 标记：True = 本进程拥有 OKX WebSocket（market / monolith 角色），
        # False = 本进程通过 NATS 从远端 producer 接收快照（gateway / decision / execution 角色）。
        # 影响 status() 对 transport_connected 的计算逻辑：consumer 模式下不检查
        # 本地 okx_ws_client，而是从 NATS 快照新鲜度推导连接状态。
        self._is_producer = is_producer
        self.logger = get_logger("aats.market_gateway")
        self._demo_provider = DemoMarketDataProvider(exchange_name=settings.exchange_name)
        self._latest_snapshots: dict[str, MarketSnapshot] = {}
        self._latest_received_at: dict[str, Any] = {}
        self._okx_states: dict[str, OKXInstrumentMarketState] = {}
        self._background_task: asyncio.Task[None] | None = None
        self._fallback_task: asyncio.Task[None] | None = None
        self._backfill_tasks: set[asyncio.Task[None]] = set()
        self._last_publish_ts = None
        self._last_error: str | None = None
        self._consecutive_message_errors: int = 0
        # R2-P1-M8：schema 错误单独计数。P1-9 把 ValueError/KeyError 分离出系统
        # 错误，但原代码完全不 counting → 若 OKX 改了推送格式，会静默冲刷日志
        # 永远不 escalate。独立计数 + 阈值升级 critical，保证 schema 故障不被
        # 系统错误路径漏掉。
        self._consecutive_schema_errors: int = 0
        self._rest_fallback_last_success_ts = None
        self._rest_fallback_last_attempt_ts = None
        self._rest_fallback_last_error: str | None = None
        self._rest_fallback_active = False
        self._rest_fallback_consecutive_failures: int = 0

    async def start(self) -> None:
        if self.settings.market_data_backend != "okx":
            return
        # R2-P1-M9：启动时预校验配置的 symbol。P1-7 给 okx_inst_id 加了格式校验，
        # 但仅在消息路径（apply_message）里兜住；如果 expanded_allowed_symbols()
        # 本身就有 typo（"BTC-USD" 少了 T），subscribe 消息会被 OKX 侧拒，P0-2
        # 触发 ack timeout 死循环重连。这里提前按规则过一遍，明确告警非法 symbol
        # 的同时仍然启动服务（其他合法 symbol 能正常消费），不 hard-fail。
        raw_symbols = tuple(self.settings.expanded_allowed_symbols())
        invalid_symbols: list[tuple[str, str]] = []
        for symbol in raw_symbols:
            try:
                self.okx_normalizer.okx_inst_id(symbol)
            except ValueError as exc:
                invalid_symbols.append((symbol, str(exc)))
        if invalid_symbols:
            log_event(
                self.logger,
                "okx_invalid_symbol_config_detected",
                level="error",
                invalid_symbols=[{"symbol": s, "error": e} for s, e in invalid_symbols],
                total_configured=len(raw_symbols),
            )
        if self.okx_ws_client is not None:
            if self._background_task is None or self._background_task.done():
                self._background_task = asyncio.create_task(self._run_okx_stream(), name="aats_okx_market_stream")
        if (
            self.settings.okx_market_rest_fallback_enabled
            and self.okx_rest_client is not None
            and (self._fallback_task is None or self._fallback_task.done())
        ):
            self._fallback_task = asyncio.create_task(
                self._run_okx_rest_fallback_loop(),
                name="aats_okx_market_rest_fallback",
            )

    async def stop(self) -> None:
        if self.okx_ws_client is not None:
            await self.okx_ws_client.stop()
        if self._background_task is not None:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
            self._background_task = None
        if self._fallback_task is not None:
            self._fallback_task.cancel()
            try:
                await self._fallback_task
            except asyncio.CancelledError:
                pass
            self._fallback_task = None
        for task in list(self._backfill_tasks):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._backfill_tasks.clear()

    async def publish_local_snapshot(self, symbol: str | None = None) -> MarketSnapshot:
        trading_symbol = symbol or self.settings.default_symbol
        snapshot = self.normalizer.normalize(self._build_local_payload(trading_symbol))
        await self._publish_snapshot(snapshot)
        return snapshot

    async def seed_demo_snapshot(self, symbol: str | None = None) -> MarketSnapshot:
        return await self.publish_local_snapshot(symbol=symbol)

    async def refresh_snapshot(self, *, symbol: str | None = None) -> MarketSnapshot:
        if symbol is None:
            snapshots = await self.refresh_snapshots()
            return snapshots.get(self.settings.default_symbol) or next(iter(snapshots.values()))
        trading_symbol = symbol or self.settings.default_symbol
        self._rest_fallback_last_attempt_ts = utc_now()
        try:
            if self.settings.market_data_backend == "okx":
                snapshot = await self._fetch_okx_rest_snapshot(symbol=trading_symbol)
                await self._publish_snapshot(snapshot)
            else:
                snapshot = await self.publish_local_snapshot(symbol=trading_symbol)
        except Exception as exc:
            self._last_error = str(exc)
            self._rest_fallback_last_error = str(exc)
            raise
        self._rest_fallback_last_success_ts = utc_now()
        self._rest_fallback_last_error = None
        self._rest_fallback_active = False
        self._last_error = None
        return snapshot

    async def refresh_snapshots(self, *, symbols: tuple[str, ...] | list[str] | None = None) -> dict[str, MarketSnapshot]:
        tracked_symbols = tuple(dict.fromkeys(symbols or self._tracked_symbols()))
        if not tracked_symbols:
            tracked_symbols = (self.settings.default_symbol,)
        self._rest_fallback_last_attempt_ts = utc_now()
        snapshots: dict[str, MarketSnapshot] = {}
        try:
            if self.settings.market_data_backend == "okx":
                results = await asyncio.gather(
                    *(self._fetch_okx_rest_snapshot(symbol=sym) for sym in tracked_symbols),
                    return_exceptions=True,
                )
                for sym, result in zip(tracked_symbols, results):
                    if isinstance(result, Exception):
                        log_event(
                            self.logger,
                            "refresh_snapshot_symbol_failed",
                            level="warning",
                            symbol=sym,
                            error_type=type(result).__name__,
                            error=str(result),
                        )
                        continue
                    await self._publish_snapshot(result)
                    snapshots[sym] = result
                if not snapshots:
                    raise RuntimeError(
                        f"all_symbol_refresh_failed: symbols={list(tracked_symbols)}"
                    )
            else:
                for trading_symbol in tracked_symbols:
                    snapshot = await self.publish_local_snapshot(symbol=trading_symbol)
                    snapshots[trading_symbol] = snapshot
        except Exception as exc:
            self._last_error = str(exc)
            self._rest_fallback_last_error = str(exc)
            raise
        self._rest_fallback_last_success_ts = utc_now()
        self._rest_fallback_last_error = None
        self._rest_fallback_active = False
        self._last_error = None
        return snapshots

    async def run_local_publisher(
        self,
        *,
        symbol: str | None = None,
        iterations: int,
        interval_seconds: float,
    ) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        for index in range(iterations):
            snapshots.append(await self.publish_local_snapshot(symbol=symbol))
            if interval_seconds > 0 and index + 1 < iterations:
                await asyncio.sleep(interval_seconds)
        return snapshots

    @property
    def is_producer(self) -> bool:
        """本进程是否是行情数据的 producer（拥有 OKX WebSocket）。

        True = market / monolith 角色，False = gateway / decision / execution 角色。
        """
        return self._is_producer

    def apply_remote_snapshot(self, snapshot: MarketSnapshot) -> None:
        """NATS 远端推送：非 producer 进程收到 market 进程广播的快照后调用。

        更新本地缓存使 is_fresh() / latest_price() / status() 反映跨进程同步
        到的最新市场状态。producer 进程（market / monolith）自己通过
        _publish_snapshot() 写入相同字段，不需要走这条路径。
        """
        # P0-1 幂等保护：NATS at-least-once 可能重投递同一快照，或因网络乱序
        # 送达旧于本地的快照。两种情况都不能刷新 _latest_received_at（否则
        # is_fresh() 会把"已陈旧的数据"判断为新鲜，REST fallback 永不触发）。
        # 仅当新快照的 snapshot_ts 严格新于本地版本时才 apply。
        # R2-P0-M1：existing.snapshot_ts None 防御——Pydantic schema 虽然把
        # snapshot_ts 声明为 datetime 必填，但历史 event_store payload 如果
        # 有 schema-migration 过的旧记录，可能走到这里时为 None。让 None 版本
        # 被新快照覆盖（existing 视为"比新的都旧"）。
        # R2-P1-M3：从 `<=` 改为 `<`，相同 ms 精度的 snapshot_ts 是 OKX 合法
        # 场景（两笔 ticker 同 ms），用严格小于保证后到的最新 bid/ask 不被丢。
        # new_snapshot_ts 本身 None 则拒收（上游 schema violation）。
        new_ts = snapshot.snapshot_ts
        if new_ts is None:
            return
        existing = self._latest_snapshots.get(snapshot.symbol)
        if (
            existing is not None
            and existing.snapshot_ts is not None
            and new_ts < existing.snapshot_ts
        ):
            return
        now = utc_now()
        self._latest_snapshots[snapshot.symbol] = snapshot
        self._latest_received_at[snapshot.symbol] = now
        if self._last_publish_ts is None or now > self._last_publish_ts:
            self._last_publish_ts = now

    async def handle_remote_market_snapshot(self, message: dict) -> None:
        """NATS bus handler：接收 market.snapshots topic 的远端快照。

        仅 non-producer 角色（gateway / decision / execution）使用。
        handler 签名遵循 EventBus.subscribe() 的 MessageHandler 协议：
        接收已解码的 dict envelope，内部 parse → validate → apply。

        异常一律 swallow + 日志：单条消息解析失败不应中断 durable consumer
        的整个消费循环——下一条消息大概率能成功，行情流不会因为偶发的
        envelope 格式异常而全面中断。
        """
        try:
            envelope = parse_envelope(message)
            snapshot = MarketSnapshot.model_validate(envelope.payload)
            self.apply_remote_snapshot(snapshot)
        except Exception as exc:
            log_event(
                self.logger,
                "market_remote_snapshot_handler_error",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def latest_snapshot(self, symbol: str) -> MarketSnapshot | None:
        return self._latest_snapshots.get(symbol)

    def latest_price(self, symbol: str) -> Decimal:
        snapshot = self._latest_snapshots.get(symbol)
        return snapshot.last_price if snapshot is not None else Decimal("0")

    def is_fresh(self, symbol: str) -> bool:
        return self.receipt_is_fresh(symbol) and self.snapshot_is_fresh(symbol)

    def receipt_is_fresh(self, symbol: str) -> bool:
        received_at = self._latest_received_at.get(symbol)
        if received_at is None:
            return False
        age_seconds = (utc_now() - received_at).total_seconds()
        return age_seconds <= self.settings.market_data_stale_after_seconds

    def snapshot_is_fresh(self, symbol: str) -> bool:
        snapshot = self._latest_snapshots.get(symbol)
        if snapshot is None:
            return False
        age_seconds = (utc_now() - snapshot.snapshot_ts).total_seconds()
        return age_seconds <= self.settings.market_data_stale_after_seconds

    def status(self) -> dict[str, Any]:
        default_snapshot = self._latest_snapshots.get(self.settings.default_symbol)
        last_received_ts = self._latest_received_at.get(self.settings.default_symbol)
        tracked_symbols = self._tracked_symbols()
        stale_symbols = [symbol for symbol in tracked_symbols if not self.is_fresh(symbol)]
        connected = True
        last_update_ts = last_received_ts or (default_snapshot.snapshot_ts if default_snapshot is not None else None)
        detail = "demo_market_data"
        transport_connected = True
        transport_connected_public: bool | None = None
        transport_connected_business: bool | None = None
        receipt_fresh = self.receipt_is_fresh(self.settings.default_symbol)
        snapshot_fresh = self.snapshot_is_fresh(self.settings.default_symbol)
        fresh = receipt_fresh and snapshot_fresh
        if self.settings.market_data_backend == "okx" and self._is_producer and self.okx_ws_client is not None:
            # Producer 模式（market / monolith 角色）：直接查询本地 WS 客户端状态。
            okx_status = self.okx_ws_client.status()
            transport_connected = bool(okx_status["connected"])
            transport_connected_public = bool(okx_status.get("connected_public", False))
            transport_connected_business = bool(okx_status.get("connected_business", False))
            last_update_ts = okx_status.get("last_message_ts") or last_update_ts
            detail = "okx_public_ws"
            self._last_error = okx_status.get("last_error")
        elif self.settings.market_data_backend == "okx" and not self._is_producer:
            # Consumer 模式（gateway / decision / execution 角色）：本进程不拥有
            # OKX WebSocket，而是通过 NATS 订阅从 market 进程接收快照。
            # transport_connected 根据 NATS 快照新鲜度推导：只要本进程持续收到
            # 新鲜的 NATS 快照，就说明远端 producer 的 WS 通道是活的。
            transport_connected = receipt_fresh
            detail = "okx_nats_consumer"
        if self.settings.market_data_backend == "okx":
            connected = transport_connected or receipt_fresh
        blockers: list[str] = []
        if not transport_connected and not receipt_fresh:
            blockers.append("market_connection_down")
        if not fresh:
            blockers.append("market_data_stale")
        if not transport_connected and receipt_fresh:
            detail = f"{detail}_transport_degraded"
        if self._rest_fallback_active:
            detail = f"{detail}_rest_fallback"
        return {
            "backend": self.settings.market_data_backend,
            "connected": connected,
            "transport_connected": transport_connected,
            "transport_connected_public": transport_connected_public,
            "transport_connected_business": transport_connected_business,
            "fresh": fresh,
            "receipt_fresh": receipt_fresh,
            "snapshot_fresh": snapshot_fresh,
            "last_update_ts": last_update_ts,
            "market_snapshot_ts": default_snapshot.snapshot_ts if default_snapshot is not None else None,
            "last_error": self._last_error,
            "rest_fallback_enabled": bool(self.settings.okx_market_rest_fallback_enabled and self.okx_rest_client is not None),
            "rest_fallback_active": self._rest_fallback_active,
            "rest_fallback_last_attempt_ts": self._rest_fallback_last_attempt_ts,
            "rest_fallback_last_success_ts": self._rest_fallback_last_success_ts,
            "rest_fallback_last_error": self._rest_fallback_last_error,
            "detail": detail,
            "blockers": blockers,
            "ready": fresh,
            "tracked_symbols": list(tracked_symbols),
            "tracked_symbol_count": len(tracked_symbols),
            "stale_tracked_symbols": stale_symbols,
            "tracked_symbols_fresh": len(stale_symbols) == 0,
        }

    async def _run_okx_stream(self) -> None:
        if self.okx_ws_client is None:
            return
        log_event(self.logger, "market_stream_started", backend="okx")
        await self.okx_ws_client.run_forever(on_message=self._handle_okx_message)

    async def _run_okx_rest_fallback_loop(self) -> None:
        _CIRCUIT_BREAKER_THRESHOLD = 10
        _CIRCUIT_BREAKER_COOLDOWN_MULTIPLIER = 6
        while True:
            try:
                refreshed = await self._run_okx_rest_fallback_once()
                if refreshed:
                    self._rest_fallback_consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._rest_fallback_consecutive_failures += 1
                self._rest_fallback_last_error = str(exc)
                circuit_open = self._rest_fallback_consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD
                log_event(
                    self.logger,
                    "okx_market_rest_fallback_error",
                    level="critical" if circuit_open else "error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    consecutive_failures=self._rest_fallback_consecutive_failures,
                    circuit_breaker_open=circuit_open,
                )
                if circuit_open:
                    await asyncio.sleep(
                        self.settings.okx_market_rest_fallback_poll_interval_seconds
                        * _CIRCUIT_BREAKER_COOLDOWN_MULTIPLIER
                    )
                    continue
            await asyncio.sleep(self.settings.okx_market_rest_fallback_poll_interval_seconds)

    async def _run_okx_rest_fallback_once(self) -> bool:
        if (
            self.settings.market_data_backend != "okx"
            or not self.settings.okx_market_rest_fallback_enabled
            or self.okx_rest_client is None
        ):
            self._rest_fallback_active = False
            return False
        stale_symbols = [symbol for symbol in self._tracked_symbols() if not self.is_fresh(symbol)]
        if not stale_symbols:
            self._rest_fallback_active = False
            return False
        self._rest_fallback_last_attempt_ts = utc_now()
        refreshed: dict[str, MarketSnapshot] = {}
        for symbol in stale_symbols:
            try:
                snapshot = await self._fetch_okx_rest_snapshot(symbol=symbol)
                await self._publish_snapshot(snapshot)
                refreshed[symbol] = snapshot
            except Exception as exc:
                log_event(
                    self.logger,
                    "okx_rest_fallback_symbol_failed",
                    level="warning",
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        if not refreshed:
            return False
        self._rest_fallback_last_success_ts = utc_now()
        self._rest_fallback_last_error = None
        self._rest_fallback_active = True
        log_event(
            self.logger,
            "okx_market_rest_fallback_published",
            symbols=list(refreshed.keys()),
            failed_symbols=[s for s in stale_symbols if s not in refreshed],
            snapshot_ts_map={symbol: snapshot.snapshot_ts.isoformat() for symbol, snapshot in refreshed.items()},
        )
        return True

    async def _fetch_okx_rest_snapshot(self, *, symbol: str) -> MarketSnapshot:
        if self.okx_rest_client is None:
            raise RuntimeError("okx_rest_client_unavailable")
        _gather_results = await asyncio.gather(
            self.okx_rest_client.get_market_ticker(symbol=symbol),
            self.okx_rest_client.get_market_candles(symbol=symbol, bar="15m", limit=1),
            self.okx_rest_client.get_market_candles(symbol=symbol, bar="1H", limit=1),
            return_exceptions=True,
        )
        for _r in _gather_results:
            if isinstance(_r, Exception):
                self.logger.warning("gather task failed: %s", _r)
        ticker_payload = _gather_results[0] if not isinstance(_gather_results[0], Exception) else {}
        candle_15m_payload = _gather_results[1] if not isinstance(_gather_results[1], Exception) else {}
        candle_1h_payload = _gather_results[2] if not isinstance(_gather_results[2], Exception) else {}
        ticker_rows = ticker_payload.get("data", [])
        candle_15m_rows = candle_15m_payload.get("data", [])
        candle_1h_rows = candle_1h_payload.get("data", [])
        if not isinstance(ticker_rows, list) or not ticker_rows or not isinstance(ticker_rows[0], dict):
            raise RuntimeError("okx_market_rest_ticker_missing")
        if not isinstance(candle_15m_rows, list) or not candle_15m_rows or not isinstance(candle_15m_rows[0], list):
            raise RuntimeError("okx_market_rest_candle_15m_missing")
        if not isinstance(candle_1h_rows, list) or not candle_1h_rows or not isinstance(candle_1h_rows[0], list):
            raise RuntimeError("okx_market_rest_candle_1h_missing")
        return self.okx_normalizer.build_snapshot_from_rest_payloads(
            symbol=symbol,
            ticker_payload=ticker_rows[0],
            candle_15m_payload=candle_15m_rows[0],
            candle_1h_payload=candle_1h_rows[0],
        )

    async def _handle_okx_message(self, message: dict[str, Any]) -> None:
        # P1-9：异常分级。
        #   - ValueError / KeyError：单条消息 schema/字段问题，很可能是 OKX 偶发
        #     畸形推送，记 warning 并跳过；不参与连续错误计数（避免噪声）。
        #   - 其他异常：视为系统问题（normalizer 内部异常、publisher 失败等），
        #     累计到 _consecutive_message_errors；超阈值升级 critical 主动告警。
        # R2-P1-M8：schema 错误现在有独立计数器 _consecutive_schema_errors，阈值
        # 比系统错误稍高（100 vs 20）——单条偶发畸形是正常，但持续 100 条意味
        # OKX 可能动了 schema 或 normalizer 规则需要更新，必须 escalate critical。
        _CONSECUTIVE_ERROR_ESCALATION = 20
        _CONSECUTIVE_SCHEMA_ERROR_ESCALATION = 100
        try:
            snapshots = self.okx_normalizer.apply_message(message=message, states=self._okx_states)
            for snapshot in snapshots:
                await self._publish_snapshot(snapshot)
            gaps = self.okx_normalizer.drain_detected_gaps()
            if gaps:
                task = asyncio.create_task(
                    self._handle_candle_gaps(gaps),
                    name="aats_okx_gap_backfill",
                )
                self._backfill_tasks.add(task)
                task.add_done_callback(self._backfill_tasks.discard)
            self._consecutive_message_errors = 0
            self._consecutive_schema_errors = 0
        except (ValueError, KeyError) as exc:
            self._consecutive_schema_errors += 1
            self._last_error = str(exc)
            schema_level = "warning"
            if self._consecutive_schema_errors >= _CONSECUTIVE_SCHEMA_ERROR_ESCALATION:
                schema_level = "critical"
            log_event(
                self.logger,
                "okx_market_message_schema_error",
                level=schema_level,
                error_type=type(exc).__name__,
                error=str(exc),
                message_preview=str(message)[:200],
                consecutive_schema_errors=self._consecutive_schema_errors,
            )
            return
        except Exception as exc:
            self._consecutive_message_errors += 1
            self._last_error = str(exc)
            level = "error"
            if self._consecutive_message_errors >= _CONSECUTIVE_ERROR_ESCALATION:
                level = "critical"
            log_event(
                self.logger,
                "okx_market_message_error",
                level=level,
                error_type=type(exc).__name__,
                error=str(exc),
                consecutive_errors=self._consecutive_message_errors,
            )
            # Market transport should stay alive even if a downstream consumer fails.
            return

    async def _handle_candle_gaps(self, gaps: list[CandleGap]) -> None:
        try:
            for gap in gaps:
                log_event(
                    self.logger,
                    "okx_ws_candle_gap_detected",
                    level="warning",
                    symbol=gap.symbol,
                    channel=gap.channel,
                    last_ts=gap.last_ts.isoformat(),
                    new_ts=gap.new_ts.isoformat(),
                    gap_seconds=(gap.new_ts - gap.last_ts).total_seconds(),
                    expected_interval_seconds=gap.expected_interval_seconds,
                )
            if self.okx_rest_client is None:
                return
            affected_symbols = list(dict.fromkeys(gap.symbol for gap in gaps))
            for symbol in affected_symbols:
                try:
                    snapshot = await self._fetch_okx_rest_snapshot(symbol=symbol)
                    await self._publish_snapshot(snapshot)
                    log_event(self.logger, "okx_ws_gap_backfill_complete", symbol=symbol)
                except Exception as exc:
                    log_event(
                        self.logger,
                        "okx_ws_gap_backfill_failed",
                        level="warning",
                        symbol=symbol,
                        error=str(exc),
                    )
        except Exception as exc:
            log_event(
                self.logger,
                "okx_ws_gap_handler_error",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _publish_snapshot(self, snapshot: MarketSnapshot) -> None:
        received_at = utc_now()
        # 先更新本地快照缓存，确保 latest_price() 等本地查询立即可用。
        self._latest_snapshots[snapshot.symbol] = snapshot
        # NATS 发布：可能抛 TimeoutError / ConnectionError。
        await self.publisher.publish(snapshot)
        # ── 仅在发布成功后才标记"已收到"──
        # _latest_received_at 驱动 is_fresh() → 驱动 REST fallback 的触发条件。
        # 如果在 publish 之前就更新，NATS 不可用时 REST fallback 也不会启动，
        # 形成静默数据中断：WebSocket 在收、NATS 在丢、REST 以为没问题。
        self._latest_received_at[snapshot.symbol] = received_at
        self._last_publish_ts = received_at

    def _tracked_symbols(self) -> tuple[str, ...]:
        symbols = tuple(dict.fromkeys(symbol for symbol in self.settings.expanded_allowed_symbols() if symbol))
        return symbols or (self.settings.default_symbol,)

    def _build_local_payload(self, symbol: str) -> dict[str, Any]:
        return self._demo_provider.build_payload(symbol)
