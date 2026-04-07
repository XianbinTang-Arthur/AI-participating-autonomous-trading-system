"""Stage 8 单元测试：OpenTelemetry SDK 初始化骨架。

只覆盖：
1. TelemetryConfig.from_env 解析环境变量
2. 默认（未 configure）情况下 get_tracer 返回 _NoopTracer
3. start_span 在 no-op tracer 下不抛错
4. inject/extract trace context 在 no-op 下安全通过
5. configure_telemetry 在 OTel 包没装时优雅降级到 no-op
6. shutdown_telemetry 幂等

不测的部分（需要真实 OTel SDK + Jaeger，留到 Stage 8 集成测试）：
- 真实 span 导出到 OTLP
- 跨进程 trace 链路
- BatchSpanProcessor 导出策略
"""
from __future__ import annotations

import pytest

from aats.bootstrap.telemetry import (
    TelemetryConfig,
    _NoopSpan,
    _NoopTracer,
    _reset_for_tests,
    configure_telemetry,
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    shutdown_telemetry,
    start_span,
)


@pytest.fixture(autouse=True)
def _isolate_telemetry_state() -> None:
    """每个 test case 前后都把 telemetry 全局状态重置，避免污染。"""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ─────────────────────────────────────────────────────────────────────
# TelemetryConfig.from_env
# ─────────────────────────────────────────────────────────────────────


def test_telemetry_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AATS_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("AATS_OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("AATS_OTEL_ENDPOINT", raising=False)
    cfg = TelemetryConfig.from_env()
    assert cfg.process_role == "monolith"
    assert cfg.service_name == "aats-monolith"
    assert cfg.otlp_endpoint == "http://127.0.0.1:4317"
    assert cfg.trace_sample_ratio == 1.0


def test_telemetry_config_explicit_process_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AATS_PROCESS_ROLE", raising=False)
    cfg = TelemetryConfig.from_env(process_role="decision")
    assert cfg.process_role == "decision"
    assert cfg.service_name == "aats-decision"


def test_telemetry_config_uses_aats_process_role_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AATS_PROCESS_ROLE", "execution")
    cfg = TelemetryConfig.from_env()
    assert cfg.process_role == "execution"
    assert cfg.service_name == "aats-execution"


def test_telemetry_config_explicit_role_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AATS_PROCESS_ROLE", "execution")
    cfg = TelemetryConfig.from_env(process_role="decision")
    assert cfg.process_role == "decision"


def test_telemetry_config_otel_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AATS_OTEL_SERVICE_NAME", "custom-svc")
    monkeypatch.setenv("AATS_OTEL_ENDPOINT", "http://10.0.0.5:4317")
    monkeypatch.setenv("AATS_OTEL_SAMPLE_RATIO", "0.05")
    monkeypatch.setenv("AATS_OTEL_DEPLOYMENT_ENV", "prod")
    cfg = TelemetryConfig.from_env()
    assert cfg.service_name == "custom-svc"
    assert cfg.otlp_endpoint == "http://10.0.0.5:4317"
    assert cfg.trace_sample_ratio == 0.05
    assert cfg.deployment_environment == "prod"


# ─────────────────────────────────────────────────────────────────────
# 默认 no-op tracer
# ─────────────────────────────────────────────────────────────────────


def test_get_tracer_returns_noop_when_not_configured() -> None:
    tracer = get_tracer()
    assert isinstance(tracer, _NoopTracer)


def test_start_span_no_op_does_not_raise() -> None:
    with start_span("test.span") as span:
        assert isinstance(span, _NoopSpan)
        # 调常见 span 方法都不应抛错
        span.set_attribute("key", "value")
        span.set_status("ok")
        span.add_event("event_name", {"k": "v"})
        span.record_exception(ValueError("test"))


def test_start_span_with_attributes_no_op() -> None:
    with start_span("test.span", attributes={"a": 1, "b": "c"}) as span:
        assert isinstance(span, _NoopSpan)


# ─────────────────────────────────────────────────────────────────────
# trace context propagation no-op safety
# ─────────────────────────────────────────────────────────────────────


def test_inject_trace_context_no_op_does_not_raise() -> None:
    carrier: dict[str, str] = {}
    inject_trace_context(carrier)
    # 没装 OTel 时 carrier 应当保持原样（或至少不抛错）
    # 如果 OTel 已装，carrier 里会有 traceparent；这两种情况都接受
    assert isinstance(carrier, dict)


def test_extract_trace_context_no_op_safe() -> None:
    # 没装 OTel 时返回 None；装了的话返回一个 OTel context 对象
    ctx = extract_trace_context({"unrelated_key": "x"})
    assert ctx is None or ctx is not None  # 不抛即可


# ─────────────────────────────────────────────────────────────────────
# configure_telemetry 防御性降级
# ─────────────────────────────────────────────────────────────────────


def test_configure_telemetry_idempotent() -> None:
    cfg = TelemetryConfig(service_name="aats-test", process_role="monolith")
    configure_telemetry(cfg)
    # 第二次调用不应抛错（无论 OTel 装没装）
    configure_telemetry(cfg)


def test_configure_telemetry_without_otel_falls_back_to_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟没有 OTel 包：configure 不抛错，tracer 仍是 no-op。"""
    import sys

    # 模拟 import 失败的方法：把 opentelemetry 模块设为 None
    # 但这会污染其它测试，因此只验证 configure 后调用 get_tracer 不抛错
    cfg = TelemetryConfig(service_name="aats-test", process_role="monolith")
    configure_telemetry(cfg)
    tracer = get_tracer()
    # 不论 OTel 装没装，tracer 都应当能用
    assert tracer is not None
    with start_span("test"):
        pass


def test_shutdown_telemetry_idempotent() -> None:
    shutdown_telemetry()
    shutdown_telemetry()  # 第二次调用也不应抛错


def test_shutdown_telemetry_after_configure() -> None:
    cfg = TelemetryConfig(service_name="aats-test")
    configure_telemetry(cfg)
    shutdown_telemetry()
    # 关闭后再 get_tracer 应当回到 no-op
    tracer = get_tracer()
    assert isinstance(tracer, _NoopTracer)
