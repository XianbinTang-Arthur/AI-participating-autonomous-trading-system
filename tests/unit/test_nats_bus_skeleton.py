"""Stage 4 单元测试：NATS event bus 骨架。

只覆盖不需要 NATS server 的部分：
1. 配置/路由 helper
2. HybridEventBus 在两个 in-memory 后端之间正确分发
3. NatsEventBus 在没有 nats-py / 没 connect() 时 publish/subscribe 应当抛错

不测的部分（需要 docker 起 NATS server，留到 Stage 4 集成测试）：
- NATS 实际连接、JetStream 持久化、durable consumer 重启续传
"""
from __future__ import annotations

import asyncio
import dataclasses
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

import pytest

from aats.bus.base import EventBus, MessageHandler
from aats.bus.nats_bus import (
    DEFAULT_AATS_EVENTS_COMMANDS_SPEC,
    DEFAULT_AATS_EVENTS_MARKET_SPEC,
    DEFAULT_AATS_EVENTS_SPEC,
    DEFAULT_CRITICAL_EVENTS_TOPICS,
    DEFAULT_CRITICAL_TOPICS,
    DEFAULT_MARKET_STREAM_TOPICS,
    DEFAULT_OBSERVER_TOPICS,
    DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS,
    DEFAULT_STREAM_SPECS,
    SNAPSHOT_DELIVERY_TOPICS,
    TRANSIENT_DELIVERY_TOPICS,
    ConsumerConfigSpec,
    DeliverPolicyStr,
    DeliverySemantics,
    HybridBusRouting,
    HybridEventBus,
    NatsBusConfig,
    NatsDeliveryGate,
    NatsEventBus,
    StreamSpec,
    UnroutedTopicError,
    _compute_stream_config_drift,
    build_consumer_config_spec,
    build_nats_streams_from_env,
    consumer_mutable_config_migration_blockers,
    delivery_semantics_for,
)
from aats.events import topics as _topics
from aats.schemas.common import EventEnvelope


# ─────────────────────────────────────────────────────────────────────
# 测试用 fake EventBus
# ─────────────────────────────────────────────────────────────────────


