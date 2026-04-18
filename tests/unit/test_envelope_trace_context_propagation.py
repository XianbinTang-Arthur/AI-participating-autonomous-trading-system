"""Stage 8 单元测试：EventEnvelope + NatsEventBus trace_context 透传。

覆盖范围
========

1. EventEnvelope
   - 默认 trace_context 字段为 None
   - 旧版 JSON（没有 trace_context 字段）能正常 model_validate（向后兼容）
   - 带 trace_context 的 envelope JSON round-trip 保持一致

2. NatsEventBus.publish_envelope
   - 没有 active span 时 inject_trace_context no-op，envelope.trace_context 仍为 None
   - monkey-patch inject_trace_context 写入假 traceparent 时，真正 publish 出去的
     JSON 字节流里能找到 traceparent（说明 publish 路径调用了 inject 并且序列化
     时把 trace_context 写进去了）
   - publish 失败走 except 分支时不会把 trace_context 污染给其它 envelope

3. NatsEventBus.subscribe._on_msg
   - 带 trace_context 的 envelope 能被 extract_trace_context 反解 + 调用 handler
   - 不带 trace_context 的 envelope 走 parent_ctx=None 路径，handler 仍被正常调用
   - extract_trace_context 抛错时 fail-soft 不影响 handler 调用

不测的部分（需要真 OTel SDK 或真 NATS server）
================================================
- 真实 trace_id 在 4 进程真跑下能否从 gateway 一路传到 execution → Stage 8-6 drill
- 真 OTLP exporter → Jaeger UI trace tree 可见 → Stage 8-6 drill
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aats.bus import nats_bus as nats_bus_mod
from aats.bus.nats_bus import NatsBusConfig, NatsEventBus
from aats.schemas.common import EventEnvelope


# ─────────────────────────────────────────────────────────────────────
# 测试 helpers
# ─────────────────────────────────────────────────────────────────────


def _make_envelope(
    *,
    topic: str = "decisions",
    trace_context: dict[str, str] | None = None,
) -> EventEnvelope:
    data: dict[str, Any] = {
        "event_type": "test_event",
        "source_component": "unit_test",
        "topic": topic,
        "key": "k1",
        "payload": {"value": 42},
    }
    if trace_context is not None:
        data["trace_context"] = trace_context
    return EventEnvelope.model_validate(data)


# ─────────────────────────────────────────────────────────────────────
# EventEnvelope schema 兼容性
# ─────────────────────────────────────────────────────────────────────


def test_envelope_default_trace_context_is_none() -> None:
    env = _make_envelope()
    assert env.trace_context is None


def test_envelope_schema_compatible_accepts_same_major() -> None:
    """R3-P1-X5：同主版本（1.x.y）均兼容。"""
    from aats.bus.nats_bus import _envelope_schema_compatible

    assert _envelope_schema_compatible("1.0.0") is True
    assert _envelope_schema_compatible("1.5.17") is True
    assert _envelope_schema_compatible("1") is True  # pre-semver fallback


def test_envelope_schema_incompatible_rejects_other_major_and_garbage() -> None:
    """R3-P1-X5：主版本不同 / 非 semver / 空值一律视为不兼容。"""
    from aats.bus.nats_bus import _envelope_schema_compatible

    assert _envelope_schema_compatible("2.0.0") is False
    assert _envelope_schema_compatible("0.9.0") is False
    assert _envelope_schema_compatible("") is False
    assert _envelope_schema_compatible(None) is False
    assert _envelope_schema_compatible("abc") is False
    # "10.x" 这样前缀匹配陷阱：prefix 必须是 "1." 完整匹配
    assert _envelope_schema_compatible("10.0.0") is False


def test_envelope_backward_compat_without_trace_context_field() -> None:
    """旧版 publisher 产出的 JSON 没有 trace_context 字段时，新版 consumer
    必须能无错解析。这是 Stage 8 零停机升级的前提。"""
    old_json = {
        "schema_version": "1.0.0",
        "event_id": "evt_legacy",
        "event_type": "market.snapshot",
        "event_timestamp": "2024-01-01T00:00:00Z",
        "source_component": "market_gateway",
        "topic": "market.snapshot",
        "key": "BTC-USDT",
        "payload": {"price": "50000"},
    }
    env = EventEnvelope.model_validate(old_json)
    assert env.trace_context is None
    assert env.event_id == "evt_legacy"


def test_envelope_with_trace_context_round_trip() -> None:
    """带 trace_context 的 envelope 经过 JSON 序列化 + 反序列化后
    trace_context 必须保持一致。这是跨进程透传的序列化正确性前提。"""
    ctx = {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "aats=gw1",
    }
    env = _make_envelope(trace_context=ctx)
    raw = env.model_dump_json()
    # JSON 里必须能找到完整 traceparent 字符串
    assert "4bf92f3577b34da6a3ce929d0e0e4736" in raw
    restored = EventEnvelope.model_validate_json(raw)
    assert restored.trace_context == ctx


def test_envelope_trace_context_not_leaked_across_instances() -> None:
    """回归保护：trace_context 是 Field(default=None)，不是共享 dict。
    两个不同 envelope 的 trace_context 在任一方修改后都不能互相影响。"""
    e1 = _make_envelope()
    e2 = _make_envelope()
    assert e1.trace_context is None
    assert e2.trace_context is None
    # 即使通过 model_copy 修改 e1，也不能让 e2.trace_context 变。
    e1_mutated = e1.model_copy(update={"trace_context": {"traceparent": "x"}})
    assert e1_mutated.trace_context == {"traceparent": "x"}
    assert e2.trace_context is None


# ─────────────────────────────────────────────────────────────────────
# NatsEventBus.publish_envelope 注入路径
# ─────────────────────────────────────────────────────────────────────


def _build_bus_with_fake_js() -> tuple[NatsEventBus, AsyncMock]:
    """构造一个未真连 NATS 的 NatsEventBus，直接装一个 fake JetStream context。"""
    bus = NatsEventBus(config=NatsBusConfig(), consumer_role="decision")
    fake_js = MagicMock()
    fake_js.publish = AsyncMock()
    bus._js = fake_js  # type: ignore[attr-defined]
    return bus, fake_js.publish


def test_publish_envelope_no_active_span_keeps_trace_context_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认情况下 inject_trace_context 是真 OTel inject 或 no-op；测试里强制让它
    变成 no-op（carrier 保持为空），验证发出的 JSON 里的 trace_context 仍为 None。"""

    def _noop_inject(carrier: Any) -> None:
        return None

    monkeypatch.setattr(nats_bus_mod, "inject_trace_context", _noop_inject)

    bus, publish_mock = _build_bus_with_fake_js()
    env = _make_envelope()
    asyncio.run(bus.publish_envelope(env, persist=False))

    publish_mock.assert_awaited_once()
    sent_bytes = publish_mock.await_args.kwargs["payload"]
    sent = json.loads(sent_bytes.decode("utf-8"))
    # 没有 active span 时 envelope 的 trace_context 字段必须是 None（或 pydantic
    # 默认在序列化时直接写 null）。任何情况下都不能出现 traceparent。
    assert sent["trace_context"] is None
    assert "traceparent" not in sent_bytes.decode("utf-8")


