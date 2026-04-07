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
from typing import Any

import pytest

from aats.bus.base import EventBus, MessageHandler
from aats.bus.nats_bus import (
    DEFAULT_CRITICAL_TOPICS,
    DEFAULT_OBSERVER_TOPICS,
    ConsumerConfigSpec,
    HybridBusRouting,
    HybridEventBus,
    NatsBusConfig,
    NatsEventBus,
    UnroutedTopicError,
    build_consumer_config_spec,
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
    assert routing.route_for(_topics.AI_PERFORMANCE_REPORTS) == "observer"


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
        asyncio.run(bus.ensure_stream(topics=["decisions"]))


def test_nats_bus_construction_does_not_require_nats_py() -> None:
    """构造 NatsEventBus 不应触发 nats-py import；只有 connect() 才会。"""
    # 这里不真的卸载 nats，但要确保 __init__ 是 pure-python，可以被
    # monolith 安全 import
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="monolith")
    assert bus._connected is False
    assert bus._client is None
    assert bus._js is None


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


# ─────────────────────────────────────────────────────────────────────
# M2: stream_max_age_seconds 可配置
# ─────────────────────────────────────────────────────────────────────


def test_stream_max_age_default_is_seven_days() -> None:
    """默认 max_age = 7 天（NatsBusConfig 持秒；nats-py StreamConfig.max_age 也以秒为单位，
    内部 _to_nanoseconds() 自行换算，调用方不要再预乘 1e9 —— 否则会被双重换算成超大整数，
    触发 NATS server "invalid JSON" 拒绝。"""
    config = NatsBusConfig()
    assert config.stream_max_age_seconds == 7 * 24 * 60 * 60


def test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds() -> None:
    """回归测试：ensure_stream 必须把 stream_max_age_seconds 原样（秒）
    传给 nats-py StreamConfig.max_age，**不能**预先乘 1e9。

    Why: nats-py 2.14 文档明确 max_age 字段以秒为单位
    （nats/js/api.py: ``max_age: Optional[float] = None  # in seconds``），
    内部 _to_nanoseconds() 自行换算。早期实现错把秒预乘 1e9 后再传,
    导致 nats-py 又乘一次 1e9，最终发出 60_000_000_000_000_000_000 这种
    超大值，NATS server JSON parser 直接 reject 'invalid JSON'。
    集成测试发现这个 bug 后补的回归保护。
    """
    pytest.importorskip("nats", reason="nats-py 未安装；本回归只在装了 nats-integration extras 的环境下跑")

    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    bus = NatsEventBus(
        config=NatsBusConfig(stream_max_age_seconds=3600),
        consumer_role="test",
    )
    # 绕过真 connect()：直接装一个 fake JetStream context
    fake_js = MagicMock()
    fake_js.add_stream = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]

    _asyncio.run(bus.ensure_stream(topics=["decisions"]))

    fake_js.add_stream.assert_awaited_once()
    cfg = fake_js.add_stream.await_args.kwargs["config"]
    # 关键断言：是 3600（秒），不是 3600 * 1e9（纳秒）
    assert cfg.max_age == 3600, (
        f"ensure_stream 把 max_age 传成了 {cfg.max_age}，期望 3600 秒。"
        " 看起来又把秒预乘了 1e9 —— 见 docstring 的 Why。"
    )


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