class FakeBus(EventBus):
    """记录所有 publish / subscribe 调用的内存 fake。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.published: list[tuple[str, str, dict[str, Any]]] = []
        self.subscriptions: dict[str, list[MessageHandler]] = defaultdict(list)
        self.envelope_published: list[EventEnvelope] = []

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        self.published.append((topic, key, payload))

    async def publish_envelope(
        self,
        envelope: EventEnvelope,
        *,
        persist: bool = True,
    ) -> None:
        self.envelope_published.append(envelope)

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self.subscriptions[topic].append(handler)


def _make_envelope(topic: str, key: str = "k1") -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_type": "test_event",
            "source_component": "unit_test",
            "topic": topic,
            "key": key,
            "payload": {"value": 42},
        }
    )


# ─────────────────────────────────────────────────────────────────────
# NatsBusConfig
# ─────────────────────────────────────────────────────────────────────


def test_subject_for_prefixes_topic() -> None:
    cfg = NatsBusConfig()
    assert cfg.subject_for("decisions") == "aats.decisions"
    assert cfg.subject_for("execution_intents") == "aats.execution_intents"


def test_durable_name_includes_role_and_topic() -> None:
    cfg = NatsBusConfig()
    name = cfg.durable_name_for("decision", "execution_intents")
    assert "decision" in name
    assert "execution_intents" in name
    assert name.startswith("aats-")


def test_durable_name_sanitizes_dots_and_spaces() -> None:
    cfg = NatsBusConfig()
    name = cfg.durable_name_for("execution", "ui.event pings")
    assert "." not in name
    assert " " not in name


def test_critical_observer_disjoint() -> None:
    """同一个 topic 不应同时出现在 critical 和 observer 集合里。"""
    overlap = DEFAULT_CRITICAL_TOPICS & DEFAULT_OBSERVER_TOPICS
    assert overlap == frozenset()


def test_critical_topics_cover_core_event_flow() -> None:
    """关键事件流（AI 决策、订单、成交、风控、对账）必须在 critical 集合中。

    用 aats.events.topics 模块的真实常量（dotted name），不是 5c 之前的字面量。
    这是 Stage 4 隐患修复的核心断言：路由表必须用真实 topic 名才能生效。
    """
    must_have = {
        _topics.AI_DECISION_BRIEFS,
        _topics.ORDER_INTENTS,
        _topics.ORDER_UPDATES,
        _topics.FILL_EVENTS,
        _topics.RISK_DECISIONS,
        _topics.RECONCILIATION_REPORTS,
    }
    assert must_have.issubset(DEFAULT_CRITICAL_TOPICS), (
        f"以下核心 topic 缺失: {must_have - DEFAULT_CRITICAL_TOPICS}"
    )


def test_all_topics_module_constants_are_routed() -> None:
    """枚举 aats.events.topics 模块全部 module-level 常量，每条都必须被归类。

    Why: 这是 5c 路由表 bug 的根本预防——只要将来有人加新 topic 到 topics.py
    但忘记加到 critical / observer 集合里，本测试会立刻失败，迫使他们补上。
    """
    declared = {
        value
        for name, value in vars(_topics).items()
        if isinstance(value, str)
        and not name.startswith("_")
        and name.isupper()
    }
    routed = DEFAULT_CRITICAL_TOPICS | DEFAULT_OBSERVER_TOPICS
    missing = declared - routed
    assert not missing, (
        f"以下 topic 在 aats/events/topics.py 中声明但未被归类:"
        f" {sorted(missing)}\n"
        f" 请加到 aats/bus/nats_bus.py 的 DEFAULT_CRITICAL_TOPICS 或"
        f" DEFAULT_OBSERVER_TOPICS 中（每条加一句 inline 注释说明归类理由）。"
    )


# ─────────────────────────────────────────────────────────────────────
# HybridBusRouting
# ─────────────────────────────────────────────────────────────────────


def test_routing_critical_topic() -> None:
    """真实 topic 常量必须路由到 critical。"""
    routing = HybridBusRouting()
    assert routing.route_for(_topics.ORDER_INTENTS) == "critical"
    assert routing.route_for(_topics.FILL_EVENTS) == "critical"
    assert routing.route_for(_topics.RISK_DECISIONS) == "critical"
    assert routing.route_for(_topics.AI_DECISION_BRIEFS) == "critical"


def test_routing_observer_topic() -> None:
    """真实 observer topic 常量必须路由到 observer。"""
    routing = HybridBusRouting()
    assert routing.route_for(_topics.HEALTH_SNAPSHOTS) == "observer"
    assert routing.route_for(_topics.BLOCKER_SNAPSHOTS) == "observer"
    assert routing.route_for(_topics.STRATEGY_PROFILE_EVALUATIONS) == "observer"


def test_cross_process_topics_not_in_observer() -> None:
    """跨进程消费的 topic 必须在 critical，不能在 observer（防止重犯 guard signal bug）。"""
    routing = HybridBusRouting()
    # AI_PERFORMANCE_REPORTS: decision→gateway，曾误放 observer
    assert routing.route_for(_topics.AI_PERFORMANCE_REPORTS) == "critical"
    # STRATEGY_PROFILE_OPTIMIZATION_REPORTS: decision→gateway，曾误放 observer
    assert routing.route_for(_topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS) == "critical"
    # GUARD_SIGNAL_UPDATES: execution→decision，最早发现的同类 bug
    assert routing.route_for(_topics.GUARD_SIGNAL_UPDATES) == "critical"


def test_routing_unknown_raises_unrouted_topic_error() -> None:
    """5c 严格模式：未分类 topic 默认抛 UnroutedTopicError，不再 silent fallback。

    Why: Stage 4 时代默认 default_route='critical'，路由表错位被 fallback
    蒙混过关。5c 修复后默认 None，强制开发者显式归类。
    """
    routing = HybridBusRouting()
    with pytest.raises(UnroutedTopicError, match="brand_new_topic"):
        routing.route_for("brand_new_topic")


def test_routing_unknown_explicit_critical_default_falls_back_to_critical() -> None:
    """API 兼容：显式构造 default_route='critical' 时仍可 fallback（老语义）。"""
    routing = HybridBusRouting(default_route="critical")
    assert routing.route_for("brand_new_topic") == "critical"


def test_routing_unknown_explicit_observer_default_falls_back_to_observer() -> None:
    """API 兼容：显式构造 default_route='observer' 时 fallback 到内存。"""
    routing = HybridBusRouting(default_route="observer")
    assert routing.route_for("brand_new_topic") == "observer"


# ─────────────────────────────────────────────────────────────────────
# HybridEventBus 分发
# ─────────────────────────────────────────────────────────────────────


def test_hybrid_publish_critical_routes_to_critical_bus() -> None:
    critical = FakeBus("critical")
    observer = FakeBus("observer")
    bus = HybridEventBus(critical_bus=critical, observer_bus=observer)
    asyncio.run(
        bus.publish(
            _topics.ORDER_INTENTS,
            "k1",
            {
                "event_type": "x",
                "source_component": "t",
                "topic": _topics.ORDER_INTENTS,
                "key": "k1",
                "payload": {},
            },
        )
    )
    assert len(critical.published) == 1
    assert len(observer.published) == 0


def test_hybrid_publish_observer_routes_to_observer_bus() -> None:
    critical = FakeBus("critical")
    observer = FakeBus("observer")
    bus = HybridEventBus(critical_bus=critical, observer_bus=observer)
    asyncio.run(
        bus.publish(
            _topics.HEALTH_SNAPSHOTS,
            "k1",
            {
                "event_type": "x",
                "source_component": "t",
                "topic": _topics.HEALTH_SNAPSHOTS,
                "key": "k1",
                "payload": {},
            },
        )
    )
    assert len(critical.published) == 0
    assert len(observer.published) == 1


def test_hybrid_subscribe_critical_routes_to_critical_bus() -> None:
    critical = FakeBus("critical")
    observer = FakeBus("observer")
    bus = HybridEventBus(critical_bus=critical, observer_bus=observer)

    async def _h(_: dict) -> None:
        pass

    asyncio.run(bus.subscribe(_topics.ORDER_INTENTS, _h))
    assert _topics.ORDER_INTENTS in critical.subscriptions
    assert _topics.ORDER_INTENTS not in observer.subscriptions


def test_hybrid_subscribe_observer_routes_to_observer_bus() -> None:
    critical = FakeBus("critical")
    observer = FakeBus("observer")
    bus = HybridEventBus(critical_bus=critical, observer_bus=observer)

    async def _h(_: dict) -> None:
        pass

    asyncio.run(bus.subscribe(_topics.HEALTH_SNAPSHOTS, _h))
    assert _topics.HEALTH_SNAPSHOTS not in critical.subscriptions
    assert _topics.HEALTH_SNAPSHOTS in observer.subscriptions


def test_hybrid_publish_envelope_uses_envelope_method_when_available() -> None:
    critical = FakeBus("critical")
    observer = FakeBus("observer")
    bus = HybridEventBus(critical_bus=critical, observer_bus=observer)

    env = _make_envelope(_topics.ORDER_INTENTS)
    asyncio.run(bus.publish_envelope(env))
    assert critical.envelope_published == [env]


def test_hybrid_publish_envelope_falls_back_to_plain_publish() -> None:
    """如果底层 bus 没有 publish_envelope（罕见），退回到 publish。"""

    class MinimalBus(EventBus):
        def __init__(self) -> None:
            self.published: list[tuple[str, str, dict[str, Any]]] = []

        async def publish(self, topic: str, key: str, payload: dict) -> None:
            self.published.append((topic, key, payload))

        async def subscribe(self, topic: str, handler: MessageHandler) -> None:
            pass

    minimal = MinimalBus()
    bus = HybridEventBus(critical_bus=minimal, observer_bus=FakeBus("observer"))
    env = _make_envelope(_topics.ORDER_INTENTS, key="abc")
    asyncio.run(bus.publish_envelope(env))
    assert len(minimal.published) == 1
    assert minimal.published[0][0] == _topics.ORDER_INTENTS
    assert minimal.published[0][1] == "abc"


# ─────────────────────────────────────────────────────────────────────
# NatsEventBus 未连接的防御性行为
# ─────────────────────────────────────────────────────────────────────


def test_nats_bus_publish_before_connect_raises() -> None:
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")
    env = _make_envelope("decisions")
    with pytest.raises(RuntimeError, match="before connect"):
        asyncio.run(bus.publish_envelope(env))


def test_nats_bus_subscribe_before_connect_raises() -> None:
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")

    async def _h(_: dict) -> None:
        pass

    with pytest.raises(RuntimeError, match="before connect"):
        asyncio.run(bus.subscribe("decisions", _h))


def test_nats_bus_ensure_stream_before_connect_raises() -> None:
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")
    with pytest.raises(RuntimeError, match="before connect"):
        asyncio.run(bus.ensure_streams())


def test_nats_bus_construction_does_not_require_nats_py() -> None:
    """构造 NatsEventBus 不应触发 nats-py import；只有 connect() 才会。"""
    # 这里不真的卸载 nats，但要确保 __init__ 是 pure-python，可以被
    # monolith 安全 import
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="monolith")
    assert bus._connected is False
    assert bus._client is None
    assert bus._js is None


def test_nats_connect_failure_closes_partial_client_without_masking_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_error = RuntimeError("primary connect failure")

    class _FakeNatsClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def connect(self, **_kwargs: Any) -> None:
            raise primary_error

        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secondary cleanup failure")

    async def run() -> None:
        from nats.aio import client as nats_client_module

        monkeypatch.setattr(nats_client_module, "Client", lambda: client)
        bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")

        with pytest.raises(RuntimeError) as exc_info:
            await bus.connect()

        assert exc_info.value is primary_error
        assert client.close_calls == 1
        assert bus._client is None
        assert bus._js is None
        assert bus._connected is False

    client = _FakeNatsClient()
    asyncio.run(run())


def test_nats_jetstream_initialization_failure_closes_connected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_error = RuntimeError("primary jetstream failure")

    class _FakeNatsClient:
        def __init__(self) -> None:
            self.connect_calls = 0
            self.close_calls = 0

        async def connect(self, **_kwargs: Any) -> None:
            self.connect_calls += 1

        def jetstream(self) -> Any:
            raise primary_error

        async def close(self) -> None:
            self.close_calls += 1

    async def run() -> None:
        from nats.aio import client as nats_client_module

        monkeypatch.setattr(nats_client_module, "Client", lambda: client)
        bus = NatsEventBus(config=NatsBusConfig(), consumer_role="execution")

        with pytest.raises(RuntimeError) as exc_info:
            await bus.connect()

        assert exc_info.value is primary_error
        assert client.connect_calls == 1
        assert client.close_calls == 1
        assert bus._client is None
        assert bus._js is None
        assert bus._connected is False

    client = _FakeNatsClient()
    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# _on_error 分级：连接瞬态 → WARNING；未知错误 → ERROR
# 背景：deploy 重启 NATS 容器时 4 个 app 服务几乎同时收到 EOF +
# ConnectionRefused，如果都打 ERROR 会误触发 sev3-error-rate 告警
# （15m 内 >5 条 ERROR）。见 2026-04-23 问题报告。
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: ConnectionRefusedError(111, "Connect call failed"),
        lambda: ConnectionResetError("peer reset"),
        lambda: BrokenPipeError("pipe"),
        lambda: TimeoutError("handshake timeout"),
        lambda: OSError("sock"),
        # 模拟 nats-py 自己抛的 UnexpectedEOF (名字匹配即可)
        lambda: type("UnexpectedEOF", (Exception,), {})("eof"),
        lambda: type("ConnectionClosedError", (Exception,), {})("closed"),
        lambda: type("NoServersError", (Exception,), {})("no servers"),
    ],
)
def test_on_error_transient_logged_as_warning(
    caplog: pytest.LogCaptureFixture,
    exc_factory,
) -> None:
    """重连瞬态 (UnexpectedEOF/ConnectionRefused/... 白名单) → WARNING，
    不会计入 sev3-error-rate 告警。"""
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="test")
    caplog.set_level("DEBUG", logger=bus.logger.name)
    caplog.clear()

    asyncio.run(bus._on_error(exc_factory()))

    records = [r for r in caplog.records if r.__dict__.get("event_name") == "nats_client_error"]
    assert len(records) == 1, f"expected 1 nats_client_error, got {len(records)}"
    assert records[0].levelname == "WARNING", (
        f"transient NATS error should log WARNING, got {records[0].levelname}: {records[0].getMessage()}"
    )
    assert "transient=True" in records[0].getMessage()


def test_on_error_unknown_still_logged_as_error(caplog: pytest.LogCaptureFixture) -> None:
    """未登记在白名单的异常 → 保持 ERROR，保护真故障信号。"""
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="test")
    caplog.set_level("DEBUG", logger=bus.logger.name)
    caplog.clear()

    asyncio.run(bus._on_error(ValueError("something unexpected")))

    records = [r for r in caplog.records if r.__dict__.get("event_name") == "nats_client_error"]
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert "transient=False" in records[0].getMessage()


# ─────────────────────────────────────────────────────────────────────
# M1: ConsumerConfigSpec — NatsBusConfig 真的流到 subscribe 路径上
# ─────────────────────────────────────────────────────────────────────


def test_consumer_config_spec_uses_default_nats_bus_config() -> None:
    """默认 NatsBusConfig 派生出的 spec 字段必须等于默认值。"""
    config = NatsBusConfig()
    spec = build_consumer_config_spec(config=config, durable="aats-decision-decisions")
    assert isinstance(spec, ConsumerConfigSpec)
    assert spec.durable_name == "aats-decision-decisions"
    assert spec.ack_wait_seconds == config.ack_wait_seconds == 30.0
    assert spec.max_ack_pending == config.max_ack_pending == 256
    assert spec.max_deliver == config.max_deliver == 5


def test_consumer_config_spec_propagates_custom_nats_bus_config() -> None:
    """覆写 NatsBusConfig 的反压参数后，spec 必须用新值。

    这是 M1 的核心断言：如果未来有人删掉 subscribe() 里的
    build_consumer_config_spec 调用，这个测试会立即报错。
    """
    config = NatsBusConfig(
        ack_wait_seconds=45.0,
        max_ack_pending=128,
        max_deliver=3,
    )
    spec = build_consumer_config_spec(config=config, durable="aats-execution-execution_orders")
    assert spec.ack_wait_seconds == 45.0
    assert spec.max_ack_pending == 128
    assert spec.max_deliver == 3
    assert spec.durable_name == "aats-execution-execution_orders"


def test_consumer_config_spec_is_frozen() -> None:
    """spec 是 frozen dataclass，确保订阅参数派生后不可被意外篡改。"""
    spec = build_consumer_config_spec(
        config=NatsBusConfig(),
        durable="aats-decision-decisions",
    )
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        spec.ack_wait_seconds = 999.0  # type: ignore[misc]


@pytest.mark.parametrize(
    (
        "semantics",
        "current_ack_wait",
        "target_ack_wait",
        "current_max_deliver",
        "target_max_deliver",
        "expected",
    ),
    (
        ("event", 30.0, 30.0, 5, 5, ()),
        ("event", 29.0, 30.0, 5, 5, ("ack_wait_drift",)),
        ("event", 31.0, 30.0, 5, 5, ("ack_wait_drift",)),
        ("snapshot", 30.0, 90.0, 5, 5, ()),
        ("transient", 30.0, 90.0, 5, 5, ()),
        ("snapshot", 90.0, 30.0, 5, 5, ("ack_wait_drift",)),
        ("snapshot", 0.0, 90.0, 5, 5, ("ack_wait_drift",)),
        ("snapshot", -1.0, 90.0, 5, 5, ("ack_wait_drift",)),
        ("snapshot", float("nan"), 90.0, 5, 5, ("ack_wait_drift",)),
        ("snapshot", float("inf"), 90.0, 5, 5, ("ack_wait_drift",)),
        ("snapshot", 30.0, 90.0, 6, 5, ("max_deliver_drift",)),
        (
            "event",
            29.0,
            30.0,
            6,
            5,
            ("ack_wait_drift", "max_deliver_drift"),
        ),
    ),
)
def test_consumer_mutable_config_migration_policy_is_fail_closed(
    semantics: str,
    current_ack_wait: object,
    target_ack_wait: object,
    current_max_deliver: object,
    target_max_deliver: object,
    expected: tuple[str, ...],
) -> None:
    assert consumer_mutable_config_migration_blockers(
        delivery_semantics=semantics,
        current_ack_wait_seconds=current_ack_wait,
        target_ack_wait_seconds=target_ack_wait,
        current_max_deliver=current_max_deliver,
        target_max_deliver=target_max_deliver,
    ) == expected


# ─────────────────────────────────────────────────────────────────────
# M2: stream_max_age_seconds 可配置
# ─────────────────────────────────────────────────────────────────────


def test_stream_max_age_default_is_one_day() -> None:
    """默认 max_age = 1 天（2026-04-20 从 7 天改 1 天，参见
    docs/task/aats_events_stream_retention_root_fix_sow.md ——
    长期合规/回放由 PG event_store 承担，NATS 仅作 hot buffer）。

    单位校对：NatsBusConfig 持秒；nats-py StreamConfig.max_age 也以秒为单位，
    内部 _to_nanoseconds() 自行换算，调用方不要再预乘 1e9 —— 否则会被双重换算成超大整数，
    触发 NATS server "invalid JSON" 拒绝。"""
    config = NatsBusConfig()
    assert config.stream_max_age_seconds == 24 * 60 * 60


def test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds() -> None:
    """回归测试：ensure_stream 必须把 stream_max_age_seconds 原样（秒）
    传给 nats-py StreamConfig.max_age，**不能**预先乘 1e9。

    Why: nats-py 2.14 文档明确 max_age 字段以秒为单位
    （nats/js/api.py: ``max_age: Optional[float] = None  # in seconds``），
    内部 _to_nanoseconds() 自行换算。早期实现错把秒预乘 1e9 后再传,
    导致 nats-py 又乘一次 1e9，最终发出 60_000_000_000_000_000_000 这种
    超大值，NATS server JSON parser 直接 reject 'invalid JSON'。
    集成测试发现这个 bug 后补的回归保护。

    Slice 6.5 说明
    ==============
    ensure_stream 现在是 idempotent upsert：先 stream_info，再按分支决定
    add_stream / update_stream / 什么都不做。本测试 stub stream_info 抛
    NotFoundError，强制走 add_stream 分支，保留原断言语义。
    """
    pytest.importorskip("nats", reason="nats-py 未安装；本回归只在装了 nats-integration extras 的环境下跑")

    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    from nats.js.errors import NotFoundError  # type: ignore[import-not-found]

    bus = NatsEventBus(
        config=NatsBusConfig(stream_max_age_seconds=3600),
        consumer_role="test",
    )
    # 绕过真 connect()：直接装一个 fake JetStream context
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    # slice nats-capacity: ensure_stream 现在是 legacy shim，会发 DeprecationWarning
    with pytest.warns(DeprecationWarning, match="ensure_streams"):
        _asyncio.run(bus.ensure_stream(topics=["decisions"]))

    fake_js.add_stream.assert_awaited_once()
    fake_js.update_stream.assert_not_awaited()
    cfg = fake_js.add_stream.await_args.kwargs["config"]
    # 关键断言：是 3600（秒），不是 3600 * 1e9（纳秒）
    assert cfg.max_age == 3600, (
        f"ensure_stream 把 max_age 传成了 {cfg.max_age}，期望 3600 秒。"
        " 看起来又把秒预乘了 1e9 —— 见 docstring 的 Why。"
    )


# ─────────────────────────────────────────────────────────────────────
# Slice 6.5 fix: ensure_stream idempotent upsert
#
# Why: 原实现直接 add_stream 非幂等 —— stream 已存在且 subjects 不同时
# 抛 BadRequestError 10058 "stream name already in use with a different
# configuration"。Slice 6.5 新增 OBLIGATION_UPDATES topic 把 subject 数
# 从 N 变成 N+1，直接让所有跑旧 stream 的容器启动失败。
#
# 修复后的三分支矩阵：
#   1. stream 不存在  → add_stream      → "created" 日志
#   2. 已有 = 新      → 什么都不做      → "unchanged" 日志
#   3. 已有 ≠ 新      → update_stream   → "updated" 日志 + diff
# ─────────────────────────────────────────────────────────────────────


def test_ensure_stream_creates_when_stream_missing() -> None:
    """NotFoundError → add_stream 路径。"""
    pytest.importorskip(
        "nats",
        reason="nats-py 未安装；本回归只在装了 nats-integration extras 的环境下跑",
    )

    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    from nats.js.errors import NotFoundError  # type: ignore[import-not-found]

    bus = NatsEventBus(
        config=NatsBusConfig(stream_name="AATS_EVENTS_TEST"),
        consumer_role="test",
    )
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    # slice nats-capacity: ensure_stream 现在是 legacy shim
    with pytest.warns(DeprecationWarning, match="ensure_streams"):
        _asyncio.run(bus.ensure_stream(topics=["decisions", "execution.order_intents"]))

    fake_js.stream_info.assert_awaited_once_with("AATS_EVENTS_TEST")
    fake_js.add_stream.assert_awaited_once()
    fake_js.update_stream.assert_not_awaited()

    cfg = fake_js.add_stream.await_args.kwargs["config"]
    assert set(cfg.subjects) == {"aats.decisions", "aats.execution.order_intents"}


def test_ensure_stream_unchanged_when_subjects_match() -> None:
    """已有 subjects == 新 subjects → 不应该 add_stream 也不应该 update_stream。

    这是 dev 环境常见的 hot restart 场景：进程重启但 stream 没变，应该
    一次 stream_info 就 return，避免浪费 server round-trip。
    """
    pytest.importorskip(
        "nats",
        reason="nats-py 未安装；本回归只在装了 nats-integration extras 的环境下跑",
    )

    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(stream_name="AATS_EVENTS_TEST"),
        consumer_role="test",
    )
    # fake StreamInfo：.config 所有字段都必须和 legacy shim 默认容量完全相等，
    # 否则 _compute_stream_config_drift 会报 drift → 走 update_stream 分支。
    # legacy shim 容量默认（见 NatsEventBus.ensure_stream docstring）：
    #   max_age = config.stream_max_age_seconds = 24 * 3600（2026-04-20 改 1 天，
    #             原 7 * 24 * 3600，参见
    #             docs/task/aats_events_stream_retention_root_fix_sow.md）
    #   max_bytes = 128 MB
    #   max_msgs = 10_000
    #   max_msg_size = 4 MB
    #   num_replicas = 1 (StreamSpec 默认)
    #   duplicate_window = 120.0 (StreamSpec 默认)
    #   deny_purge = False (StreamSpec 默认)
    fake_info = MagicMock()
    fake_info.config.subjects = [
        "aats.execution.order_intents",
        "aats.decisions",
    ]
    fake_info.config.max_age = 24 * 60 * 60
    fake_info.config.max_bytes = 128 * 1024 * 1024
    fake_info.config.max_msgs = 10_000
    fake_info.config.max_msg_size = 4 * 1024 * 1024
    fake_info.config.num_replicas = 1
    fake_info.config.duplicate_window = 120.0
    fake_info.config.deny_purge = False
    # B2a 加入 retention 字段 drift 检测；legacy shim 默认是 limits
    from nats.js.api import RetentionPolicy as _TestRetentionPolicy
    fake_info.config.retention = _TestRetentionPolicy.LIMITS
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(return_value=fake_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    # 传进去的 topics 顺序和 existing_subjects 不一样，但 set 相等
    # slice nats-capacity: ensure_stream 现在是 legacy shim
    with pytest.warns(DeprecationWarning, match="ensure_streams"):
        _asyncio.run(bus.ensure_stream(topics=["decisions", "execution.order_intents"]))

    fake_js.stream_info.assert_awaited_once_with("AATS_EVENTS_TEST")
    fake_js.add_stream.assert_not_awaited()
    fake_js.update_stream.assert_not_awaited()


def test_ensure_stream_updates_when_subjects_differ() -> None:
    """已有 subjects ≠ 新 subjects → update_stream 被调用，而不是 add_stream。

    这是 Slice 6.5 升级场景：OBLIGATION_UPDATES 新增到 DEFAULT_CRITICAL_TOPICS,
    全部容器第一次启动新镜像时，本来已经 persist 在 JetStream 的 AATS_EVENTS
    只缺这一个 subject，应该原地 upsert，而不是把进程烧死。
    """
    pytest.importorskip(
        "nats",
        reason="nats-py 未安装；本回归只在装了 nats-integration extras 的环境下跑",
    )

    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(stream_name="AATS_EVENTS_TEST"),
        consumer_role="test",
    )
    # fake existing stream：subjects 比新的少一个；其他容量字段补齐 legacy
    # shim 默认，隔离这个测试只断言 subjects drift（避免 max_bytes 等巧合
    # 触发 update 让断言含义变模糊）
    fake_info = MagicMock()
    fake_info.config.subjects = [
        "aats.decisions",
        "aats.execution.order_intents",
    ]
    fake_info.config.max_age = 7 * 24 * 60 * 60
    fake_info.config.max_bytes = 128 * 1024 * 1024
    fake_info.config.max_msgs = 10_000
    fake_info.config.max_msg_size = 4 * 1024 * 1024
    fake_info.config.num_replicas = 1
    fake_info.config.duplicate_window = 120.0
    fake_info.config.deny_purge = False
    # B2a 加入 retention 字段 drift 检测；legacy shim 默认是 limits
    from nats.js.api import RetentionPolicy as _TestRetentionPolicy
    fake_info.config.retention = _TestRetentionPolicy.LIMITS
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(return_value=fake_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    # slice nats-capacity: ensure_stream 现在是 legacy shim
    with pytest.warns(DeprecationWarning, match="ensure_streams"):
        _asyncio.run(
            bus.ensure_stream(
                topics=[
                    "decisions",
                    "execution.order_intents",
                    "execution.obligation_updates",  # 新增 topic
                ]
            )
        )

    fake_js.stream_info.assert_awaited_once_with("AATS_EVENTS_TEST")
    fake_js.add_stream.assert_not_awaited()
    fake_js.update_stream.assert_awaited_once()

    cfg = fake_js.update_stream.await_args.kwargs["config"]
    # update_stream 必须传完整的新 subjects 列表（nats-py update_stream 语义
    # 是 REPLACE，不是 patch —— subjects 少一个等于下线那个 subject）
    assert set(cfg.subjects) == {
        "aats.decisions",
        "aats.execution.order_intents",
        "aats.execution.obligation_updates",
    }


def test_ensure_stream_updates_when_subject_removed() -> None:
    """对称 case：已有 subjects 比新的多一个 → 也必须 update_stream。

    Why: set(existing) != set(new) 在两个方向上都必须触发 update，否则
    退役 topic 时遗留 subject 会一直留在 stream config 里（不致命，
    但会造成配置漂移）。
    """
    pytest.importorskip(
        "nats",
        reason="nats-py 未安装；本回归只在装了 nats-integration extras 的环境下跑",
    )

    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(stream_name="AATS_EVENTS_TEST"),
        consumer_role="test",
    )
    fake_info = MagicMock()
    fake_info.config.subjects = [
        "aats.decisions",
        "aats.execution.order_intents",
        "aats.retired_topic",  # 待下线
    ]
    fake_info.config.max_age = 7 * 24 * 60 * 60
    fake_info.config.max_bytes = 128 * 1024 * 1024
    fake_info.config.max_msgs = 10_000
    fake_info.config.max_msg_size = 4 * 1024 * 1024
    fake_info.config.num_replicas = 1
    fake_info.config.duplicate_window = 120.0
    fake_info.config.deny_purge = False
    # B2a 加入 retention 字段 drift 检测；legacy shim 默认是 limits
    from nats.js.api import RetentionPolicy as _TestRetentionPolicy
    fake_info.config.retention = _TestRetentionPolicy.LIMITS
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(return_value=fake_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    # slice nats-capacity: ensure_stream 现在是 legacy shim
    with pytest.warns(DeprecationWarning, match="ensure_streams"):
        _asyncio.run(bus.ensure_stream(topics=["decisions", "execution.order_intents"]))

    fake_js.add_stream.assert_not_awaited()
    fake_js.update_stream.assert_awaited_once()
    cfg = fake_js.update_stream.await_args.kwargs["config"]
    assert "aats.retired_topic" not in set(cfg.subjects)


def test_stream_max_age_can_be_overridden() -> None:
    """生产可调长，dev 可调短。"""
    long_term = NatsBusConfig(stream_max_age_seconds=30 * 24 * 60 * 60)
    short_term = NatsBusConfig(stream_max_age_seconds=3600)
    assert long_term.stream_max_age_seconds == 30 * 24 * 60 * 60
    assert short_term.stream_max_age_seconds == 3600


# ─────────────────────────────────────────────────────────────────────
# M3: ensure_stream 用 topic 名（不带前缀），不再要求调用方手写 subject
# ─────────────────────────────────────────────────────────────────────


def test_ensure_stream_signature_takes_topics_keyword() -> None:
    """ensure_stream 必须接 topics 关键字参数（不是 subjects）。

    这是 M3 的核心断言：如果有人改回 `subjects=` 接收完整 NATS subject 名，
    这个测试会立即失败，迫使他们重新审视前缀维护的复杂度。
    """
    import inspect

    sig = inspect.signature(NatsEventBus.ensure_stream)
    params = list(sig.parameters.keys())
    # self + topics
    assert "topics" in params
    assert "subjects" not in params


# ═════════════════════════════════════════════════════════════════════
# slice nats-capacity: StreamSpec 校验 + 分层 stream + ensure_streams
#
# 设计文档：docs/task/slice_nats_jetstream_capacity_fix_design.md
# 不变量 I-1 ~ I-11
# ═════════════════════════════════════════════════════════════════════


# ── Group A: StreamSpec 校验（8 tests） ──────────────────────────────


def _make_valid_stream_spec_kwargs() -> dict[str, Any]:
    """生成一个能构造成功的 StreamSpec 关键字参数，便于各测试按需 override。"""
    return {
        "name": "TEST_STREAM",
        "topics": frozenset({"decisions", "execution.order_intents"}),
        "max_age_seconds": 86_400,
        "max_bytes": 1_000_000,
        "max_msgs": 1_000,
        "max_msg_size": 1_024,
    }


def test_stream_spec_requires_screaming_snake_case_name() -> None:
    """name 必须是 SCREAMING_SNAKE_CASE（大写字母 + 下划线 + 数字）。"""
    for bad_name in ("lower_case", "Mixed_Case", "with-dash", "", "with space"):
        kwargs = _make_valid_stream_spec_kwargs()
        kwargs["name"] = bad_name
        with pytest.raises(ValueError, match="SCREAMING_SNAKE_CASE"):
            StreamSpec(**kwargs)
    # 合法 case 不应该抛
    StreamSpec(**{**_make_valid_stream_spec_kwargs(), "name": "AATS_EVENTS_2"})


def test_stream_spec_rejects_empty_topics() -> None:
    """topics 必须非空 —— 否则 stream 没有任何 subject，nats-py add_stream 会炸。"""
    kwargs = _make_valid_stream_spec_kwargs()
    kwargs["topics"] = frozenset()
    with pytest.raises(ValueError, match="must have at least one topic"):
        StreamSpec(**kwargs)


def test_stream_spec_rejects_non_positive_max_age() -> None:
    """max_age_seconds 必须 > 0。"""
    for bad_age in (0, -1, -3600.0):
        kwargs = _make_valid_stream_spec_kwargs()
        kwargs["max_age_seconds"] = bad_age
        with pytest.raises(ValueError, match="max_age_seconds must be positive"):
            StreamSpec(**kwargs)


def test_stream_spec_rejects_non_positive_max_bytes() -> None:
    """max_bytes 必须 > 0 —— 禁止传 -1 让服务器硬限裸奔（slice 核心病根）。"""
    for bad_bytes in (0, -1):
        kwargs = _make_valid_stream_spec_kwargs()
        kwargs["max_bytes"] = bad_bytes
        with pytest.raises(ValueError, match="max_bytes must be positive"):
            StreamSpec(**kwargs)


def test_stream_spec_rejects_non_positive_max_msgs() -> None:
    """max_msgs 必须 > 0 —— 同 max_bytes 原因，保险丝不能缺。"""
    for bad_msgs in (0, -1):
        kwargs = _make_valid_stream_spec_kwargs()
        kwargs["max_msgs"] = bad_msgs
        with pytest.raises(ValueError, match="max_msgs must be positive"):
            StreamSpec(**kwargs)


def test_stream_spec_rejects_non_positive_max_msg_size() -> None:
    """max_msg_size 必须 > 0。"""
    for bad_size in (0, -1):
        kwargs = _make_valid_stream_spec_kwargs()
        kwargs["max_msg_size"] = bad_size
        with pytest.raises(ValueError, match="max_msg_size must be positive"):
            StreamSpec(**kwargs)


def test_stream_spec_rejects_invalid_storage() -> None:
    """storage 只接受 'file' 或 'memory'。"""
    kwargs = _make_valid_stream_spec_kwargs()
    kwargs["storage"] = "s3"
    with pytest.raises(ValueError, match="storage must be 'file' or 'memory'"):
        StreamSpec(**kwargs)
    # 合法值不抛
    StreamSpec(**{**_make_valid_stream_spec_kwargs(), "storage": "file"})
    StreamSpec(**{**_make_valid_stream_spec_kwargs(), "storage": "memory"})


def test_stream_spec_rejects_num_replicas_less_than_one() -> None:
    """num_replicas >= 1（单节点 dev 下 = 1；集群可升）。"""
    kwargs = _make_valid_stream_spec_kwargs()
    kwargs["num_replicas"] = 0
    with pytest.raises(ValueError, match="num_replicas must be >= 1"):
        StreamSpec(**kwargs)


# ── Group B: 默认 spec 不变量（I-1, I-8, I-11）（4 tests） ──────────


def test_stream_specs_cover_all_critical_topics_exactly_once() -> None:
    """I-8'：DEFAULT_STREAM_SPECS topics 并集 ∪ DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS
    == DEFAULT_CRITICAL_TOPICS。

    每个 critical topic 必须恰好归属到某一处（不遗漏/不重复），否则 publish
    路由歧义或 silent drop：
      - 普通 critical topic → 某个 StreamSpec 的 topics 里（通过 NATS stream
        跨进程投递）
      - persist-only critical topic → DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS 里
        （publish_envelope 短路，只 event_store.append，不 js.publish）

    2026-04-20 不变量从 I-8（stream_topics 并集 == CRITICAL_TOPICS）升级为
    I-8'，加入 persist-only 类别。背景见
    docs/task/nats_retention_global_architecture_sow.md §B0。

    加新 critical topic 时：
      - 高频（≥ 1 Hz）且跨进程订阅 → DEFAULT_MARKET_STREAM_TOPICS
      - 跨进程订阅 → DEFAULT_CRITICAL_EVENTS_TOPICS（隐式，等于
        CRITICAL_TOPICS - MARKET_STREAM_TOPICS - PERSIST_ONLY）
      - 0 live NATS 订阅（只靠 PG 查询）→ DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS
    """
    all_spec_topics: set[str] = set()
    for spec in DEFAULT_STREAM_SPECS:
        # 检查 topic 不重复
        assert all_spec_topics.isdisjoint(spec.topics), (
            f"topic(s) {all_spec_topics & spec.topics} claimed by multiple streams"
        )
        all_spec_topics.update(spec.topics)

    # persist-only 不能同时出现在 stream 里（否则 js.publish 路径和短路路径
    # 会竞争：短路先 return，但 stream 里的 subject claim 依然存在，测试拓扑
    # 混乱）
    overlap = all_spec_topics & DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS
    assert not overlap, (
        f"persist-only topics must NOT appear in any stream spec: {overlap}"
    )

    # 并集 == DEFAULT_CRITICAL_TOPICS（I-8' 不变量）
    covered = all_spec_topics | DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS
    missing = DEFAULT_CRITICAL_TOPICS - covered
    extra = covered - DEFAULT_CRITICAL_TOPICS
    assert not missing, (
        f"critical topics not covered by any stream or persist-only: {missing}"
    )
    assert not extra, f"stream/persist-only claims non-critical topics: {extra}"


def test_persist_only_topics_are_removed_from_stream_subjects() -> None:
    """I-8' 配套：persist-only critical topic 不能出现在任何 StreamSpec 的
    subjects 里——否则 publish_envelope 的短路 return 会跳过 js.publish，
    但 stream 的 subject claim 仍然存在，导致 stream 路由元数据和实际行为
    不一致（审计上产生疑问）。

    2026-04-20 本测试锁定 persist-only topic 与 stream subjects 的互斥。
    """
    for persist_only_topic in DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS:
        for spec in DEFAULT_STREAM_SPECS:
            assert persist_only_topic not in spec.topics, (
                f"persist-only topic {persist_only_topic!r} leaked into "
                f"StreamSpec {spec.name}.topics — check "
                f"DEFAULT_CRITICAL_EVENTS_TOPICS computation"
            )


def test_stream_specs_market_and_events_have_symmetric_max_msg_size() -> None:
    """I-11 对称 max_msg_size：MARKET 和 EVENTS 两条 stream 单条消息上限
    必须相同，且等于 4 MB（对齐 server max_payload）。

    Why 对称：publish 一个 4 MB envelope 到 MARKET 应该成功，publish 同一个
    envelope 到 EVENTS 也应该成功；非对称会导致"只有某一条路径能传大 envelope"
    的奇怪语义 —— 调用方没法事先知道某个 topic 会到哪条 stream。
    """
    assert DEFAULT_AATS_EVENTS_MARKET_SPEC.max_msg_size == 4 * 1024 * 1024
    assert DEFAULT_AATS_EVENTS_SPEC.max_msg_size == 4 * 1024 * 1024


def test_total_stream_capacity_within_server_budget() -> None:
    """I-1 容量预算：3 条 stream max_bytes 之和 <= 6.5 GB（留 1.5 GB headroom，
    server max_file_store = 8 GB）。

    2026-04-20 B2a 新增 AATS_EVENTS_COMMANDS stream (512 MB)。预算：
      AATS_EVENTS_MARKET   2.0 GB
      AATS_EVENTS          4.0 GB  (INTEREST 实际稳态 < 0.5 GB)
      AATS_EVENTS_COMMANDS 0.5 GB
      合计                 6.5 GB
      server max_file_store 8 GB (nats-server.conf)
      headroom             1.5 GB

    加大 stream 容量时必须同步考虑 server 配置，否则一次过大的写入突袭会直接
    撞 server 硬限触发 10023（本 slice 修复的原病根）。
    """
    total = sum(spec.max_bytes for spec in DEFAULT_STREAM_SPECS)
    # 6.5 GB 上限
    max_budget = int(6.5 * 1024**3)
    assert total <= max_budget, (
        f"total stream capacity {total} bytes exceeds 6.5 GB budget ({max_budget})"
    )


def test_default_stream_specs_match_design_doc_capacities() -> None:
    """锁死设计文档 §4.3 的容量参数（任何后续调整都会让本测试红灯 → 迫使同步更新文档）。"""
    assert DEFAULT_AATS_EVENTS_MARKET_SPEC.name == "AATS_EVENTS_MARKET"
    assert DEFAULT_AATS_EVENTS_MARKET_SPEC.max_age_seconds == 86_400  # 1 天
    assert DEFAULT_AATS_EVENTS_MARKET_SPEC.max_bytes == 2 * 1024**3   # 2 GB

    assert DEFAULT_AATS_EVENTS_SPEC.name == "AATS_EVENTS"
    # 2026-04-20 从 7 天改 1 天，参见
    # docs/task/aats_events_stream_retention_root_fix_sow.md ——
    # NATS stream 是 hot buffer，长期合规/回放由 PG event_store 承担。
    assert DEFAULT_AATS_EVENTS_SPEC.max_age_seconds == 24 * 60 * 60  # 1 天
    assert DEFAULT_AATS_EVENTS_SPEC.max_bytes == 4 * 1024**3   # 4 GB（不动）

    # MARKET 只承载 MARKET_SNAPSHOTS + FEATURE_SNAPSHOTS
    assert DEFAULT_MARKET_STREAM_TOPICS == frozenset(
        {_topics.MARKET_SNAPSHOTS, _topics.FEATURE_SNAPSHOTS}
    )

    # EVENTS 承载其他所有 critical topic
    assert DEFAULT_AATS_EVENTS_SPEC.topics == DEFAULT_CRITICAL_EVENTS_TOPICS


# ── Group C: NatsBusConfig 拓扑校验（2 tests） ──────────────────────


def test_nats_bus_config_rejects_empty_streams() -> None:
    """streams 非空校验。"""
    with pytest.raises(ValueError, match="streams must be non-empty"):
        NatsBusConfig(streams=())


def test_nats_bus_config_rejects_topic_claimed_by_multiple_streams() -> None:
    """I-8 拓扑互斥：同一个 topic 不能被多条 stream 同时 claim。

    Why：两条 stream 都匹配同一个 subject，nats-py add_stream 会抛
    "subjects overlap" 运行时错；我们在 config 构造时就 fail-fast，
    让错误定位清晰到"两条 stream 定义冲突"而不是模糊的 nats error。
    """
    spec_a = StreamSpec(
        name="STREAM_A",
        topics=frozenset({"shared_topic", "only_a"}),
        max_age_seconds=3600,
        max_bytes=1_000_000,
        max_msgs=1_000,
        max_msg_size=1_024,
    )
    spec_b = StreamSpec(
        name="STREAM_B",
        topics=frozenset({"shared_topic", "only_b"}),  # shared_topic 同时在 A 和 B
        max_age_seconds=3600,
        max_bytes=1_000_000,
        max_msgs=1_000,
        max_msg_size=1_024,
    )
    with pytest.raises(ValueError, match="shared_topic.*claimed by both"):
        NatsBusConfig(streams=(spec_a, spec_b))


# ── Group D: build_nats_streams_from_env（4 tests） ─────────────────


def test_build_nats_streams_from_env_no_overrides_returns_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 env var 时返回 DEFAULT_STREAM_SPECS 不变。"""
    for var in (
        "AATS_NATS_MARKET_MAX_BYTES",
        "AATS_NATS_MARKET_MAX_MSGS",
        "AATS_NATS_MARKET_MAX_MSG_SIZE",
        "AATS_NATS_MARKET_MAX_AGE_SECONDS",
        "AATS_NATS_EVENTS_MAX_BYTES",
        "AATS_NATS_EVENTS_MAX_MSGS",
        "AATS_NATS_EVENTS_MAX_MSG_SIZE",
        "AATS_NATS_EVENTS_MAX_AGE_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)

    result = build_nats_streams_from_env(DEFAULT_STREAM_SPECS)
    assert result == DEFAULT_STREAM_SPECS


def test_build_nats_streams_from_env_applies_market_max_bytes_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖 MARKET max_bytes 生效，其他字段/其他 stream 不变。"""
    monkeypatch.setenv("AATS_NATS_MARKET_MAX_BYTES", "999999999")
    result = build_nats_streams_from_env(DEFAULT_STREAM_SPECS)
    market = next(s for s in result if s.name == "AATS_EVENTS_MARKET")
    events = next(s for s in result if s.name == "AATS_EVENTS")
    assert market.max_bytes == 999_999_999
    assert market.max_msgs == DEFAULT_AATS_EVENTS_MARKET_SPEC.max_msgs  # 未改
    assert events.max_bytes == DEFAULT_AATS_EVENTS_SPEC.max_bytes       # 未改


def test_build_nats_streams_from_env_applies_events_multi_field_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖 EVENTS 多个字段 + MARKET age 同时生效。"""
    monkeypatch.setenv("AATS_NATS_EVENTS_MAX_BYTES", "8589934592")    # 8 GB
    monkeypatch.setenv("AATS_NATS_EVENTS_MAX_MSGS", "10000000")       # 10M
    monkeypatch.setenv("AATS_NATS_EVENTS_MAX_MSG_SIZE", "8388608")    # 8 MB
    monkeypatch.setenv("AATS_NATS_MARKET_MAX_AGE_SECONDS", "172800")  # 2 天

    result = build_nats_streams_from_env(DEFAULT_STREAM_SPECS)
    market = next(s for s in result if s.name == "AATS_EVENTS_MARKET")
    events = next(s for s in result if s.name == "AATS_EVENTS")

    assert events.max_bytes == 8_589_934_592
    assert events.max_msgs == 10_000_000
    assert events.max_msg_size == 8_388_608
    assert market.max_age_seconds == 172_800


def test_build_nats_streams_from_env_rejects_invalid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env override 如果让 spec 校验不过（比如负数），应该原地 raise
    —— 不能 silently fall back 到 default（那样就失去了 override 的审计价值）。
    """
    monkeypatch.setenv("AATS_NATS_MARKET_MAX_BYTES", "-1")
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        build_nats_streams_from_env(DEFAULT_STREAM_SPECS)


# ── Group E: ensure_streams 多 stream 三分支（8 tests） ─────────────


def _make_fake_stream_info_from_spec(spec: StreamSpec, subject_prefix: str = "aats.") -> Any:
    """根据 StreamSpec 构造一个 FakeStreamInfo，字段全部匹配（走 unchanged 分支）。"""
    from unittest.mock import MagicMock
    from nats.js.api import RetentionPolicy as _RetentionPolicy
    info = MagicMock()
    info.config.subjects = [f"{subject_prefix}{t}" for t in sorted(spec.topics)]
    info.config.max_age = spec.max_age_seconds
    info.config.max_bytes = spec.max_bytes
    info.config.max_msgs = spec.max_msgs
    info.config.max_msg_size = spec.max_msg_size
    info.config.num_replicas = spec.num_replicas
    info.config.duplicate_window = spec.duplicate_window_seconds
    info.config.deny_purge = spec.deny_purge
    # B2a retention 字段加入 drift 检测，需要和 spec 保持一致才走 unchanged 分支
    retention_map = {
        "limits": _RetentionPolicy.LIMITS,
        "interest": _RetentionPolicy.INTEREST,
        "workqueue": _RetentionPolicy.WORK_QUEUE,
    }
    info.config.retention = retention_map[spec.retention]
    return info


def test_ensure_streams_before_connect_raises() -> None:
    """未 connect 之前调 ensure_streams 应该抛 RuntimeError（对称 ensure_stream legacy 行为）。"""
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="test")
    with pytest.raises(RuntimeError, match="before connect"):
        asyncio.run(bus.ensure_streams())


def test_ensure_streams_creates_all_missing_streams() -> None:
    """I-2：两条 stream 都不存在 → 分别走 add_stream，顺序无关但都被调一次。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    from nats.js.errors import NotFoundError  # type: ignore[import-not-found]

    bus = NatsEventBus(
        config=NatsBusConfig(streams=DEFAULT_STREAM_SPECS),
        consumer_role="test",
    )
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    asyncio.run(bus.ensure_streams())

    assert fake_js.add_stream.await_count == len(DEFAULT_STREAM_SPECS)
    fake_js.update_stream.assert_not_awaited()


def test_ensure_streams_noop_when_both_streams_match() -> None:
    """I-2：两条 stream 都已存在且 config 完全匹配 → unchanged，
    既不 add_stream 也不 update_stream。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(streams=DEFAULT_STREAM_SPECS),
        consumer_role="test",
    )

    # 每条 stream 的 stream_info 返回匹配的 FakeStreamInfo
    name_to_info = {
        spec.name: _make_fake_stream_info_from_spec(spec)
        for spec in DEFAULT_STREAM_SPECS
    }

    async def fake_stream_info(name: str) -> Any:
        return name_to_info[name]

    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=fake_stream_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    asyncio.run(bus.ensure_streams())

    fake_js.add_stream.assert_not_awaited()
    fake_js.update_stream.assert_not_awaited()
    assert fake_js.stream_info.await_count == len(DEFAULT_STREAM_SPECS)


def test_ensure_streams_updates_when_max_bytes_drifts() -> None:
    """容量感知对比：max_bytes 漂移 → update_stream。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(streams=(DEFAULT_AATS_EVENTS_SPEC,)),
        consumer_role="test",
    )

    drifted_info = _make_fake_stream_info_from_spec(DEFAULT_AATS_EVENTS_SPEC)
    drifted_info.config.max_bytes = 1_000_000  # 比 spec 小得多 → drift

    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(return_value=drifted_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    asyncio.run(bus.ensure_streams())

    fake_js.add_stream.assert_not_awaited()
    fake_js.update_stream.assert_awaited_once()
    # update_stream 带的 config 应该是 spec 的目标值
    cfg = fake_js.update_stream.await_args.kwargs["config"]
    assert cfg.max_bytes == DEFAULT_AATS_EVENTS_SPEC.max_bytes


def test_ensure_streams_updates_when_max_msgs_drifts() -> None:
    """容量感知对比：max_msgs 漂移 → update_stream。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(streams=(DEFAULT_AATS_EVENTS_MARKET_SPEC,)),
        consumer_role="test",
    )

    drifted_info = _make_fake_stream_info_from_spec(DEFAULT_AATS_EVENTS_MARKET_SPEC)
    drifted_info.config.max_msgs = 100  # 差一大截

    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(return_value=drifted_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    asyncio.run(bus.ensure_streams())

    fake_js.update_stream.assert_awaited_once()


def test_ensure_streams_updates_when_max_msg_size_drifts() -> None:
    """容量感知对比：max_msg_size 漂移 → update_stream。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(streams=(DEFAULT_AATS_EVENTS_MARKET_SPEC,)),
        consumer_role="test",
    )

    drifted_info = _make_fake_stream_info_from_spec(DEFAULT_AATS_EVENTS_MARKET_SPEC)
    drifted_info.config.max_msg_size = 1024  # 远低于 4 MB

    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(return_value=drifted_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    asyncio.run(bus.ensure_streams())

    fake_js.update_stream.assert_awaited_once()


def test_ensure_streams_updates_one_noop_another() -> None:
    """I-2 混合场景：MARKET 有漂移（update）+ EVENTS 完全匹配（noop）。

    验证 ensure_streams 确实对每条 stream 独立判断，不会把一个 drift 的判断
    牵连到另一条 stream。
    """
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(streams=DEFAULT_STREAM_SPECS),
        consumer_role="test",
    )

    market_info = _make_fake_stream_info_from_spec(DEFAULT_AATS_EVENTS_MARKET_SPEC)
    market_info.config.max_bytes = 1_000  # drift
    events_info = _make_fake_stream_info_from_spec(DEFAULT_AATS_EVENTS_SPEC)  # 精确匹配
    # B2a 新增 COMMANDS spec 也要提供精确匹配 fake，否则 ensure_streams 第三条
    # 查不到会 KeyError
    commands_info = _make_fake_stream_info_from_spec(DEFAULT_AATS_EVENTS_COMMANDS_SPEC)

    name_to_info = {
        "AATS_EVENTS_MARKET": market_info,
        "AATS_EVENTS": events_info,
        "AATS_EVENTS_COMMANDS": commands_info,
    }

    async def fake_stream_info(name: str) -> Any:
        return name_to_info[name]

    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=fake_stream_info)
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    asyncio.run(bus.ensure_streams())

    fake_js.add_stream.assert_not_awaited()
    # 只有 MARKET 触发 update
    assert fake_js.update_stream.await_count == 1
    updated_cfg = fake_js.update_stream.await_args.kwargs["config"]
    assert updated_cfg.name == "AATS_EVENTS_MARKET"