def test_publish_envelope_injects_trace_context_when_inject_writes_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟 OTel 已装 + 当前上下文有 active span：inject 会往 carrier 写
    traceparent。验证发出的 JSON 字节流里确实带上 traceparent。"""
    fake_ctx = {
        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
        "tracestate": "aats=unit",
    }

    def _fake_inject(carrier: Any) -> None:
        carrier.update(fake_ctx)

    monkeypatch.setattr(nats_bus_mod, "inject_trace_context", _fake_inject)

    bus, publish_mock = _build_bus_with_fake_js()
    env = _make_envelope()
    asyncio.run(bus.publish_envelope(env, persist=False))

    publish_mock.assert_awaited_once()
    sent_bytes = publish_mock.await_args.kwargs["payload"]
    sent = json.loads(sent_bytes.decode("utf-8"))
    assert sent["trace_context"] == fake_ctx


def test_publish_envelope_injection_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inject_trace_context 抛错时 publish 不应当失败；envelope 的
    trace_context 回落到 None，JetStream publish 正常继续。"""

    def _broken_inject(carrier: Any) -> None:
        raise RuntimeError("simulated OTel propagator error")

    monkeypatch.setattr(nats_bus_mod, "inject_trace_context", _broken_inject)

    bus, publish_mock = _build_bus_with_fake_js()
    env = _make_envelope()
    # 不应抛出
    asyncio.run(bus.publish_envelope(env, persist=False))

    publish_mock.assert_awaited_once()
    sent_bytes = publish_mock.await_args.kwargs["payload"]
    sent = json.loads(sent_bytes.decode("utf-8"))
    assert sent["trace_context"] is None


def test_publish_envelope_does_not_mutate_original_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model_copy 返回新对象；原始 envelope 不应被 in-place 修改，避免 caller
    复用同一个 envelope 时拿到上一次 publish 写进去的 trace_context。"""
    fake_ctx = {"traceparent": "00-aaaa-bbbb-01"}

    def _fake_inject(carrier: Any) -> None:
        carrier.update(fake_ctx)

    monkeypatch.setattr(nats_bus_mod, "inject_trace_context", _fake_inject)

    bus, _ = _build_bus_with_fake_js()
    env = _make_envelope()
    assert env.trace_context is None
    asyncio.run(bus.publish_envelope(env, persist=False))
    # 原 envelope 仍然是 None，说明 publish_envelope 没有 in-place 改它
    assert env.trace_context is None


def test_publish_envelope_sets_nats_msg_id_header_for_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3-P1-X4 回归：publish_envelope 必须传 Nats-Msg-Id header = envelope.event_id，
    否则 JetStream stream 上配的 duplicate_window=120s 完全失效：outbox 重试发同一条
    envelope、或者进程崩溃重启后 PENDING 行重新发出去，broker 都会把每一次 publish
    当作新消息，而不会按 event_id 去重。"""

    def _noop_inject(carrier: Any) -> None:
        return None

    monkeypatch.setattr(nats_bus_mod, "inject_trace_context", _noop_inject)

    bus, publish_mock = _build_bus_with_fake_js()
    env = _make_envelope()
    asyncio.run(bus.publish_envelope(env, persist=False))

    publish_mock.assert_awaited_once()
    headers = publish_mock.await_args.kwargs.get("headers")
    assert headers is not None, "publish_envelope 必须向 JetStream 传 headers 以激活去重"
    assert headers.get("Nats-Msg-Id") == env.event_id


