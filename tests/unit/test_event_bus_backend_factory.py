"""Stage 4 单元测试：_construct_event_bus 工厂 + _start_event_bus 生命周期。

覆盖：

1. **纯构造**（不连接 NATS server）
   - in_memory backend → InMemoryEventBus
   - hybrid backend    → HybridEventBus(critical=NatsEventBus, observer=InMemoryBus)
   - nats backend      → NatsEventBus

2. **NatsBusConfig 字段映射**
   - settings.nats_url       → NatsBusConfig.servers (单元素 tuple)
   - settings.nats_stream_name → NatsBusConfig.stream_name
   - settings.nats_stream_max_age_seconds → NatsBusConfig.stream_max_age_seconds

3. **生命周期**
   - _start_event_bus(InMemoryEventBus) 是 no-op（向后兼容默认）
   - _start_event_bus(HybridEventBus) 委派到底层 NatsEventBus.start
   - HybridEventBus.start 把 critical_topics 透传给 critical_bus.start
   - HybridEventBus.close 委派到两条底层总线

4. **observer_bus 不双写 event_store**：避免 critical 已经持久化又被
   observer 写一次造成的重复事件。

⚠️ 这一组测试 **不依赖 nats-py 服务器**：所有 NatsEventBus.start 调用都被
mock 替换，只验证组装路径、参数传递、生命周期顺序。真正的 JetStream
集成测试在 testcontainers 集成层完成。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

import pytest

from aats.bootstrap.config import (
    _construct_event_bus,
    _register_event_bus_connection_supervision,
    _start_event_bus,
)
from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.bus.nats_bus import (
    DEFAULT_CRITICAL_TOPICS,
    HybridEventBus,
    NatsEventBus,
)


def _paper_settings(**overrides: object) -> AATSSettings:
    base = {
        "mode": "paper_live",
        "market_data_backend": "demo",
        "execution_backend": "paper",
        "account_backend": "disabled",
        "account_read_enabled": False,
        "storage_mode": "memory",
        "event_persistence_mode": "strict",
    }
    base.update(overrides)
    return AATSSettings.model_validate(base)


# ─────────────────────────────────────────────────────────────────────
# 纯构造（不连接 NATS server）
# ─────────────────────────────────────────────────────────────────────


class TestConstructEventBusInMemory:
    """in_memory backend 必须返回 InMemoryEventBus，不实例化任何 NATS 类型。"""

    def test_in_memory_backend_returns_in_memory_bus(self) -> None:
        settings = _paper_settings(event_bus_backend="in_memory")
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, InMemoryEventBus)
        # 必须不是 NATS / Hybrid 衍生类型
        assert not isinstance(bus, NatsEventBus)
        assert not isinstance(bus, HybridEventBus)

    def test_in_memory_backend_passes_event_store(self) -> None:
        """in_memory 模式需要 event_store 才能持久化事件——确保参数正确传递。"""
        sentinel = object()
        settings = _paper_settings(event_bus_backend="in_memory")
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=sentinel,
            process_role=None,
        )
        assert bus._event_store is sentinel  # type: ignore[attr-defined]

    def test_in_memory_backend_passes_persistence_mode(self) -> None:
        settings = _paper_settings(
            event_bus_backend="in_memory",
            event_persistence_mode="permissive",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert bus._persistence_mode == "permissive"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hybrid_nats_connection_waiter_is_registered_as_critical() -> None:
    failure = asyncio.Event()

    class _CriticalBus:
        async def wait_for_terminal_connection_failure(self) -> None:
            await failure.wait()
            raise RuntimeError("nats_connection_terminal_failure")

    class _Runtime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def create_background_task(
            self,
            coroutine,
            *,
            name: str,
            critical: bool,
        ) -> asyncio.Task[object]:
            self.calls.append((name, critical))
            return asyncio.create_task(coroutine, name=name)

    runtime = _Runtime()
    hybrid = HybridEventBus(
        critical_bus=_CriticalBus(),  # type: ignore[arg-type]
        observer_bus=InMemoryEventBus(),
    )
    task = _register_event_bus_connection_supervision(
        runtime=runtime,  # type: ignore[arg-type]
        bus=hybrid,
    )
    assert task is not None
    assert runtime.calls == [("aats_nats_connection_supervision", True)]
    failure.set()
    with pytest.raises(RuntimeError, match="nats_connection_terminal_failure"):
        await task


class TestConstructEventBusHybrid:
    """hybrid backend 必须返回 HybridEventBus，内部 critical 是 NatsEventBus，
    observer 是 InMemoryEventBus。"""

    def test_hybrid_backend_returns_hybrid_bus(self) -> None:
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)

    def test_hybrid_backend_critical_is_nats(self) -> None:
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        assert isinstance(bus.critical_bus, NatsEventBus)

    def test_hybrid_backend_observer_is_in_memory(self) -> None:
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        assert isinstance(bus.observer_bus, InMemoryEventBus)

    def test_hybrid_observer_does_not_double_write_event_store(self) -> None:
        """observer_bus 必须把 event_store 设为 None：critical_bus 已经
        负责写 event_store，observer 重复写会造成事件重复。"""
        sentinel = object()
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=sentinel,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        observer = bus.observer_bus
        assert isinstance(observer, InMemoryEventBus)
        assert observer._event_store is None  # type: ignore[attr-defined]
        # critical 必须拿到 event_store
        critical = bus.critical_bus
        assert isinstance(critical, NatsEventBus)
        assert critical._event_store is sentinel  # type: ignore[attr-defined]

    def test_hybrid_backend_uses_default_routing(self) -> None:
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        assert bus.routing.critical_topics == DEFAULT_CRITICAL_TOPICS


class TestConstructEventBusNats:
    """nats backend 必须返回纯 NatsEventBus（Stage 5+ 全量切换形态）。"""

    def test_nats_backend_returns_nats_bus(self) -> None:
        settings = _paper_settings(
            event_bus_backend="nats",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, NatsEventBus)
        assert not isinstance(bus, HybridEventBus)


# ─────────────────────────────────────────────────────────────────────
# NatsBusConfig 字段映射
# ─────────────────────────────────────────────────────────────────────


class TestNatsConfigFieldMapping:
    """settings.nats_* 字段必须正确流入 NatsBusConfig。"""

    def test_nats_url_becomes_single_element_tuple(self) -> None:
        settings = _paper_settings(
            event_bus_backend="nats",
            nats_url="nats://nats-server.internal:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, NatsEventBus)
        assert bus._config.servers == ("nats://nats-server.internal:4222",)  # type: ignore[attr-defined]

    def test_nats_stream_name_propagated(self) -> None:
        settings = _paper_settings(
            event_bus_backend="nats",
            nats_url="nats://127.0.0.1:4222",
            nats_stream_name="AATS_TEST_STREAM",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, NatsEventBus)
        assert bus._config.stream_name == "AATS_TEST_STREAM"  # type: ignore[attr-defined]

    def test_nats_stream_max_age_propagated(self) -> None:
        # 30 天保留
        thirty_days = 30 * 24 * 60 * 60
        settings = _paper_settings(
            event_bus_backend="nats",
            nats_url="nats://127.0.0.1:4222",
            nats_stream_max_age_seconds=thirty_days,
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, NatsEventBus)
        assert bus._config.stream_max_age_seconds == float(thirty_days)  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "process_role,expected_consumer_role",
        [
            (None, "monolith"),
            ("monolith", "monolith"),
            ("gateway", "gateway"),
            ("market", "market"),
            ("decision", "decision"),
            ("execution", "execution"),
        ],
    )
    def test_consumer_role_derived_from_process_role(
        self, process_role: str | None, expected_consumer_role: str
    ) -> None:
        """consumer_role 决定 JetStream durable name 派生路径——
        必须按 process_role 区分，否则 4 进程会共用同一个 consumer，
        互相吃对方的消息。"""
        settings = _paper_settings(
            event_bus_backend="nats",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=process_role,
        )
        assert isinstance(bus, NatsEventBus)
        assert bus._consumer_role == expected_consumer_role  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────
# 生命周期：_start_event_bus
# ─────────────────────────────────────────────────────────────────────


class TestStartEventBusLifecycle(unittest.IsolatedAsyncioTestCase):
    """_start_event_bus 的语义：
    - InMemoryEventBus 没有 start → no-op
    - NatsEventBus / HybridEventBus 有 start → 调用之
    """

    async def test_start_in_memory_bus_is_noop(self) -> None:
        bus = InMemoryEventBus(event_store=None, persistence_mode="strict")
        # 不应抛错，且不需要任何 mock
        await _start_event_bus(bus)

    async def test_start_nats_bus_calls_start_method(self) -> None:
        """_start_event_bus 必须调用 NatsEventBus.start。"""
        settings = _paper_settings(
            event_bus_backend="nats",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, NatsEventBus)
        # 替换 start 防止真的去连 NATS
        bus.start = AsyncMock()  # type: ignore[method-assign]
        await _start_event_bus(bus)
        bus.start.assert_awaited_once()

    async def test_start_hybrid_bus_delegates_to_critical_without_topics(self) -> None:
        """HybridEventBus.start 必须调用 critical_bus.start **不传 topics** 参数。

        slice nats-capacity 变更（§7.5a R2）：之前 HybridEventBus.start 会把
        sorted(critical_topics) 作为 topics= 透传 → 走 legacy shim 路径 → 把所有
        critical topic 塞进单条 stream，正是 MARKET 高频挤爆 stream 的直接原因。

        新语义：runtime 路径走 NatsBusConfig.streams，通过 ensure_streams() 做
        多 stream upsert。HybridEventBus.start 不再传 topics —— 本测试锁死这个
        行为，防止未来误改回去。
        """
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        # 替换底层 critical_bus.start 防止真的去连 NATS
        critical_start = AsyncMock()
        bus.critical_bus.start = critical_start  # type: ignore[method-assign,union-attr]
        await _start_event_bus(bus)
        critical_start.assert_awaited_once()
        # slice nats-capacity: 必须**不传** topics（走新的 ensure_streams 路径）
        call_kwargs = critical_start.call_args.kwargs
        call_args = critical_start.call_args.args
        assert "topics" not in call_kwargs, (
            f"HybridEventBus.start 不应该再给 critical_start 传 topics, "
            f"但是收到 kwargs={call_kwargs}。slice nats-capacity §7.5a R2。"
        )
        assert call_args == (), (
            f"HybridEventBus.start 不应该给 critical_start 传位置参数, "
            f"但是收到 args={call_args}"
        )

    async def test_hybrid_close_delegates_to_both_buses(self) -> None:
        """HybridEventBus.close 必须 best-effort 关闭 critical 和 observer。"""
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        critical_close = AsyncMock()
        bus.critical_bus.close = critical_close  # type: ignore[method-assign,union-attr]
        # observer 是 InMemoryEventBus，没有 close —— 通过 monkey-patch 加一个
        observer_close = AsyncMock()
        bus.observer_bus.close = observer_close  # type: ignore[method-assign,union-attr]
        await bus.close()
        critical_close.assert_awaited_once()
        observer_close.assert_awaited_once()

    async def test_hybrid_close_reports_failure_after_attempting_both(self) -> None:
        """单条失败不阻止另一条清理，但最终必须向 lifecycle 报告失败。"""
        settings = _paper_settings(
            event_bus_backend="hybrid",
            nats_url="nats://127.0.0.1:4222",
        )
        bus = _construct_event_bus(
            runtime_settings=settings,
            event_store=None,
            process_role=None,
        )
        assert isinstance(bus, HybridEventBus)
        bus.critical_bus.close = AsyncMock(  # type: ignore[method-assign,union-attr]
            side_effect=RuntimeError("simulated nats drain failure")
        )
        observer_close = AsyncMock()
        bus.observer_bus.close = observer_close  # type: ignore[method-assign,union-attr]
        with pytest.raises(
            RuntimeError,
            match="hybrid_bus_close_failed:critical",
        ) as raised:
            await bus.close()
        observer_close.assert_awaited_once()
        assert raised.value.__cause__ is None