def test_ensure_stream_legacy_shim_emits_deprecation_warning() -> None:
    """I-10 legacy shim 隔离：ensure_stream(topics=...) 必须发
    DeprecationWarning，提醒未来用 ensure_streams()。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    from nats.js.errors import NotFoundError  # type: ignore[import-not-found]

    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="test")
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    with pytest.warns(DeprecationWarning, match="ensure_streams"):
        asyncio.run(bus.ensure_stream(topics=["decisions"]))


# ── Group F: NatsEventBus.start() 双路径（2 tests） ─────────────────


def test_nats_event_bus_start_no_topics_calls_ensure_streams() -> None:
    """runtime 新路径：start() 无 topics → 走 ensure_streams()（多 stream）。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    from nats.js.errors import NotFoundError  # type: ignore[import-not-found]

    bus = NatsEventBus(
        config=NatsBusConfig(streams=DEFAULT_STREAM_SPECS),
        consumer_role="test",
    )
    # 直接 stub connect + js，跳过 nats 客户端
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]
    bus._connected = True  # type: ignore[attr-defined]

    # 不传 topics → 走新路径 → 应该对 DEFAULT_STREAM_SPECS 里每条 stream 调一次 add_stream
    asyncio.run(bus.start(topics=None))

    assert fake_js.add_stream.await_count == len(DEFAULT_STREAM_SPECS)