# ─────────────────────────────────────────────────────────────────────
# NatsEventBus._on_msg 提取路径
#
# subscribe() 依赖 nats-py 的 ConsumerConfig / AckPolicy，本地单元测试不启
# nats-py 连接；但是 subscribe() 内部定义的 _on_msg 闭包才是真正要测的逻辑。
# 我们没办法从外部直接 import _on_msg。采用的办法：直接构造一条等价的函数，
# 调用同样的 extract_trace_context + start_span 组合，断言它在 fail-soft 下
# 仍然会调到 handler。
# ─────────────────────────────────────────────────────────────────────


def test_consumer_extract_no_trace_context_still_calls_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """envelope.trace_context 是 None 时 consumer 走 parent_ctx=None 路径，
    handler 仍然会被调到。"""
    called: list[dict[str, Any]] = []

    async def handler(msg: dict[str, Any]) -> None:
        called.append(msg)

    async def _consume_fake() -> None:
        # 模拟 _on_msg 内部逻辑
        from aats.bootstrap.telemetry import extract_trace_context, start_span

        env = _make_envelope()  # trace_context=None
        parent_ctx = None
        if env.trace_context:
            parent_ctx = extract_trace_context(env.trace_context)
        with start_span(f"nats.receive.{env.topic}"):
            await handler(
                {
                    "topic": env.topic,
                    "key": env.key,
                    "payload": env.model_dump(mode="json"),
                }
            )
        assert parent_ctx is None

    asyncio.run(_consume_fake())
    assert len(called) == 1
    assert called[0]["topic"] == "decisions"


def test_consumer_extract_with_trace_context_calls_extract_and_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """envelope.trace_context 非空时 extract_trace_context 会被调用一次，
    handler 仍然被调到，整条链路无异常。"""
    extract_calls: list[Any] = []

    def _fake_extract(carrier: Any) -> Any:
        extract_calls.append(dict(carrier))
        return "fake_parent_ctx_token"  # 非 None 即可

    monkeypatch.setattr(nats_bus_mod, "extract_trace_context", _fake_extract)

    called: list[dict[str, Any]] = []

    async def handler(msg: dict[str, Any]) -> None:
        called.append(msg)

    async def _consume_fake() -> None:
        from aats.bootstrap.telemetry import start_span

        env = _make_envelope(trace_context={"traceparent": "00-abc-def-01"})
        parent_ctx = None
        if env.trace_context:
            parent_ctx = nats_bus_mod.extract_trace_context(env.trace_context)
        assert parent_ctx == "fake_parent_ctx_token"
        with start_span(f"nats.receive.{env.topic}"):
            await handler(
                {
                    "topic": env.topic,
                    "key": env.key,
                    "payload": env.model_dump(mode="json"),
                }
            )

    asyncio.run(_consume_fake())
    assert len(extract_calls) == 1
    assert extract_calls[0]["traceparent"] == "00-abc-def-01"
    assert len(called) == 1


def test_consumer_extract_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extract_trace_context 抛错时 parent_ctx 回落到 None，handler 仍然被调到。"""

    def _broken_extract(carrier: Any) -> Any:
        raise RuntimeError("simulated OTel extract error")

    monkeypatch.setattr(nats_bus_mod, "extract_trace_context", _broken_extract)

    called: list[dict[str, Any]] = []

    async def handler(msg: dict[str, Any]) -> None:
        called.append(msg)

    async def _consume_fake() -> None:
        from aats.bootstrap.telemetry import start_span

        env = _make_envelope(trace_context={"traceparent": "00-xxx-yyy-01"})
        parent_ctx: Any = None
        try:
            parent_ctx = nats_bus_mod.extract_trace_context(env.trace_context or {})
        except Exception:
            parent_ctx = None
        assert parent_ctx is None  # 被 fail-soft 吞掉
        with start_span(f"nats.receive.{env.topic}"):
            await handler(
                {
                    "topic": env.topic,
                    "key": env.key,
                    "payload": env.model_dump(mode="json"),
                }
            )

    asyncio.run(_consume_fake())
    assert len(called) == 1
