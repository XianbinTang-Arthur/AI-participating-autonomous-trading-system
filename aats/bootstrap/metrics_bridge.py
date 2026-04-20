"""MetricsRegistry → OpenTelemetry Counter 桥接模块。

设计文档：docs/task/grafana_audit_fix_design.md §D1

问题
====
``MetricsRegistry`` 是纯进程内 ``dict[str, int]`` 计数器，通过
``/system/metrics`` 暴露为 JSON——Grafana 无法直接查询。

方案
====
把 ``MetricsRegistry.snapshot()`` 的增量同步到 OTel Counter，
由 ``PrometheusMetricReader``（:9464）以 Prometheus exposition format 暴露，
Prometheus server 采集后 Grafana 即可用 PromQL 查询。

            MetricsRegistry.increment("decision_cycles")
                   │
                   ▼
            snapshot() 每 30 秒读一次
                   │
                   ▼
            OTel Counter.add(delta)
                   │
                   ▼
            PrometheusMetricReader :9464/metrics
                   │
                   ▼
            Prometheus server (scrape)
                   │
                   ▼
            Grafana Prometheus datasource

⚠️ 如果 OTel 未安装或 ``get_meter()`` 返回 None，桥接退化为 no-op。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.telemetry import get_meter

_logger = get_logger("aats.metrics_bridge")

# 同步间隔（秒）。30s 与 Prometheus scrape_interval 对齐。
_SYNC_INTERVAL_SECONDS = 30.0


def create_bridge(registry: MetricsRegistry) -> Callable[[], None] | None:
    """创建 MetricsRegistry → OTel Counter 桥接函数。

    返回 ``sync_once`` 可调用对象（每次调用同步增量）；
    如果 OTel meter 不可用则返回 None。
    """
    meter = get_meter()
    if meter is None:
        log_event(
            _logger,
            "metrics_bridge_skipped",
            level="info",
            reason="OTel meter not available; metrics bridge disabled",
        )
        return None

    # 为每个已知的 MetricsRegistry 指标创建 OTel Counter。
    # 使用 _total 后缀是 Prometheus 命名约定（Counter 类型）。
    counters: dict[str, Any] = {}
    last_snapshot: dict[str, int] = {}
    # P0-b Task 2.4：labeled counters 复用同一个 OTel Counter，通过 attributes
    # 区分 label 组（Prometheus 层面自动展开为多条 series）。
    labeled_counters: dict[str, Any] = {}
    last_labeled_snapshot: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    def _ensure_counter(name: str) -> Any:
        """惰性创建 Counter——支持运行时新增的指标名。"""
        if name not in counters:
            counters[name] = meter.create_counter(
                name=f"aats_{name}",
                description=f"AATS counter: {name}",
                unit="1",
            )
        return counters[name]

    def _ensure_labeled_counter(name: str) -> Any:
        """惰性创建带 attributes 的 Counter。"""
        if name not in labeled_counters:
            labeled_counters[name] = meter.create_counter(
                name=f"aats_{name}",
                description=f"AATS labeled counter: {name}",
                unit="1",
            )
        return labeled_counters[name]

    def sync_once() -> None:
        """读取 MetricsRegistry 快照，计算增量，写入 OTel Counter。"""
        current = registry.snapshot()
        for metric_name, current_value in current.items():
            previous = last_snapshot.get(metric_name, 0)
            delta = current_value - previous
            if delta > 0:
                counter = _ensure_counter(metric_name)
                try:
                    counter.add(delta)
                except Exception:
                    pass  # OTel SDK 异常不影响业务
            last_snapshot[metric_name] = current_value

        # P0-b Task 2.4：同步 labeled counters。
        labeled_current = registry.labeled_snapshot()
        for (metric_name, label_tuple), current_value in labeled_current.items():
            previous = last_labeled_snapshot.get((metric_name, label_tuple), 0)
            delta = current_value - previous
            if delta > 0:
                counter = _ensure_labeled_counter(metric_name)
                try:
                    counter.add(delta, attributes=dict(label_tuple))
                except Exception:
                    pass  # OTel SDK 异常不影响业务
            last_labeled_snapshot[(metric_name, label_tuple)] = current_value

    log_event(
        _logger,
        "metrics_bridge_created",
        sync_interval_seconds=_SYNC_INTERVAL_SECONDS,
    )
    return sync_once


async def start_metrics_bridge_loop(
    registry: MetricsRegistry,
    *,
    interval: float = _SYNC_INTERVAL_SECONDS,
) -> None:
    """后台 asyncio task：定期同步 MetricsRegistry 到 OTel Counter。

    应当通过 ``asyncio.create_task()`` 启动。task 被 cancel 时安静退出。
    """
    sync_fn = create_bridge(registry)
    if sync_fn is None:
        return  # OTel 不可用，静默退出

    log_event(
        _logger,
        "metrics_bridge_loop_started",
        interval_seconds=interval,
    )
    try:
        while True:
            try:
                sync_fn()
            except Exception as exc:
                log_event(
                    _logger,
                    "metrics_bridge_sync_error",
                    level="warning",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        # 最后同步一次，确保关停前的增量不丢
        try:
            sync_fn()
        except Exception:
            pass
