"""Stage 8 代码准备：OpenTelemetry SDK 初始化骨架。

监督原则
========
多进程拆分后，单条请求的链路会跨 4 个进程：
    gateway_proc (行情) → market_proc (特征) → decision_proc (AI/策略)
    → execution_proc (订单提交) → exchange → fill 回流
任何一段慢都会拖累总体延迟。如果还像 monolith 那样靠 grep 日志排查，
基本不可能在事件密度上去后还能定位瓶颈。

OpenTelemetry 提供的解法是 W3C trace context：
- 一条 trace_id 跟随事件穿过 4 个进程
- 每个进程内的关键 span（process_market_event / generate_decision /
  submit_order / await_fill）按 parent_span_id 链接
- 所有 span 集中送到 Jaeger（已在 docker-compose.yml 部署）
- Grafana 里能直接按 trace_id 跳转

本模块只提供：
1. TelemetryConfig：从 settings/env 读取的配置 dataclass
2. configure_telemetry：惰性初始化 TracerProvider + OTLP exporter
3. shutdown_telemetry：退出时 flush + close
4. start_span helper：context manager 形式，与 monolith 现有 log_event 友好共存
5. inject_trace_context / extract_trace_context：跨进程边界（NATS 消息头/
   HTTP header）传递 W3C trace context

⚠️ 本模块的 configure_telemetry 不会被 build_runtime 自动调用。Stage 8
落地时，主入口（gateway_proc / market_proc / decision_proc / execution_proc）
启动时各调一次，传入对应的 service_name 和 process_role。

⚠️ opentelemetry-* 是可选依赖：本模块只在 configure_telemetry() 被调用时
import 真正的 SDK；其他时候用 no-op tracer，使得 monolith 不必装 OTel
也能 import 本文件。
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger, log_event

if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.trace import Span, Tracer  # type: ignore[import-not-found]


# ─────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TelemetryConfig:
    """OpenTelemetry SDK 初始化配置。

    所有字段都可以通过 AATS_OTEL_* 环境变量覆盖（与 settings 同前缀风格）。
    """

    # 服务名：在 Jaeger UI 里能看到的进程标识。建议 = process_role
    service_name: str = "aats-monolith"
    # 服务版本：从 git tag 或 CI 注入
    service_version: str = "0.0.0-dev"
    # 部署环境：dev/staging/prod，便于按环境过滤
    deployment_environment: str = "dev"
    # OTLP collector endpoint。docker-compose 里 jaeger 暴露 4317 (gRPC)
    otlp_endpoint: str = "http://127.0.0.1:4317"
    # 协议：grpc / http/protobuf
    otlp_protocol: str = "grpc"
    # 采样率：0.0 ~ 1.0；prod 建议 0.05 ~ 0.2
    trace_sample_ratio: float = 1.0
    # 批量导出参数
    export_batch_size: int = 512
    export_schedule_delay_millis: int = 5000
    # 关闭超时（flush）
    shutdown_timeout_seconds: float = 5.0
    # 进程角色，会作为 resource 属性写入每条 span
    process_role: str = "monolith"
    # 资源属性附加项
    extra_resource_attributes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, service_name: str | None = None, process_role: str | None = None) -> "TelemetryConfig":
        """从 AATS_OTEL_* 环境变量读取配置。

        process_role 显式参数优先，其次 AATS_PROCESS_ROLE 环境变量。
        """
        env_role = os.environ.get("AATS_PROCESS_ROLE", "").strip().lower() or "monolith"
        effective_role = (process_role or env_role).strip().lower() or "monolith"
        return cls(
            service_name=service_name or os.environ.get("AATS_OTEL_SERVICE_NAME", f"aats-{effective_role}"),
            service_version=os.environ.get("AATS_OTEL_SERVICE_VERSION", "0.0.0-dev"),
            deployment_environment=os.environ.get("AATS_OTEL_DEPLOYMENT_ENV", "dev"),
            otlp_endpoint=os.environ.get("AATS_OTEL_ENDPOINT", "http://127.0.0.1:4317"),
            otlp_protocol=os.environ.get("AATS_OTEL_PROTOCOL", "grpc"),
            trace_sample_ratio=float(os.environ.get("AATS_OTEL_SAMPLE_RATIO", "1.0")),
            export_batch_size=int(os.environ.get("AATS_OTEL_BATCH_SIZE", "512")),
            export_schedule_delay_millis=int(os.environ.get("AATS_OTEL_BATCH_DELAY_MS", "5000")),
            shutdown_timeout_seconds=float(os.environ.get("AATS_OTEL_SHUTDOWN_TIMEOUT", "5.0")),
            process_role=effective_role,
        )


# ─────────────────────────────────────────────────────────────────────
# 全局状态
# ─────────────────────────────────────────────────────────────────────


_telemetry_state: dict[str, Any] = {
    "configured": False,
    "tracer_provider": None,
    "tracer": None,
    "config": None,
    "noop_tracer": None,
}

_telemetry_logger = get_logger("aats.telemetry")


# ─────────────────────────────────────────────────────────────────────
# No-op tracer（OTel 未安装或未 configure 时使用）
# ─────────────────────────────────────────────────────────────────────


class _NoopSpan:
    """最小化兼容的 span 占位符，覆盖 OTel Span 常用方法。"""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D401
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoopTracer:
    """没有 OTel 时使用：返回 _NoopSpan，所有调用都是 no-op。"""

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *args: Any, **kwargs: Any) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


def _get_noop_tracer() -> _NoopTracer:
    if _telemetry_state["noop_tracer"] is None:
        _telemetry_state["noop_tracer"] = _NoopTracer()
    return _telemetry_state["noop_tracer"]  # type: ignore[no-any-return]


# ─────────────────────────────────────────────────────────────────────
# 初始化 / 关闭
# ─────────────────────────────────────────────────────────────────────


def configure_telemetry(config: TelemetryConfig) -> bool:
    """惰性初始化 OpenTelemetry SDK。

    返回 True 表示真正配置了 OTel SDK；False 表示 OTel 包没装，
    本进程将继续使用 no-op tracer（不会抛错）。
    """
    if _telemetry_state["configured"]:
        log_event(
            _telemetry_logger,
            "telemetry_already_configured",
            level="warning",
            service_name=config.service_name,
        )
        return True

    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
            BatchSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import (  # type: ignore[import-not-found]
            TraceIdRatioBased,
        )
    except ImportError:
        log_event(
            _telemetry_logger,
            "telemetry_otel_not_installed",
            level="warning",
            service_name=config.service_name,
            hint="pip install opentelemetry-sdk opentelemetry-exporter-otlp",
        )
        _telemetry_state["configured"] = True  # 标记为已尝试，避免重复 warn
        _telemetry_state["tracer"] = _get_noop_tracer()
        _telemetry_state["config"] = config
        return False

    # 选择 OTLP exporter（grpc 或 http）
    exporter: Any
    try:
        if config.otlp_protocol.lower() == "http" or "http" in config.otlp_protocol.lower():
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint)
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint, insecure=True)
    except ImportError as exc:
        log_event(
            _telemetry_logger,
            "telemetry_otlp_exporter_not_installed",
            level="warning",
            error=str(exc),
            hint="pip install opentelemetry-exporter-otlp",
        )
        _telemetry_state["configured"] = True
        _telemetry_state["tracer"] = _get_noop_tracer()
        _telemetry_state["config"] = config
        return False

    resource_attrs: dict[str, str] = {
        "service.name": config.service_name,
        "service.version": config.service_version,
        "deployment.environment": config.deployment_environment,
        "aats.process_role": config.process_role,
    }
    resource_attrs.update(config.extra_resource_attributes)

    provider = TracerProvider(
        resource=Resource.create(resource_attrs),
        sampler=TraceIdRatioBased(config.trace_sample_ratio),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_export_batch_size=config.export_batch_size,
            schedule_delay_millis=config.export_schedule_delay_millis,
        )
    )
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("aats", config.service_version)

    _telemetry_state["configured"] = True
    _telemetry_state["tracer_provider"] = provider
    _telemetry_state["tracer"] = tracer
    _telemetry_state["config"] = config

    log_event(
        _telemetry_logger,
        "telemetry_configured",
        service_name=config.service_name,
        process_role=config.process_role,
        endpoint=config.otlp_endpoint,
        protocol=config.otlp_protocol,
        sample_ratio=config.trace_sample_ratio,
    )
    return True


def shutdown_telemetry() -> None:
    """退出时刷出未导出的 span 并关闭 provider。"""
    provider = _telemetry_state.get("tracer_provider")
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:  # pragma: no cover
        log_event(
            _telemetry_logger,
            "telemetry_shutdown_failed",
            level="error",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    _telemetry_state["tracer_provider"] = None
    _telemetry_state["tracer"] = None
    _telemetry_state["configured"] = False


# ─────────────────────────────────────────────────────────────────────
# Tracer 访问 helper
# ─────────────────────────────────────────────────────────────────────


def get_tracer() -> "Tracer | _NoopTracer":
    """取得当前 tracer。未 configure 时返回 _NoopTracer。"""
    tracer = _telemetry_state.get("tracer")
    if tracer is None:
        return _get_noop_tracer()
    return tracer  # type: ignore[no-any-return]


@contextlib.contextmanager
def start_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """轻量 span 包装，统一 caller 写法。

    用法::

        with start_span("decision.run_cycle", attributes={"symbol": "BTC-USDT"}) as span:
            ...
            span.set_attribute("decision.confidence", 0.72)
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, v)
                except Exception:
                    # set_attribute 在某些 OTel 版本对 None / 复杂类型挑剔
                    pass
        yield span