def test_nats_event_bus_start_with_topics_calls_legacy_shim() -> None:
    """legacy 测试路径：start(topics=[...]) → 走 ensure_stream 老 shim
    → 应该发 DeprecationWarning + 单次 add_stream（一条临时 stream）。"""
    pytest.importorskip("nats")

    from unittest.mock import AsyncMock, MagicMock

    from nats.js.errors import NotFoundError  # type: ignore[import-not-found]

    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="test")
    fake_js = MagicMock()
    fake_js.stream_info = AsyncMock(side_effect=NotFoundError())
    fake_js.add_stream = AsyncMock()
    fake_js.update_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]
    bus._connected = True  # type: ignore[attr-defined]

    with pytest.warns(DeprecationWarning):
        asyncio.run(bus.start(topics=["decisions"]))

    fake_js.add_stream.assert_awaited_once()


# ── Group G: _compute_stream_config_drift 单元测试（2 tests） ───────


def test_compute_stream_config_drift_empty_when_exact_match() -> None:
    """所有字段匹配 → 返回空 dict。"""
    spec = DEFAULT_AATS_EVENTS_SPEC
    info = _make_fake_stream_info_from_spec(spec)
    subjects = [f"aats.{t}" for t in sorted(spec.topics)]

    drift = _compute_stream_config_drift(info.config, spec, desired_subjects=subjects)
    assert drift == {}


def test_compute_stream_config_drift_handles_nanosecond_max_age() -> None:
    """nats-py 某些版本 stream_info 返回的 max_age 可能是纳秒整数。
    drift 函数必须能识别并归一化到秒做比较，否则会把完全匹配的 stream
    误判成 drift 触发多余 update_stream。
    """
    spec = DEFAULT_AATS_EVENTS_SPEC
    info = _make_fake_stream_info_from_spec(spec)
    # 模拟纳秒整数：spec.max_age_seconds * 1e9 = 纳秒（2026-04-20 改 1 天 = 8.64e13 纳秒）
    info.config.max_age = int(spec.max_age_seconds * 1e9)
    subjects = [f"aats.{t}" for t in sorted(spec.topics)]

    drift = _compute_stream_config_drift(info.config, spec, desired_subjects=subjects)
    assert "max_age_seconds" not in drift, (
        f"drift 函数没有把纳秒归一化到秒，误报 drift: {drift}"
    )


# ─────────────────────────────────────────────────────────────────────
# Slow Consumer 防护：delivery semantics + flow control
# ─────────────────────────────────────────────────────────────────────


def test_delivery_semantics_for_snapshot_topics() -> None:
    """所有 SNAPSHOT_DELIVERY_TOPICS 必须返回 "snapshot"。"""
    for topic in SNAPSHOT_DELIVERY_TOPICS:
        assert delivery_semantics_for(topic) == "snapshot", (
            f"topic {topic!r} 应为 snapshot 语义"
        )


def test_delivery_semantics_for_transient_topics() -> None:
    """所有 TRANSIENT_DELIVERY_TOPICS 必须返回 "transient"。"""
    for topic in TRANSIENT_DELIVERY_TOPICS:
        assert delivery_semantics_for(topic) == "transient", (
            f"topic {topic!r} 应为 transient 语义"
        )


def test_delivery_semantics_for_event_topics_default() -> None:
    """未被归类为 snapshot/transient 的 topic 一律返回 "event"（安全默认）。"""
    event_topics = [
        _topics.ORDER_UPDATES,
        _topics.FILL_EVENTS,
        _topics.OBLIGATION_UPDATES,
        _topics.DECISION_CONTEXTS,
        _topics.RISK_DECISIONS,
    ]
    for topic in event_topics:
        assert delivery_semantics_for(topic) == "event", (
            f"topic {topic!r} 应为 event 语义（默认）"
        )


def test_consumer_config_spec_snapshot_delivers_last() -> None:
    """Snapshot topic 必须生成 deliver_policy="last" 的 ConsumerConfigSpec。"""
    config = NatsBusConfig()
    spec = build_consumer_config_spec(
        config=config,
        durable="aats-gateway-market_snapshots",
        topic=_topics.MARKET_SNAPSHOTS,
    )
    assert spec.deliver_policy == "last"


def test_consumer_config_spec_transient_delivers_new() -> None:
    """Transient topic 必须生成 deliver_policy="new" 的 ConsumerConfigSpec。"""
    config = NatsBusConfig()
    spec = build_consumer_config_spec(
        config=config,
        durable="aats-gateway-operator_command_responses",
        topic=_topics.OPERATOR_COMMAND_RESPONSES,
    )
    assert spec.deliver_policy == "new"


def test_consumer_config_spec_event_delivers_all() -> None:
    """Event topic 必须生成 deliver_policy="all" 的 ConsumerConfigSpec（保证不丢消息）。"""
    config = NatsBusConfig()
    spec = build_consumer_config_spec(
        config=config,
        durable="aats-execution-order_updates",
        topic=_topics.ORDER_UPDATES,
    )
    assert spec.deliver_policy == "all"


def test_per_topic_ack_wait_override_applies() -> None:
    """2026-04-20 code review Issue 2+3 fix regression:
    per_topic_ack_wait_seconds 覆盖单个 topic 的 ack_wait, 其他 topic 用默认.

    诊断报告发现 aats-decision-features_snapshots 因 run_cycle 17s + ack_wait 30s
    → 死循环重投 8580 次. 新加 per-topic ack_wait override 机制, 本测试锁定
    机制真的生效.
    """
    config = NatsBusConfig(
        per_topic_ack_wait_seconds={_topics.FEATURE_SNAPSHOTS: 90.0},
        ack_wait_seconds=30.0,  # 全局默认保持 30s
    )

    # 被 override 的 topic 用 90s
    spec_override = build_consumer_config_spec(
        config=config,
        durable="aats-decision-features_snapshots",
        topic=_topics.FEATURE_SNAPSHOTS,
    )
    assert spec_override.ack_wait_seconds == 90.0, (
        f"per_topic_ack_wait 未生效, 实际 {spec_override.ack_wait_seconds}"
    )

    # 未 override 的 topic 用默认 30s
    spec_default = build_consumer_config_spec(
        config=config,
        durable="aats-execution-order_updates",
        topic=_topics.ORDER_UPDATES,
    )
    assert spec_default.ack_wait_seconds == 30.0, (
        f"非 override topic 应保持默认 30s, 实际 {spec_default.ack_wait_seconds}"
    )


def test_per_topic_max_ack_pending_and_ack_wait_combo_for_slow_consumers() -> None:
    """2026-04-20 code review Issue 2+3 fix regression (combo):
    FEATURE_SNAPSHOTS 必须同时有 per_topic_max_ack_pending=32 + ack_wait=90s
    + deliver_policy="last" (from SNAPSHOT_DELIVERY_TOPICS). 三者联合防止
    决策流堵塞 (pending 142K 的那类 backlog).
    """
    config = NatsBusConfig(
        per_topic_max_ack_pending={_topics.FEATURE_SNAPSHOTS: 32},
        per_topic_ack_wait_seconds={_topics.FEATURE_SNAPSHOTS: 90.0},
    )
    spec = build_consumer_config_spec(
        config=config,
        durable="aats-decision-features_snapshots",
        topic=_topics.FEATURE_SNAPSHOTS,
    )
    assert spec.max_ack_pending == 32
    assert spec.ack_wait_seconds == 90.0
    assert spec.deliver_policy == "last", (
        "FEATURE_SNAPSHOTS 必须在 SNAPSHOT_DELIVERY_TOPICS, 给出 deliver_policy=last"
    )
    assert spec.flow_control is True


def test_consumer_config_spec_flow_control_defaults() -> None:
    """默认 NatsBusConfig 必须启用 flow_control=True + idle_heartbeat=5.0。

    这两个默认值是 Slow Consumer 防护的核心——新部署不需要任何额外配置就
    自动获得 NATS 原生反压能力。
    """
    config = NatsBusConfig()
    assert config.flow_control is True
    assert config.idle_heartbeat_seconds == 5.0

    # 确认 spec 传播了这些值
    spec = build_consumer_config_spec(
        config=config,
        durable="aats-decision-decisions",
        topic=_topics.DECISION_CONTEXTS,
    )
    assert spec.flow_control is True
    assert spec.idle_heartbeat_seconds == 5.0


def test_delivery_classified_topics_are_subset_of_critical() -> None:
    """snapshot/transient 分类的 topic 必须在 critical 路由中。

    只有 critical topic 才会经过 NatsEventBus.subscribe()，observer topic
    走内存 bus 不经过 JetStream。如果某个 topic 被分类为 snapshot/transient
    却不在 critical 路由中，deliver_policy 设置就不会生效——纯粹浪费配置。
    """
    classified = SNAPSHOT_DELIVERY_TOPICS | TRANSIENT_DELIVERY_TOPICS
    non_critical = classified - DEFAULT_CRITICAL_TOPICS
    assert not non_critical, (
        f"以下 topic 被分类为 snapshot/transient 但不在 critical 路由中"
        f"（deliver_policy 不会生效）: {sorted(non_critical)}"
    )


def test_account_baselines_not_in_snapshot_delivery() -> None:
    """ACCOUNT_BASELINES 不应归为 snapshot（低频但每条有状态意义）。

    回归防护：operator 可连续 rebaseline，DeliverLast 会丢失中间状态。
    """
    assert _topics.ACCOUNT_BASELINES not in SNAPSHOT_DELIVERY_TOPICS
    assert delivery_semantics_for(_topics.ACCOUNT_BASELINES) == "event"


def test_delivery_semantics_return_type_is_literal() -> None:
    """delivery_semantics_for 返回值和 ConsumerConfigSpec.deliver_policy
    必须是 Literal 类型，防止拼写错误在运行时才暴露。
    """
    from typing import get_type_hints

    hints = get_type_hints(delivery_semantics_for)
    assert hints["return"] is DeliverySemantics

    hints_spec = get_type_hints(ConsumerConfigSpec)
    assert hints_spec["deliver_policy"] is DeliverPolicyStr


# ─────────────────────────────────────────────────────────────────────
# R4-X2：consumer 回调对确定性失败必须 term()/ack() 而不是 nak()
# ─────────────────────────────────────────────────────────────────────


class _FakeMsgWithTerm:
    """模拟 nats-py 2.10+ msg：既有 term() 又有 ack()/nak()。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.term_called = 0
        self.ack_called = 0
        self.nak_called = 0

    async def term(self) -> None:
        self.term_called += 1

    async def ack(self) -> None:
        self.ack_called += 1

    async def nak(self, delay: int | None = None) -> None:  # noqa: ARG002
        self.nak_called += 1


class _FakeMsgNoTerm:
    """模拟老 nats-py：没 term()，只能 ack()/nak()。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.ack_called = 0
        self.nak_called = 0

    async def ack(self) -> None:
        self.ack_called += 1

    async def nak(self, delay: int | None = None) -> None:  # noqa: ARG002
        self.nak_called += 1


class _ProgressMsg(_FakeMsgWithTerm):
    def __init__(self, data: bytes, *, fail_progress: bool = False) -> None:
        super().__init__(data)
        self.progress_called = 0
        self.fail_progress = fail_progress

    async def in_progress(self) -> None:
        self.progress_called += 1
        if self.fail_progress:
            raise RuntimeError("simulated progress failure")


_TEST_CONSUMER_CREATED = "2026-08-29T00:00:00Z"


def _fake_consumer_info(
    config: Any,
    *,
    created: Any = _TEST_CONSUMER_CREATED,
    cursor: tuple[int, int, int, int] = (0, 0, 0, 0),
    push_bound: bool = True,
    num_pending: int = 0,
    num_ack_pending: int = 0,
) -> Any:
    return SimpleNamespace(
        name=config.durable_name,
        config=config,
        created=created,
        delivered=SimpleNamespace(
            stream_seq=cursor[0],
            consumer_seq=cursor[1],
        ),
        ack_floor=SimpleNamespace(
            stream_seq=cursor[2],
            consumer_seq=cursor[3],
        ),
        push_bound=push_bound,
        num_pending=num_pending,
        num_ack_pending=num_ack_pending,
    )


class _BoundConsumerSubscription:
    def __init__(
        self,
        info: Any,
        *,
        binding_subject: str | None = None,
    ) -> None:
        self._info = info
        self._subject = binding_subject or info.config.deliver_subject
        self.drained = False
        self.unsubscribed = False

    @property
    def subject(self) -> str:
        return self._subject

    async def consumer_info(self) -> Any:
        return self._info

    async def drain(self) -> None:
        self.drained = True

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _HangingUnsubscribeBoundSubscription(_BoundConsumerSubscription):
    def __init__(self, info: Any, *, binding_subject: str) -> None:
        super().__init__(info, binding_subject=binding_subject)
        self.unsubscribe_started = False

    async def unsubscribe(self) -> None:
        self.unsubscribe_started = True
        await asyncio.Future()


class _CbCapturingJS:
    """mock JetStreamContext：subscribe() 把 cb 捕获下来给测试调用。"""

    def __init__(self) -> None:
        self.captured_cb: Any = None
        self.captured_config: Any = None

    async def consumer_info(self, _stream: str, _durable: str) -> Any:
        from nats.js.errors import NotFoundError

        raise NotFoundError

    async def subscribe(
        self,
        *,
        subject: str,  # noqa: ARG002
        durable: str,  # noqa: ARG002
        cb: Any,
        manual_ack: bool,  # noqa: ARG002
        config: Any,
        flow_control: bool = False,  # noqa: ARG002
        idle_heartbeat: float | None = None,  # noqa: ARG002
    ) -> Any:
        self.captured_cb = cb
        self.captured_config = config
        return object()


async def _capture_on_msg(bus: NatsEventBus) -> Any:
    """安装 _CbCapturingJS 并跑一次 subscribe()，拿到 _on_msg 闭包。"""

    js = _CbCapturingJS()
    bus._js = js  # type: ignore[assignment]

    async def _noop_handler(_msg: dict[str, Any]) -> None:
        pass

    await bus.subscribe("market_snapshots", _noop_handler)
    assert js.captured_cb is not None
    return js.captured_cb


def test_delivery_gate_limits_broker_prefetch_to_one_message() -> None:
    """门禁关闭期间只有队首 callback 能发 +WPI，broker 不得再预取。"""

    async def _run(*, gated: bool) -> int:
        gate = NatsDeliveryGate() if gated else None
        bus = NatsEventBus(
            config=NatsBusConfig(max_ack_pending=73),
            consumer_role="decision",
            delivery_gate=gate,
        )
        js = _CbCapturingJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_msg: dict[str, Any]) -> None:
            return None

        await bus.subscribe("market_snapshots", _noop_handler)
        return int(js.captured_config.max_ack_pending)

    assert asyncio.run(_run(gated=True)) == 1
    assert asyncio.run(_run(gated=False)) == 73


def test_post_bind_mismatch_aborts_callback_and_bounds_local_unsubscribe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Identity failure must not drain, ACK, delete, or wait indefinitely."""

    from nats.js.errors import NotFoundError

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(consumer_supervision_interval_seconds=0.01)

    class _RoguePostBindJS:
        def __init__(self) -> None:
            self.subscription: _HangingUnsubscribeBoundSubscription | None = None
            self.callback_task: asyncio.Task[None] | None = None
            self.message = _ProgressMsg(b"not-parsed")
            self.delete_called = False

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            raise NotFoundError

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.delete_called = True

        async def subscribe(self, **kwargs: Any) -> Any:
            broker_config = kwargs["config"].evolve(
                filter_subject=kwargs["subject"],
                deliver_subject="_INBOX.rogue",
            )
            broker_info = _fake_consumer_info(broker_config)
            broker_info.name = None
            self.subscription = _HangingUnsubscribeBoundSubscription(
                broker_info,
                binding_subject="_INBOX.local",
            )
            self.callback_task = asyncio.create_task(kwargs["cb"](self.message))
            return self.subscription

    async def _run() -> tuple[NatsDeliveryGate, _RoguePostBindJS]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _RoguePostBindJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            raise AssertionError("aborted callback must not reach handler")

        with pytest.raises(
            RuntimeError,
            match="nats_critical_consumer_post_bind_config_drift",
        ) as exc_info:
            await asyncio.wait_for(
                bus.subscribe(topic, _noop_handler),
                timeout=0.2,
            )
        assert "consumer_name" in str(exc_info.value)
        assert "deliver_subject_binding" in str(exc_info.value)
        assert js.callback_task is not None
        await asyncio.wait_for(js.callback_task, timeout=0.1)
        return gate, js

    gate, js = asyncio.run(_run())
    assert gate.aborted is True
    assert js.subscription is not None
    assert js.subscription.unsubscribe_started is True
    assert js.subscription.drained is False
    assert js.delete_called is False
    assert js.message.ack_called == 0
    assert js.message.nak_called == 0
    assert js.message.term_called == 0
    assert "_INBOX.local" not in caplog.text
    assert "_INBOX.rogue" not in caplog.text


def test_existing_gated_durable_is_updated_in_place_before_binding() -> None:
    """v1 durable 的 256 预取必须原位改成 1，且绝不能删除 cursor。"""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(max_ack_pending=256)
    durable = config.durable_name_for("execution", topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None

    class _ExistingDurableJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=256,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
                headers_only=False,
                backoff=[],
                rate_limit_bps=0,
                mem_storage=False,
            )
            self.bound_config: Any = None

        async def consumer_info(self, stream_name: str, name: str) -> Any:
            assert stream_name == stream.name
            assert name == durable
            self.operations.append("consumer_info")
            return _fake_consumer_info(self.current)

        async def add_consumer(self, stream_name: str, *, config: Any) -> Any:
            assert stream_name == stream.name
            self.operations.append("add_consumer")
            self.current = config
            return _fake_consumer_info(config)

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **kwargs: Any) -> object:
            self.operations.append("subscribe")
            self.bound_config = kwargs["config"]
            return _BoundConsumerSubscription(
                _fake_consumer_info(self.current)
            )

    async def _run() -> _ExistingDurableJS:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _ExistingDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_msg: dict[str, Any]) -> None:
            return None

        await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == [
        "consumer_info",
        "add_consumer",
        "consumer_info",
        "subscribe",
    ]
    assert js.current.max_ack_pending == 1
    assert js.current.deliver_subject == "_INBOX.existing"
    assert js.current.filter_subject == config.subject_for(topic)


@pytest.mark.parametrize(
    (
        "topic",
        "current_ack_wait",
        "current_max_deliver",
        "expected_blocker",
    ),
    (
        (_topics.ORDER_INTENTS, 29.0, 5, "ack_wait_drift"),
        (_topics.ORDER_INTENTS, 31.0, 5, "ack_wait_drift"),
        (_topics.ORDER_INTENTS, 30.0, 6, "max_deliver_drift"),
        (_topics.MARKET_SNAPSHOTS, 0.0, 5, "ack_wait_drift"),
        (_topics.MARKET_SNAPSHOTS, -1.0, 5, "ack_wait_drift"),
        (_topics.MARKET_SNAPSHOTS, 120.0, 5, "ack_wait_drift"),
        (_topics.MARKET_SNAPSHOTS, 30.0, 6, "max_deliver_drift"),
    ),
)
def test_existing_durable_rejects_unsafe_mutable_migration_without_side_effects(
    topic: str,
    current_ack_wait: float,
    current_max_deliver: int,
    expected_blocker: str,
) -> None:
    """Preflight and runtime reject the same drift before broker mutation."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    config = NatsBusConfig(
        per_topic_ack_wait_seconds={_topics.MARKET_SNAPSHOTS: 90.0},
    )
    role = "execution" if topic == _topics.ORDER_INTENTS else "decision"
    durable = config.durable_name_for(role, topic)
    deliver_policy = (
        DeliverPolicy.ALL
        if topic == _topics.ORDER_INTENTS
        else DeliverPolicy.LAST
    )

    class _UnsafeMutableDurableJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=deliver_policy,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=current_ack_wait,
                max_deliver=current_max_deliver,
                max_ack_pending=1,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
                headers_only=False,
                backoff=[],
                rate_limit_bps=0,
                mem_storage=False,
            )

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(self.current)

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            raise AssertionError("unsafe mutable drift must not be updated")

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            raise AssertionError("unsafe mutable drift must not be rebuilt")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            raise AssertionError("unsafe mutable drift must not be bound")

    async def _run() -> _UnsafeMutableDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role=role,
            delivery_gate=NatsDeliveryGate(),
        )
        js = _UnsafeMutableDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_critical_consumer_mutable_config_drift:"
                f"{durable}:{expected_blocker}$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info"]


def test_snapshot_ack_wait_may_increase_in_place_without_cursor_rebuild() -> None:
    """A positive LAST ack_wait raise is the only mutable non-window migration."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.MARKET_SNAPSHOTS
    config = NatsBusConfig(
        per_topic_ack_wait_seconds={topic: 90.0},
    )
    durable = config.durable_name_for("decision", topic)

    class _SnapshotAckWaitJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.LAST,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=1,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
                headers_only=False,
                backoff=[],
                rate_limit_bps=0,
                mem_storage=False,
            )

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                cursor=(10, 10, 8, 8),
            )

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            self.current = config
            return _fake_consumer_info(
                self.current,
                cursor=(11, 11, 9, 9),
            )

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            raise AssertionError("safe ack_wait raise must preserve the durable")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            return _BoundConsumerSubscription(
                _fake_consumer_info(
                    self.current,
                    cursor=(11, 11, 9, 9),
                )
            )

    async def _run() -> _SnapshotAckWaitJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="decision",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _SnapshotAckWaitJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == [
        "consumer_info",
        "add_consumer",
        "consumer_info",
        "subscribe",
    ]
    assert js.current.ack_wait == 90.0
    assert js.current.max_deliver == 5


def test_in_place_update_readback_rejects_new_behavior_drift() -> None:
    """A broker response must not smuggle unsafe behavior into an update."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(max_ack_pending=256)
    durable = config.durable_name_for("execution", topic)

    class _UnsafeUpdateReadbackJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=256,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
            )

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(self.current)

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            self.current = config.evolve(headers_only=True)
            return _fake_consumer_info(self.current)

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            raise AssertionError("unverified update must not be bound")

    async def _run() -> _UnsafeUpdateReadbackJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _UnsafeUpdateReadbackJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_consumer_in_place_update_unverified:"
                f"{durable}:headers_only$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info", "add_consumer", "consumer_info"]


def test_in_place_update_readback_rejects_same_name_recreation() -> None:
    """Consumer update may not replace a critical durable behind the caller."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(max_ack_pending=256)
    durable = config.durable_name_for("execution", topic)

    class _RecreatedDuringUpdateJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=256,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
            )
            self.recreated = False

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                created=(
                    "2026-08-29T00:01:00Z"
                    if self.recreated
                    else _TEST_CONSUMER_CREATED
                ),
                cursor=(0, 0, 0, 0) if self.recreated else (10, 10, 8, 8),
            )

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            self.current = config
            self.recreated = True
            return _fake_consumer_info(self.current)

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **_kwargs: Any) -> object:
            raise AssertionError("recreated update must not be bound")

    async def _run() -> _RecreatedDuringUpdateJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _RecreatedDuringUpdateJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_consumer_in_place_update_unverified:"
                f"{durable}:created,cursor_regressed$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info", "add_consumer", "consumer_info"]


def test_post_bind_rejects_recreation_after_existing_identity_snapshot() -> None:
    """A same-config recreation between lookup and bind must not reset baseline."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(max_ack_pending=1)
    durable = config.durable_name_for("execution", topic)

    class _RecreatedDuringBindJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=1,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
            )
            self.subscription: _BoundConsumerSubscription | None = None

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                cursor=(10, 10, 8, 8),
            )

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            self.subscription = _BoundConsumerSubscription(
                _fake_consumer_info(
                    self.current,
                    created="2026-08-29T00:01:00Z",
                    cursor=(0, 0, 0, 0),
                )
            )
            return self.subscription

    async def _run() -> tuple[NatsDeliveryGate, _RecreatedDuringBindJS]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _RecreatedDuringBindJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_critical_consumer_post_bind_identity_drift:"
                f"{durable}:created,cursor_regressed$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return gate, js

    gate, js = asyncio.run(_run())
    assert gate.aborted is True
    assert js.operations == ["consumer_info", "subscribe"]
    assert js.subscription is not None
    assert js.subscription.unsubscribed is True
    assert js.subscription.drained is False


def test_gated_critical_durable_with_unacked_messages_refuses_window_shrink() -> None:
    """首次 v1→v2 降窗不能在仍有 in-flight ACK 时改变 critical cursor。"""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(max_ack_pending=256)
    durable = config.durable_name_for("execution", topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None

    class _OutstandingCriticalDurableJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=256,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
            )

        async def consumer_info(self, stream_name: str, name: str) -> Any:
            assert stream_name == stream.name
            assert name == durable
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                num_ack_pending=3,
            )

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            raise AssertionError(
                "ack window must not shrink while critical messages are unacked"
            )

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            raise AssertionError("failed migration must not bind the durable")

    async def _run() -> _OutstandingCriticalDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _OutstandingCriticalDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_msg: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_critical_consumer_ack_window_migration_requires_drain:"
                f"{durable}$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info"]
    assert js.current.max_ack_pending == 256


def test_gated_critical_durable_rejects_legacy_outstanding_after_window_shrunk() -> None:
    """已降到单 ACK 窗口也不能承接遗留的多个 in-flight delivery。"""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig(max_ack_pending=256)
    durable = config.durable_name_for("execution", topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None

    class _AlreadyShrunkOutstandingDurableJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=1,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
            )

        async def consumer_info(self, stream_name: str, name: str) -> Any:
            assert stream_name == stream.name
            assert name == durable
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                num_ack_pending=3,
            )

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            raise AssertionError("already-shrunk unsafe state must not be updated")

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            raise AssertionError("unsafe outstanding deliveries must not be bound")

    async def _run() -> _AlreadyShrunkOutstandingDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _AlreadyShrunkOutstandingDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_msg: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_critical_consumer_ack_window_migration_requires_drain:"
                f"{durable}$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info"]
    assert js.current.max_ack_pending == 1