# ─────────────────────────────────────────────────────────────────────
# 跨进程 trace context 传播
# ─────────────────────────────────────────────────────────────────────
#
# 实现思路：W3C TraceContext 通过 traceparent / tracestate 两个 header
# 跨边界传播。NATS message header 和 HTTP header 都能直接放。
#
# inject_trace_context(carrier)：把当前 span 的 trace context 写到 carrier
#                                （dict-like），生产端 publish 前调用
# extract_trace_context(carrier)：把 carrier 里的 trace context 还原成
#                                 OTel context，消费端处理事件前调用
#
# 没装 OTel 时 inject/extract 都是 no-op，carrier 不会被破坏。


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """把当前活动 span 的 W3C trace context 写入 carrier。"""
    try:
        from opentelemetry.propagate import inject  # type: ignore[import-not-found]
    except ImportError:
        return
    inject(carrier)


def extract_trace_context(carrier: Mapping[str, str]) -> Any:
    """从 carrier 还原 W3C trace context。

    返回的 context 应当作为 with use_context(...) 之类的 OTel API 输入。
    没装 OTel 时返回 None。
    """
    try:
        from opentelemetry.propagate import extract  # type: ignore[import-not-found]
    except ImportError:
        return None
    return extract(dict(carrier))


# ─────────────────────────────────────────────────────────────────────
# 内部 helper：测试用
# ─────────────────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """单元测试用：重置全局状态以隔离 test cases。"""
    _telemetry_state.update(
        {
            "configured": False,
            "tracer_provider": None,
            "tracer": None,
            "config": None,
            "noop_tracer": None,
        }
    )