@pytest.mark.parametrize(
    ("topic", "role", "expected_policy"),
    (
        (_topics.MARKET_SNAPSHOTS, "decision", "last"),
        (_topics.OPERATOR_COMMAND_REQUESTS, "execution", "new"),
    ),
)
def test_gated_non_event_durable_rebuilds_legacy_outstanding_before_binding(
    topic: str,
    role: str,
    expected_policy: str,
) -> None:
    """LAST/NEW cursor 可丢弃；旧宽窗口 delivery 必须在 gate 绑定前清空。"""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    config = NatsBusConfig(max_ack_pending=256)
    durable = config.durable_name_for(role, topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None
    deliver_policy = {
        "last": DeliverPolicy.LAST,
        "new": DeliverPolicy.NEW,
    }[expected_policy]

    class _OutstandingSnapshotDurableJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.deleted = False
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=deliver_policy,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=256,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
            )
            self.bound_config: Any = None

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                num_ack_pending=3,
            )

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            raise AssertionError("disposable snapshot durable must be rebuilt")

        async def delete_consumer(self, stream_name: str, name: str) -> None:
            assert stream_name == stream.name
            assert name == durable
            self.operations.append("delete_consumer")
            self.deleted = True

        async def subscribe(self, **kwargs: Any) -> object:
            assert self.deleted is True
            self.operations.append("subscribe")
            self.current = kwargs["config"].evolve(
                filter_subject=kwargs["subject"],
                deliver_subject="_INBOX.rebuilt",
            )
            self.bound_config = self.current
            return _BoundConsumerSubscription(
                _fake_consumer_info(
                    self.current,
                    created="2026-08-29T00:01:00Z",
                )
            )

    async def _run() -> _OutstandingSnapshotDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role=role,
            delivery_gate=NatsDeliveryGate(),
        )
        js = _OutstandingSnapshotDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_msg: dict[str, Any]) -> None:
            return None

        await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info", "delete_consumer", "subscribe"]
    assert js.bound_config.max_ack_pending == 1
    assert js.bound_config.deliver_policy == deliver_policy


def test_critical_durable_immutable_drift_fails_without_cursor_reset() -> None:
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig()
    durable = config.durable_name_for("execution", topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None

    class _DriftedDurableJS:
        def __init__(self) -> None:
            self.delete_called = False

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            return SimpleNamespace(
                config=ConsumerConfig(
                    durable_name=durable,
                    deliver_policy=DeliverPolicy.LAST,
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=30.0,
                    max_deliver=5,
                    max_ack_pending=256,
                    filter_subject=config.subject_for(topic),
                    deliver_subject="_INBOX.existing",
                    flow_control=True,
                    idle_heartbeat=5.0,
                )
            )

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.delete_called = True

    async def _run() -> _DriftedDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _DriftedDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_msg: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match="nats_critical_consumer_config_drift",
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.delete_called is False


_DANGEROUS_CONSUMER_BEHAVIOR_FIELDS = (
    "deliver_subject",
    "replay_policy",
    "deliver_group",
    "headers_only",
    "pause_until",
    "backoff",
    "rate_limit_bps",
    "inactive_threshold",
    "mem_storage",
    "opt_start_seq",
    "opt_start_time",
)
_UNSAFE_EXISTING_CONSUMER_CONFIG_FIELDS = (
    "durable_name",
    *_DANGEROUS_CONSUMER_BEHAVIOR_FIELDS,
)


def _unsafe_consumer_config_override(field_name: str) -> dict[str, Any]:
    from nats.js.api import ReplayPolicy

    overrides: dict[str, Any] = {
        "durable_name": None,
        "deliver_subject": None,
        "replay_policy": ReplayPolicy.ORIGINAL,
        "deliver_group": "rogue-queue",
        "headers_only": True,
        "pause_until": "2099-01-01T00:00:00Z",
        "backoff": [1.0],
        "rate_limit_bps": 1,
        "inactive_threshold": 1.0,
        "mem_storage": True,
        "opt_start_seq": 2,
        "opt_start_time": "2099-01-01T00:00:00Z",
    }
    return {field_name: overrides[field_name]}


@pytest.mark.parametrize(
    "drift_name",
    _UNSAFE_EXISTING_CONSUMER_CONFIG_FIELDS,
)
def test_critical_all_durable_rejects_unsafe_config_without_cursor_reset(
    drift_name: str,
) -> None:
    """ALL cursor must not bind, update, or rebuild under behavior drift."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.ORDER_INTENTS
    config = NatsBusConfig()
    durable = config.durable_name_for("execution", topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None

    class _DangerousCriticalDurableJS:
        def __init__(self) -> None:
            canonical = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30.0,
                max_deliver=5,
                max_ack_pending=256,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                flow_control=True,
                idle_heartbeat=5.0,
            )
            self.current = canonical.evolve(
                **_unsafe_consumer_config_override(drift_name)
            )
            self.operations: list[str] = []

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            info = _fake_consumer_info(self.current)
            info.name = durable
            return info

        async def add_consumer(self, _stream: str, *, config: Any) -> Any:
            self.operations.append("add_consumer")
            raise AssertionError("unsafe ALL durable must not be updated")

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            raise AssertionError("critical durable cursor must never be deleted")

        async def subscribe(self, **_kwargs: Any) -> object:
            self.operations.append("subscribe")
            raise AssertionError("unsafe ALL durable must not be bound")

    async def _run() -> _DangerousCriticalDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _DangerousCriticalDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        with pytest.raises(
            RuntimeError,
            match=(
                "^nats_critical_consumer_config_drift:"
                f"{durable}:{drift_name}$"
            ),
        ):
            await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info"]


def test_non_event_durable_rebuilds_dangerous_behavior_before_binding() -> None:
    """LAST/NEW semantics may discard their disposable cursor to recover."""

    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    topic = _topics.MARKET_SNAPSHOTS
    config = NatsBusConfig(
        per_topic_ack_wait_seconds={topic: 90.0},
    )
    durable = config.durable_name_for("decision", topic)
    stream = config.stream_spec_for_topic(topic)
    assert stream is not None

    class _DangerousSnapshotDurableJS:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.deleted = False
            self.current = ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.LAST,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=90.0,
                max_deliver=5,
                max_ack_pending=1,
                filter_subject=config.subject_for(topic),
                deliver_subject="_INBOX.existing",
                headers_only=True,
            )
            self.bound_config: Any = None

        async def consumer_info(self, _stream: str, _name: str) -> Any:
            self.operations.append("consumer_info")
            return _fake_consumer_info(
                self.current,
                num_ack_pending=0,
            )

        async def delete_consumer(self, _stream: str, _name: str) -> None:
            self.operations.append("delete_consumer")
            self.deleted = True

        async def subscribe(self, **kwargs: Any) -> object:
            assert self.deleted is True
            self.operations.append("subscribe")
            self.current = kwargs["config"].evolve(
                filter_subject=kwargs["subject"],
                deliver_subject="_INBOX.rebuilt",
            )
            self.bound_config = self.current
            return _BoundConsumerSubscription(
                _fake_consumer_info(self.current)
            )

    async def _run() -> _DangerousSnapshotDurableJS:
        bus = NatsEventBus(
            config=config,
            consumer_role="decision",
            delivery_gate=NatsDeliveryGate(),
        )
        js = _DangerousSnapshotDurableJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        await bus.subscribe(topic, _noop_handler)
        return js

    js = asyncio.run(_run())
    assert js.operations == ["consumer_info", "delete_consumer", "subscribe"]
    assert js.bound_config.headers_only is False


def test_nats_disconnect_timeout_aborts_gate_and_fails_supervisor() -> None:
    async def _run() -> NatsDeliveryGate:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(reconnect_failure_timeout_seconds=0.01),
            consumer_role="execution",
            delivery_gate=gate,
        )
        bus._connected = True
        supervisor = asyncio.create_task(
            bus.wait_for_terminal_connection_failure()
        )
        await bus._on_disconnected()
        with pytest.raises(RuntimeError, match="nats_connection_terminal_failure"):
            await asyncio.wait_for(supervisor, timeout=0.2)
        return gate

    gate = asyncio.run(_run())
    assert gate.aborted is True


def test_non_transient_nats_client_error_is_terminal() -> None:
    async def _run() -> NatsDeliveryGate:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        supervisor = asyncio.create_task(
            bus.wait_for_terminal_connection_failure()
        )
        await bus._on_error(RuntimeError("simulated permissions violation"))
        with pytest.raises(RuntimeError, match="nats_connection_terminal_failure"):
            await asyncio.wait_for(supervisor, timeout=0.2)
        return gate

    gate = asyncio.run(_run())
    assert gate.aborted is True


def test_transient_nats_client_error_does_not_fail_supervisor() -> None:
    async def _run() -> tuple[bool, bool]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="market",
            delivery_gate=gate,
        )
        supervisor = asyncio.create_task(
            bus.wait_for_terminal_connection_failure()
        )
        await bus._on_error(OSError("simulated temporary transport error"))
        await asyncio.sleep(0)
        still_waiting = not supervisor.done()
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        return still_waiting, gate.aborted

    still_waiting, aborted = asyncio.run(_run())
    assert still_waiting is True
    assert aborted is False


def test_nats_reconnect_before_deadline_remains_healthy() -> None:
    async def _run() -> tuple[bool, bool]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(reconnect_failure_timeout_seconds=0.03),
            consumer_role="decision",
            delivery_gate=gate,
        )
        bus._connected = True
        supervisor = asyncio.create_task(
            bus.wait_for_terminal_connection_failure()
        )
        await bus._on_disconnected()
        await asyncio.sleep(0.005)
        await bus._on_reconnected()
        await asyncio.sleep(0.04)
        still_waiting = not supervisor.done()
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        return still_waiting, gate.aborted

    still_waiting, aborted = asyncio.run(_run())
    assert still_waiting is True
    assert aborted is False


class _ConsumerSupervisionJS:
    """先创建 durable，随后模拟管理面删除或持续不可达。"""

    def __init__(self, *, failure: str) -> None:
        self.failure = failure
        self.bound = False
        self.consumer_info_calls = 0
        self.failure_calls = 0
        self.first_failure = asyncio.Event()
        self.config: Any = None

    async def consumer_info(self, _stream: str, _durable: str) -> Any:
        from nats.js.errors import NotFoundError

        self.consumer_info_calls += 1
        if not self.bound:
            # subscribe() 的首次探测：durable 尚不存在，走正常创建路径。
            raise NotFoundError
        self.failure_calls += 1
        self.first_failure.set()
        if self.failure == "deleted":
            raise NotFoundError
        if self.failure == "unavailable":
            raise OSError("simulated JetStream management outage")
        raise AssertionError(f"unsupported failure mode: {self.failure}")

    async def subscribe(self, **kwargs: Any) -> _BoundConsumerSubscription:
        self.config = kwargs["config"].evolve(
            filter_subject=kwargs["subject"],
            deliver_subject="_INBOX.supervised",
        )
        self.bound = True
        return _BoundConsumerSubscription(
            _fake_consumer_info(self.config)
        )


class _ConcurrentUnavailableConsumerSupervisionJS:
    """Bind many durables, then keep every management query pending."""

    def __init__(self, *, expected_queries: int) -> None:
        self.expected_queries = expected_queries
        self.bound_durables: set[str] = set()
        self.in_flight_queries = 0
        self.max_in_flight_queries = 0
        self.all_queries_started = asyncio.Event()

    async def consumer_info(self, _stream: str, durable: str) -> Any:
        from nats.js.errors import NotFoundError

        if durable not in self.bound_durables:
            raise NotFoundError
        self.in_flight_queries += 1
        self.max_in_flight_queries = max(
            self.max_in_flight_queries,
            self.in_flight_queries,
        )
        if self.in_flight_queries >= self.expected_queries:
            self.all_queries_started.set()
        try:
            await asyncio.Future()
        finally:
            self.in_flight_queries -= 1

    async def subscribe(self, **kwargs: Any) -> _BoundConsumerSubscription:
        self.bound_durables.add(str(kwargs["durable"]))
        config = kwargs["config"].evolve(
            filter_subject=kwargs["subject"],
            deliver_subject=f"_INBOX.{kwargs['durable']}",
        )
        return _BoundConsumerSubscription(_fake_consumer_info(config))


class _AlternatingUnhealthyConsumerSupervisionJS:
    """Alternate management outages with an explicitly unbound push consumer."""

    def __init__(self) -> None:
        self.bound = False
        self.config: Any = None
        self.failure_calls = 0

    async def consumer_info(self, _stream: str, _durable: str) -> Any:
        from nats.js.errors import NotFoundError

        if not self.bound:
            raise NotFoundError
        self.failure_calls += 1
        if self.failure_calls % 2:
            raise OSError("simulated alternating management outage")
        return _fake_consumer_info(
            self.config,
            push_bound=False,
        )

    async def subscribe(self, **kwargs: Any) -> _BoundConsumerSubscription:
        self.config = kwargs["config"].evolve(
            filter_subject=kwargs["subject"],
            deliver_subject="_INBOX.supervised",
        )
        self.bound = True
        return _BoundConsumerSubscription(
            _fake_consumer_info(self.config)
        )


class _NoAckProgressConsumerSupervisionJS:
    """Vary broker counters while the business ACK floor remains fixed."""

    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.bound = False
        self.config: Any = None
        self.query_calls = 0

    async def consumer_info(self, _stream: str, _durable: str) -> Any:
        from nats.js.errors import NotFoundError

        if not self.bound:
            raise NotFoundError
        self.query_calls += 1
        delivered_consumer_seq = (
            self.query_calls if self.mode == "redelivery_growth" else 1
        )
        num_pending = self.query_calls if self.mode == "pending_growth" else 0
        return _fake_consumer_info(
            self.config,
            cursor=(1, delivered_consumer_seq, 0, 0),
            num_pending=num_pending,
            num_ack_pending=1,
        )

    async def subscribe(self, **kwargs: Any) -> _BoundConsumerSubscription:
        self.config = kwargs["config"].evolve(
            filter_subject=kwargs["subject"],
            deliver_subject="_INBOX.supervised",
        )
        self.bound = True
        return _BoundConsumerSubscription(
            _fake_consumer_info(self.config)
        )


class _BehaviorDriftConsumerSupervisionJS:
    """Create safely, then expose one dangerous broker-side config drift."""

    def __init__(self, *, drift_name: str) -> None:
        self.drift_name = drift_name
        self.bound = False
        self.config: Any = None
        self.query_calls = 0

    async def consumer_info(self, _stream: str, _durable: str) -> Any:
        from nats.js.errors import NotFoundError

        if not self.bound:
            raise NotFoundError
        self.query_calls += 1
        return _fake_consumer_info(self.config)

    async def subscribe(
        self, **kwargs: Any
    ) -> _BoundConsumerSubscription:
        safe_config = kwargs["config"].evolve(
            filter_subject=kwargs["subject"],
            deliver_subject="_INBOX.supervised",
        )
        broker_config = {
            "filter_subject": kwargs["subject"],
            "deliver_subject": "_INBOX.supervised",
        }
        broker_config.update(
            _unsafe_consumer_config_override(self.drift_name)
        )
        self.config = kwargs["config"].evolve(**broker_config)
        self.bound = True
        return _BoundConsumerSubscription(
            _fake_consumer_info(safe_config)
        )


class _ConsumerContinuitySupervisionJS:
    """Expose same-name recreation, rogue inbox, cursor reset, or progress."""

    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.bound = False
        self.config: Any = None
        self.query_calls = 0

    async def consumer_info(self, _stream: str, _durable: str) -> Any:
        from nats.js.errors import NotFoundError

        if not self.bound:
            raise NotFoundError
        self.query_calls += 1
        created = _TEST_CONSUMER_CREATED
        config = self.config
        cursor = (10, 10, 8, 8)
        if self.mode == "recreated":
            created = "2026-08-29T00:01:00Z"
            cursor = (0, 0, 0, 0)
        elif self.mode == "rogue_inbox":
            config = self.config.evolve(deliver_subject="_INBOX.rogue")
        elif self.mode == "cursor_reset":
            cursor = (9, 9, 7, 7)
        elif self.mode == "forward":
            offset = self.query_calls
            cursor = (10 + offset, 10 + offset, 8 + offset, 8 + offset)
        else:
            raise AssertionError(f"unsupported continuity mode: {self.mode}")
        return _fake_consumer_info(
            config,
            created=created,
            cursor=cursor,
        )

    async def subscribe(self, **kwargs: Any) -> _BoundConsumerSubscription:
        self.config = kwargs["config"].evolve(
            filter_subject=kwargs["subject"],
            deliver_subject="_INBOX.supervised",
        )
        self.bound = True
        return _BoundConsumerSubscription(
            _fake_consumer_info(
                self.config,
                cursor=(10, 10, 8, 8),
            )
        )


def test_pre_promotion_proof_rejects_disconnected_bus_and_aborts_gate() -> None:
    """Build 末尾已断线时不得等到稳态 supervisor 才撤销启动门禁。"""

    async def _run() -> NatsDeliveryGate:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        bus._js = object()  # type: ignore[assignment]
        bus._connected = False

        with pytest.raises(RuntimeError, match="nats_not_ready_for_promotion"):
            await bus.verify_ready_for_promotion()
        return gate

    gate = asyncio.run(_run())
    assert gate.aborted is True
    assert gate.activated is False


@pytest.mark.parametrize(
    "mode",
    ("recreated", "rogue_inbox", "cursor_reset"),
)
def test_pre_promotion_rejects_consumer_identity_or_cursor_discontinuity(
    mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same name/config cannot hide recreation, rebinding, or cursor reset."""

    async def _run() -> tuple[NatsDeliveryGate, bool]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        bus._js = _ConsumerContinuitySupervisionJS(  # type: ignore[assignment]
            mode=mode
        )
        bus._connected = True

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            await bus._supervise_critical_consumers_once(fail_fast=True)
            return gate, bus._terminal_connection_failure.is_set()
        finally:
            await bus.close()

    gate, terminal = asyncio.run(_run())
    assert terminal is True
    assert gate.aborted is True
    assert "_INBOX.supervised" not in caplog.text
    assert "_INBOX.rogue" not in caplog.text


def test_consumer_cursor_can_advance_without_false_terminal() -> None:
    """A stable created/inbox identity may advance every cursor component."""

    async def _run() -> tuple[bool, tuple[int, int, int, int]]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _ConsumerContinuitySupervisionJS(mode="forward")
        bus._js = js  # type: ignore[assignment]
        bus._connected = True

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            await bus._supervise_critical_consumers_once(fail_fast=True)
            await bus._supervise_critical_consumers_once(fail_fast=True)
            target = next(iter(bus._consumer_supervision_targets.values()))
            return bus._terminal_connection_failure.is_set(), target.cursor
        finally:
            await bus.close()

    terminal, cursor = asyncio.run(_run())
    assert terminal is False
    assert cursor == (12, 12, 10, 10)


@pytest.mark.parametrize(
    "drift_name",
    _UNSAFE_EXISTING_CONSUMER_CONFIG_FIELDS,
)
def test_pre_promotion_rejects_unsafe_consumer_config(
    drift_name: str,
) -> None:
    """A broker-side drift after bind must abort before READY promotion."""

    async def _run() -> tuple[NatsDeliveryGate, int]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _BehaviorDriftConsumerSupervisionJS(drift_name=drift_name)
        bus._js = js  # type: ignore[assignment]
        bus._connected = True

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            with pytest.raises(
                RuntimeError,
                match="nats_not_ready_for_promotion",
            ):
                await bus.verify_ready_for_promotion()
            return gate, js.query_calls
        finally:
            await bus.close()

    gate, query_calls = asyncio.run(_run())
    assert query_calls == 1
    assert gate.aborted is True
    assert gate.activated is False


def test_pre_promotion_proof_rejects_deleted_critical_durable_immediately() -> None:
    """关键 durable 在 build 尾部被删时，首次同步 proof 必须立即失败。"""

    async def _run() -> tuple[NatsDeliveryGate, _ConsumerSupervisionJS]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _ConsumerSupervisionJS(failure="deleted")
        bus._js = js  # type: ignore[assignment]
        bus._connected = True

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            with pytest.raises(
                RuntimeError,
                match="nats_not_ready_for_promotion",
            ):
                await bus.verify_ready_for_promotion()
            return gate, js
        finally:
            await bus.close()

    gate, js = asyncio.run(_run())
    assert gate.aborted is True
    assert gate.activated is False
    assert js.failure_calls == 1


def test_pre_promotion_proof_rechecks_connection_after_consumer_queries() -> None:
    """最后一次管理面查询期间断线时，不得用查询前的连接状态晋级。"""

    class _DisconnectingHealthyJS:
        def __init__(self, bus: NatsEventBus) -> None:
            self.bus = bus
            self.bound = False
            self.config: Any = None

        async def consumer_info(self, _stream: str, _durable: str) -> Any:
            from nats.js.errors import NotFoundError

            if not self.bound:
                raise NotFoundError
            self.bus._connected = False
            return _fake_consumer_info(self.config)

        async def subscribe(
            self, **kwargs: Any
        ) -> _BoundConsumerSubscription:
            self.config = kwargs["config"].evolve(
                filter_subject=kwargs["subject"],
                deliver_subject="_INBOX.supervised",
            )
            self.bound = True
            return _BoundConsumerSubscription(
                _fake_consumer_info(self.config)
            )

    async def _run() -> NatsDeliveryGate:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        bus._connected = True
        bus._js = _DisconnectingHealthyJS(bus)  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
        with pytest.raises(RuntimeError, match="nats_not_ready_for_promotion"):
            await bus.verify_ready_for_promotion()
        return gate

    gate = asyncio.run(_run())
    assert gate.aborted is True
    assert gate.activated is False


def test_deleted_critical_durable_aborts_gate_and_fails_supervisor() -> None:
    """Core TCP 仍活着时删除 ALL durable 也必须撤销 READY。"""

    async def _run() -> tuple[NatsDeliveryGate, _ConsumerSupervisionJS]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(
                consumer_supervision_interval_seconds=0.005,
                consumer_supervision_failure_timeout_seconds=0.04,
            ),
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _ConsumerSupervisionJS(failure="deleted")
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            with pytest.raises(
                RuntimeError,
                match="nats_connection_terminal_failure",
            ):
                await asyncio.wait_for(
                    bus.wait_for_terminal_connection_failure(),
                    timeout=0.25,
                )
            return gate, js
        finally:
            await bus.close()

    gate, js = asyncio.run(_run())
    assert gate.aborted is True
    assert js.consumer_info_calls >= 2
    assert js.failure_calls >= 1


def test_consumer_info_continuous_failure_is_bounded_before_terminal() -> None:
    """瞬态管理面错误可重试，但不能让 critical durable 永久伪健康。"""

    async def _run() -> tuple[NatsDeliveryGate, _ConsumerSupervisionJS]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(
                consumer_supervision_interval_seconds=0.005,
                consumer_supervision_failure_timeout_seconds=0.04,
            ),
            consumer_role="decision",
            delivery_gate=gate,
        )
        js = _ConsumerSupervisionJS(failure="unavailable")
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            supervisor = asyncio.create_task(
                bus.wait_for_terminal_connection_failure()
            )
            await asyncio.wait_for(js.first_failure.wait(), timeout=0.1)
            # 单次暂态查询失败不得立即杀进程；只有连续失败超过配置窗口才 terminal。
            await asyncio.sleep(0)
            assert supervisor.done() is False
            assert gate.aborted is False
            with pytest.raises(
                RuntimeError,
                match="nats_connection_terminal_failure",
            ):
                await asyncio.wait_for(supervisor, timeout=0.25)
            return gate, js
        finally:
            await bus.close()

    gate, js = asyncio.run(_run())
    assert gate.aborted is True
    assert js.failure_calls >= 2


def test_many_unavailable_consumers_share_one_bounded_supervision_window() -> None:
    """管理面整体挂起时，terminal deadline 不能随 durable 数量线性放大。"""

    topics = tuple(sorted(DEFAULT_CRITICAL_TOPICS)[:20])

    async def _run() -> tuple[NatsDeliveryGate, int, float]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(
                consumer_supervision_interval_seconds=0.02,
                consumer_supervision_failure_timeout_seconds=0.04,
            ),
            consumer_role="decision",
            delivery_gate=gate,
        )
        js = _ConcurrentUnavailableConsumerSupervisionJS(
            expected_queries=len(topics),
        )
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        for topic in topics:
            await bus.subscribe(topic, _noop_handler)
        started_at = asyncio.get_running_loop().time()
        supervisor = asyncio.create_task(
            bus.wait_for_terminal_connection_failure()
        )
        try:
            # A sequential implementation needs 20 * interval just to complete
            # one failed pass. All queries must instead enter the same window.
            await asyncio.wait_for(js.all_queries_started.wait(), timeout=0.15)
            with pytest.raises(
                RuntimeError,
                match="nats_connection_terminal_failure",
            ):
                await asyncio.wait_for(supervisor, timeout=0.25)
            elapsed = asyncio.get_running_loop().time() - started_at
            return gate, js.max_in_flight_queries, elapsed
        finally:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
            await bus.close()

    gate, max_in_flight, elapsed = asyncio.run(_run())
    assert max_in_flight == len(topics)
    assert elapsed < 0.25
    assert gate.aborted is True


def test_alternating_consumer_fault_kinds_share_one_continuous_failure_window() -> None:
    """故障类型变化不能重置持续不健康计时并让 READY 永久伪健康。"""

    async def _run() -> tuple[NatsDeliveryGate, int]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(
                consumer_supervision_interval_seconds=0.005,
                consumer_supervision_failure_timeout_seconds=0.04,
            ),
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _AlternatingUnhealthyConsumerSupervisionJS()
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            with pytest.raises(
                RuntimeError,
                match="nats_connection_terminal_failure",
            ):
                await asyncio.wait_for(
                    bus.wait_for_terminal_connection_failure(),
                    timeout=0.2,
                )
            return gate, js.failure_calls
        finally:
            await bus.close()

    gate, failure_calls = asyncio.run(_run())
    assert failure_calls >= 2
    assert gate.aborted is True


@pytest.mark.parametrize(
    "progress_mode",
    ("pending_growth", "redelivery_growth"),
)
def test_broker_activity_without_ack_progress_is_bounded(
    progress_mode: str,
) -> None:
    """新增积压或 broker 重投都不能冒充 handler 已完成业务处理。"""

    async def _run() -> tuple[NatsDeliveryGate, int]:
        gate = NatsDeliveryGate()
        assert gate.activate() is True
        bus = NatsEventBus(
            config=NatsBusConfig(
                ack_wait_seconds=0.005,
                consumer_supervision_interval_seconds=0.005,
                consumer_supervision_failure_timeout_seconds=0.04,
            ),
            consumer_role="execution",
            delivery_gate=gate,
        )
        js = _NoAckProgressConsumerSupervisionJS(mode=progress_mode)
        bus._js = js  # type: ignore[assignment]

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.subscribe(_topics.ORDER_INTENTS, _noop_handler)
            with pytest.raises(
                RuntimeError,
                match="nats_connection_terminal_failure",
            ):
                await asyncio.wait_for(
                    bus.wait_for_terminal_connection_failure(),
                    timeout=0.2,
                )
            return gate, js.query_calls
        finally:
            await bus.close()

    gate, query_calls = asyncio.run(_run())
    assert query_calls >= 2
    assert gate.aborted is True


def test_nats_unexpected_close_is_terminal_but_normal_close_is_not() -> None:
    class _Client:
        async def drain(self) -> None:
            return None

    async def _run() -> tuple[bool, bool]:
        unexpected_gate = NatsDeliveryGate()
        unexpected_bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="market",
            delivery_gate=unexpected_gate,
        )
        unexpected_supervisor = asyncio.create_task(
            unexpected_bus.wait_for_terminal_connection_failure()
        )
        await unexpected_bus._on_closed()
        with pytest.raises(RuntimeError, match="nats_connection_terminal_failure"):
            await asyncio.wait_for(unexpected_supervisor, timeout=0.2)

        normal_gate = NatsDeliveryGate()
        normal_bus = NatsEventBus(
            config=NatsBusConfig(reconnect_failure_timeout_seconds=0.01),
            consumer_role="gateway",
            delivery_gate=normal_gate,
        )
        normal_bus._client = _Client()  # type: ignore[assignment]
        normal_bus._connected = True
        normal_supervisor = asyncio.create_task(
            normal_bus.wait_for_terminal_connection_failure()
        )
        await normal_bus._on_disconnected()
        await normal_bus.close()
        await asyncio.sleep(0.02)
        normal_failed = normal_supervisor.done()
        normal_supervisor.cancel()
        await asyncio.gather(normal_supervisor, return_exceptions=True)
        return unexpected_gate.aborted, normal_failed

    unexpected_aborted, normal_failed = asyncio.run(_run())
    assert unexpected_aborted is True
    assert normal_failed is False


def test_delivery_gate_blocks_all_message_side_effects_until_activation() -> None:
    """PROVISIONING consumer 可注册，但 READY 前不能 parse/term/ack。"""

    async def _run() -> _FakeMsgWithTerm:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="decision",
            delivery_gate=gate,
        )
        on_msg = await _capture_on_msg(bus)
        msg = _FakeMsgWithTerm(b"{not valid json")
        callback = asyncio.create_task(on_msg(msg))
        await asyncio.sleep(0)
        assert callback.done() is False
        assert msg.term_called == 0
        assert msg.ack_called == 0
        assert msg.nak_called == 0
        assert gate.activate() is True
        await asyncio.wait_for(callback, timeout=0.2)
        return msg

    msg = asyncio.run(_run())
    assert msg.term_called == 1
    assert msg.ack_called == 0
    assert msg.nak_called == 0


def test_closed_delivery_gate_renews_jetstream_ack_wait_until_activation() -> None:
    """PROVISIONING 等待不能消耗 max_deliver；周期 +WPI 后仅执行一次。"""

    async def _run() -> _ProgressMsg:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(ack_wait_seconds=0.03),
            consumer_role="decision",
            delivery_gate=gate,
        )
        on_msg = await _capture_on_msg(bus)
        msg = _ProgressMsg(b"{not valid json")
        callback = asyncio.create_task(on_msg(msg))
        deadline = asyncio.get_running_loop().time() + 0.2
        while msg.progress_called < 2:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("delivery progress was not renewed")
            await asyncio.sleep(0.002)
        assert msg.term_called == 0
        assert gate.activate() is True
        await asyncio.wait_for(callback, timeout=0.2)
        return msg

    msg = asyncio.run(_run())
    assert msg.progress_called >= 2
    assert msg.term_called == 1
    assert msg.ack_called == 0
    assert msg.nak_called == 0


def test_delivery_progress_failure_aborts_gate_without_ack_or_nak() -> None:
    """无法延长 ack timer 时启动门禁必须 ABORT，不得静默耗尽重投。"""

    async def _run() -> tuple[NatsDeliveryGate, _ProgressMsg]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(ack_wait_seconds=0.003),
            consumer_role="decision",
            delivery_gate=gate,
        )
        on_msg = await _capture_on_msg(bus)
        msg = _ProgressMsg(b"{not valid json", fail_progress=True)
        with pytest.raises(RuntimeError, match="nats_delivery_progress_failed"):
            await asyncio.wait_for(on_msg(msg), timeout=0.2)
        return gate, msg

    gate, msg = asyncio.run(_run())
    assert gate.aborted is True
    assert gate.activated is False
    assert msg.progress_called == 1
    assert msg.term_called == 0
    assert msg.ack_called == 0
    assert msg.nak_called == 0


def test_close_aborts_blocked_delivery_before_unsubscribe_and_drain() -> None:
    """构建失败/关停必须唤醒 gate waiter，且消息保持未确认、无副作用。"""

    class _Subscription:
        def __init__(self) -> None:
            self.drained = False

        async def drain(self) -> None:
            self.drained = True

    class _Client:
        def __init__(self) -> None:
            self.drained = False

        async def drain(self) -> None:
            self.drained = True

    async def _run() -> tuple[_FakeMsgWithTerm, _Subscription, _Client]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        on_msg = await _capture_on_msg(bus)
        msg = _FakeMsgWithTerm(b"{not valid json")
        callback = asyncio.create_task(on_msg(msg))
        await asyncio.sleep(0)
        subscription = _Subscription()
        client = _Client()
        bus._subscriptions = [subscription]
        bus._client = client  # type: ignore[assignment]
        await asyncio.wait_for(bus.close(), timeout=0.2)
        await asyncio.wait_for(callback, timeout=0.2)
        return msg, subscription, client

    msg, subscription, client = asyncio.run(_run())
    assert msg.term_called == 0
    assert msg.ack_called == 0
    assert msg.nak_called == 0
    assert subscription.drained is True
    assert client.drained is True


def test_close_waits_for_in_flight_subscription_drain_before_returning() -> None:
    """Owner release 的 bus.close 前置必须等待旧 callback 真正退出。"""

    class _Subscription:
        def __init__(self) -> None:
            self.drain_started = asyncio.Event()
            self.handler_finished = asyncio.Event()

        async def drain(self) -> None:
            self.drain_started.set()
            await self.handler_finished.wait()

    class _Client:
        async def drain(self) -> None:
            return None

    async def _run() -> None:
        bus = NatsEventBus(config=NatsBusConfig(), consumer_role="execution")
        subscription = _Subscription()
        bus._subscriptions = [subscription]
        bus._client = _Client()  # type: ignore[assignment]

        closing = asyncio.create_task(bus.close())
        await asyncio.wait_for(subscription.drain_started.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not closing.done()
        subscription.handler_finished.set()
        await asyncio.wait_for(closing, timeout=0.2)

    asyncio.run(_run())


def test_subscription_drain_failure_keeps_bus_open_and_fails_close() -> None:
    class _Subscription:
        async def drain(self) -> None:
            raise RuntimeError("private callback detail")

    class _Client:
        def __init__(self) -> None:
            self.drained = False

        async def drain(self) -> None:
            self.drained = True

    async def _run() -> tuple[NatsEventBus, _Client]:
        bus = NatsEventBus(config=NatsBusConfig(), consumer_role="execution")
        client = _Client()
        bus._subscriptions = [_Subscription()]
        bus._client = client  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="nats_delivery_drain_failed"):
            await bus.close()
        return bus, client

    bus, client = asyncio.run(_run())
    assert client.drained is True
    assert bus._subscriptions
    assert bus._client is client


def test_pre_activation_publish_flushes_before_callback_gate_opens() -> None:
    """build 期 publish 不进 INTEREST stream；peer READY 后 flush 才开回调。"""

    from unittest.mock import AsyncMock

    async def _run() -> tuple[NatsDeliveryGate, AsyncMock, EventEnvelope]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        fake_js = AsyncMock()
        bus._js = fake_js  # type: ignore[assignment]
        envelope = _make_envelope(_topics.PORTFOLIO_SNAPSHOTS)
        await bus.publish_envelope(envelope, persist=False)
        fake_js.publish.assert_not_awaited()
        assert gate.activated is False
        await bus.activate_delivery()
        return gate, fake_js.publish, envelope

    gate, publish, envelope = asyncio.run(_run())
    assert gate.activated is True
    publish.assert_awaited_once()
    kwargs = publish.await_args.kwargs
    assert kwargs["subject"] == f"aats.{envelope.topic}"
    assert kwargs["headers"] == {"Nats-Msg-Id": envelope.event_id}


def test_pre_activation_flush_failure_keeps_gate_closed() -> None:
    """JetStream ack 失败必须让 startup 失败，不可开放 consumer callback。"""

    from unittest.mock import AsyncMock

    async def _run() -> NatsDeliveryGate:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        fake_js = AsyncMock()
        fake_js.publish.side_effect = RuntimeError("simulated broker failure")
        bus._js = fake_js  # type: ignore[assignment]
        await bus.publish_envelope(
            _make_envelope(_topics.PORTFOLIO_SNAPSHOTS),
            persist=False,
        )
        with pytest.raises(RuntimeError, match="simulated broker failure"):
            await bus.activate_delivery()
        return gate

    gate = asyncio.run(_run())
    assert gate.activated is False
    assert gate.aborted is False


def test_pre_activation_publication_buffer_has_strict_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """条数之外必须限制真实 payload bytes，避免大消息把启动进程撑爆。"""

    monkeypatch.setattr(
        "aats.bus.nats_bus._MAX_PRE_ACTIVATION_PUBLICATION_BYTES",
        9,
    )

    async def _run() -> tuple[int, int]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        bus._js = object()  # type: ignore[assignment]
        await bus._publish_or_defer_until_activation(
            subject="aats.one",
            body=b"12345",
            event_id="one",
        )
        with pytest.raises(
            RuntimeError,
            match="nats_pre_activation_publication_bytes_exceeded",
        ):
            await bus._publish_or_defer_until_activation(
                subject="aats.two",
                body=b"67890",
                event_id="two",
            )
        return (
            len(bus._pre_activation_publications),
            bus._pre_activation_publication_bytes,
        )

    count, byte_count = asyncio.run(_run())
    assert count == 1
    assert byte_count == 5


def test_abort_after_activation_transition_prevents_new_network_publish() -> None:
    """等待 activation 锁的 publisher 解锁后必须再次观察 ABORT。"""

    from unittest.mock import AsyncMock

    class _TransitionRaceGate:
        def __init__(self) -> None:
            self.abort_reads = 0

        @property
        def aborted(self) -> bool:
            self.abort_reads += 1
            # fast-path、入锁重检均未 abort；解锁后的最终重检观察到 ABORT。
            return self.abort_reads >= 3

        @property
        def activated(self) -> bool:
            # fast-path 先见 CLOSED，入锁后模拟 activation 已完成。
            return self.abort_reads >= 2

    async def _run() -> AsyncMock:
        gate = _TransitionRaceGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,  # type: ignore[arg-type]
        )
        fake_js = AsyncMock()
        bus._js = fake_js  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="nats_delivery_gate_aborted"):
            await bus._publish_or_defer_until_activation(
                subject="aats.test",
                body=b"payload",
                event_id="event",
            )
        return fake_js.publish

    publish = asyncio.run(_run())
    publish.assert_not_awaited()


def test_pre_activation_byte_accounting_survives_failed_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败项仍留队列时 byte counter 必须保持一致，成功 flush 后归零。"""

    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "aats.bus.nats_bus._MAX_PRE_ACTIVATION_PUBLICATION_BYTES",
        16,
    )

    async def _run() -> tuple[int, int, int, int]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        fake_js = AsyncMock()
        fake_js.publish.side_effect = RuntimeError("first flush failed")
        bus._js = fake_js  # type: ignore[assignment]
        await bus._publish_or_defer_until_activation(
            subject="aats.one",
            body=b"12345",
            event_id="one",
        )
        with pytest.raises(RuntimeError, match="first flush failed"):
            await bus.activate_delivery()
        failed_count = len(bus._pre_activation_publications)
        failed_bytes = bus._pre_activation_publication_bytes
        fake_js.publish.side_effect = None
        await bus.activate_delivery()
        return (
            failed_count,
            failed_bytes,
            len(bus._pre_activation_publications),
            bus._pre_activation_publication_bytes,
        )

    failed_count, failed_bytes, final_count, final_bytes = asyncio.run(_run())
    assert (failed_count, failed_bytes) == (1, 5)
    assert (final_count, final_bytes) == (0, 0)


def test_abort_during_flush_never_publishes_second_queued_message() -> None:
    """首条在途 publish 无法撤销，但 ABORT 后不得继续冲刷其余队列。"""

    class _BlockingJetStream:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.calls: list[str] = []

        async def publish(
            self,
            *,
            subject: str,
            payload: bytes,  # noqa: ARG002
            headers: dict[str, str],  # noqa: ARG002
        ) -> None:
            self.calls.append(subject)
            if len(self.calls) == 1:
                self.first_started.set()
                await self.release_first.wait()

    async def _run() -> tuple[NatsDeliveryGate, list[str]]:
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="execution",
            delivery_gate=gate,
        )
        fake_js = _BlockingJetStream()
        bus._js = fake_js  # type: ignore[assignment]
        await bus.publish_envelope(
            _make_envelope(_topics.PORTFOLIO_SNAPSHOTS, key="one"),
            persist=False,
        )
        await bus.publish_envelope(
            _make_envelope(_topics.PORTFOLIO_SNAPSHOTS, key="two"),
            persist=False,
        )
        activation = asyncio.create_task(bus.activate_delivery())
        await asyncio.wait_for(fake_js.first_started.wait(), timeout=0.2)
        gate.abort()
        fake_js.release_first.set()
        with pytest.raises(RuntimeError, match="nats_delivery_gate_aborted"):
            await activation
        return gate, fake_js.calls

    gate, calls = asyncio.run(_run())
    assert gate.aborted is True
    assert gate.activated is False
    assert calls == [f"aats.{_topics.PORTFOLIO_SNAPSHOTS}"]


def test_steady_state_publishes_are_not_serialized_by_activation_lock() -> None:
    """READY 后 publish 保持并发；单条 broker stall 不得全局 head-of-line。"""

    class _ConcurrentJetStream:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.second_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.calls = 0

        async def publish(self, **_kwargs: Any) -> None:
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                await self.release_first.wait()
            else:
                self.second_started.set()

    async def _run() -> None:
        gate = NatsDeliveryGate()
        assert gate.activate() is True
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="market",
            delivery_gate=gate,
        )
        fake_js = _ConcurrentJetStream()
        bus._js = fake_js  # type: ignore[assignment]
        first = asyncio.create_task(
            bus.publish_envelope(
                _make_envelope(_topics.MARKET_SNAPSHOTS, key="one"),
                persist=False,
            )
        )
        await asyncio.wait_for(fake_js.first_started.wait(), timeout=0.2)
        second = asyncio.create_task(
            bus.publish_envelope(
                _make_envelope(_topics.MARKET_SNAPSHOTS, key="two"),
                persist=False,
            )
        )
        await asyncio.wait_for(fake_js.second_started.wait(), timeout=0.2)
        await second
        fake_js.release_first.set()
        await first

    asyncio.run(_run())


def test_close_does_not_wait_on_hung_steady_publish_activation_lock() -> None:
    """close 先 ABORT 并进入 drain；不得被一个稳态 publish 的本地锁卡住。"""

    class _HungJetStream:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def publish(self, **_kwargs: Any) -> None:
            self.started.set()
            await self.release.wait()

    class _Client:
        def __init__(self) -> None:
            self.drained = False

        async def drain(self) -> None:
            self.drained = True

    async def _run() -> tuple[bool, bool]:
        gate = NatsDeliveryGate()
        assert gate.activate() is True
        bus = NatsEventBus(
            config=NatsBusConfig(),
            consumer_role="market",
            delivery_gate=gate,
        )
        fake_js = _HungJetStream()
        client = _Client()
        bus._js = fake_js  # type: ignore[assignment]
        bus._client = client  # type: ignore[assignment]
        publishing = asyncio.create_task(
            bus.publish_envelope(
                _make_envelope(_topics.MARKET_SNAPSHOTS),
                persist=False,
            )
        )
        await asyncio.wait_for(fake_js.started.wait(), timeout=0.2)
        await asyncio.wait_for(bus.close(), timeout=0.2)
        fake_js.release.set()
        await publishing
        return gate.aborted, client.drained

    aborted, drained = asyncio.run(_run())
    assert aborted is True
    assert drained is True


def test_on_msg_parse_error_terms_when_term_supported() -> None:
    """坏 JSON 在 nats-py 2.10+ 下必须 term()，不能 nak()——
    nak() 会触发 JetStream 立即重投，同一条坏 JSON 会再次 parse 失败，
    形成 NAK 循环直到 max_deliver 耗尽，期间 consumer 持续喷 error 日志。
    """
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")

    async def _run() -> _FakeMsgWithTerm:
        on_msg = await _capture_on_msg(bus)
        msg = _FakeMsgWithTerm(b"{not valid json")
        await on_msg(msg)
        return msg

    msg = asyncio.run(_run())
    assert msg.term_called == 1
    assert msg.ack_called == 0
    assert msg.nak_called == 0, "parse error must never nak() — would NAK-loop"


def test_on_msg_parse_error_falls_back_to_ack_when_no_term() -> None:
    """老 nats-py 没 term() 时必须 ack() 兜底——
    旧实现 fallback 到 nak() 会让坏消息被 JetStream 按
    max_deliver 重投几轮，每轮都 parse fail，消耗 consumer 配额
    并污染日志。ack() 直接消费掉，语义上把坏消息当黑洞处理。
    """
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")

    async def _run() -> _FakeMsgNoTerm:
        on_msg = await _capture_on_msg(bus)
        msg = _FakeMsgNoTerm(b"{not valid json")
        await on_msg(msg)
        return msg

    msg = asyncio.run(_run())
    assert msg.ack_called == 1
    assert msg.nak_called == 0, "parse error must never nak() — would NAK-loop"


def test_on_msg_schema_incompatible_terms_when_term_supported() -> None:
    """未来 schema 主版本跳到 2.x 时旧版 consumer 收到必须 term()，
    不能 nak() 让消息回到 stream 再被投递到同一组旧 consumer。
    """
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")

    async def _run() -> _FakeMsgWithTerm:
        on_msg = await _capture_on_msg(bus)
        envelope = EventEnvelope.model_validate(
            {
                "event_type": "test_event",
                "source_component": "unit_test",
                "topic": "market_snapshots",
                "key": "k1",
                "payload": {"value": 42},
                "schema_version": "2.0.0",
            }
        )
        msg = _FakeMsgWithTerm(envelope.model_dump_json().encode("utf-8"))
        await on_msg(msg)
        return msg

    msg = asyncio.run(_run())
    assert msg.term_called == 1
    assert msg.ack_called == 0
    assert msg.nak_called == 0, "schema incompatible must never nak() — would NAK-loop"


def test_on_msg_schema_incompatible_falls_back_to_ack_when_no_term() -> None:
    """老 nats-py 没 term() 时 schema 不兼容必须 ack()，理由同 parse error。"""
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")

    async def _run() -> _FakeMsgNoTerm:
        on_msg = await _capture_on_msg(bus)
        envelope = EventEnvelope.model_validate(
            {
                "event_type": "test_event",
                "source_component": "unit_test",
                "topic": "market_snapshots",
                "key": "k1",
                "payload": {"value": 42},
                "schema_version": "2.0.0",
            }
        )
        msg = _FakeMsgNoTerm(envelope.model_dump_json().encode("utf-8"))
        await on_msg(msg)
        return msg

    msg = asyncio.run(_run())
    assert msg.ack_called == 1
    assert msg.nak_called == 0, "schema incompatible must never nak() — would NAK-loop"


# ─────────────────────────────────────────────────────────────────────
# R5-X5：trace context extract 失败时必须落 WARNING，便于 ops 排查
# 分布式追踪断链的上游 producer；否则 consumer 侧 span 会变孤儿 root
# 却无日志线索。inject 侧已有 warning（publish_envelope ~line 1059），
# extract 侧原先是静默 try/except，R5-X5 补齐。
# ─────────────────────────────────────────────────────────────────────


def test_on_msg_logs_warning_when_trace_context_extract_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """envelope.trace_context 非空但 extract_trace_context 抛异常时必须
    落一条 WARNING（event_name=trace_context_extract_failed），并带上
    topic + event_id + error_type/error 方便定位。handler 仍应被调用
    (parent_ctx graceful fallback 到 None)，extract 失败不能阻塞业务。
    """
    import logging

    from aats.bus import nats_bus as _nats_bus_mod

    def _boom(_carrier: Any) -> Any:
        raise RuntimeError("carrier malformed")

    monkeypatch.setattr(_nats_bus_mod, "extract_trace_context", _boom)

    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")
    handler_called = 0

    async def _handler(_msg: dict[str, Any]) -> None:
        nonlocal handler_called
        handler_called += 1

    async def _run() -> _FakeMsgWithTerm:
        js = _CbCapturingJS()
        bus._js = js  # type: ignore[assignment]
        await bus.subscribe("market_snapshots", _handler)
        on_msg = js.captured_cb
        assert on_msg is not None
        envelope = EventEnvelope.model_validate(
            {
                "event_type": "test_event",
                "source_component": "unit_test",
                "topic": "market_snapshots",
                "key": "k1",
                "payload": {"value": 1},
                "schema_version": "1.0.0",
                "trace_context": {"traceparent": "garbage"},
            }
        )
        msg = _FakeMsgWithTerm(envelope.model_dump_json().encode("utf-8"))
        await on_msg(msg)
        return msg

    caplog.set_level(logging.WARNING, logger="aats.event_bus.nats")
    asyncio.run(_run())

    assert handler_called == 1, (
        "extract 失败必须 graceful fallback（parent_ctx=None），"
        "不能阻塞 handler 业务逻辑"
    )
    warn_records = [
        rec for rec in caplog.records
        if rec.name == "aats.event_bus.nats"
        and rec.levelno == logging.WARNING
        and "trace_context_extract_failed" in rec.getMessage()
    ]
    assert warn_records, (
        "extract 失败必须落 WARNING（event=trace_context_extract_failed），"
        "否则分布式追踪断链时 ops 无日志线索"
    )
    rendered = warn_records[0].getMessage()
    assert "topic=market_snapshots" in rendered
    assert "error_type=RuntimeError" in rendered
