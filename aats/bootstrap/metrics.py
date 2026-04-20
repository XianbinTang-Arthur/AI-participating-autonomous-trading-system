from __future__ import annotations

import logging
from collections import defaultdict
from threading import Lock
from typing import Mapping

_logger = logging.getLogger("aats.metrics")


class MetricsRegistry:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        # P0-b Task 2.4: labeled counters。用于 Prometheus 暴露带 label 的指标
        # （如 ``aats_runtime_ai_operating_mode{mode="baseline_only"}``）。
        # key 为 ``(metric_name, tuple(sorted(labels.items())))``。
        # 两个字典物理分开，避免 legacy ``snapshot()`` 消费方被 label 条目污染。
        self._labeled_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._lock = Lock()

    def increment(self, metric_name: str, value: int = 1) -> None:
        with self._lock:
            self._counts[metric_name] += value

    def increment_labeled(
        self,
        metric_name: str,
        *,
        labels: Mapping[str, str],
        value: int = 1,
    ) -> None:
        """按 label 组递增计数器。

        P0-b Task 2.4 引入：用于 runtime mode 等需要按枚举维度区分的指标。
        labels 必须是字符串→字符串映射，内部转 sorted tuple 确保 key 稳定。

        2026-04-20 code review A-L3: 非 Mapping 输入时不再 raise TypeError
        (caller 都 `except Exception: pass`, 异常被吞 → 开发期错误永不可见).
        改成 warn log, 让错误至少出现在 Loki, 便于追溯"metric 不 emit 的原因".
        """
        if not isinstance(labels, Mapping):
            _logger.warning(
                "increment_labeled: labels 必须是 Mapping[str, str], "
                "传入类型 %r, metric=%r 的 labeled count 本次跳过",
                type(labels).__name__,
                metric_name,
            )
            return
        key = (metric_name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._labeled_counts[key] += value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def labeled_snapshot(
        self,
    ) -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
        """返回 labeled counter 的快照。

        调用方（metrics_bridge）用它产出带 attributes 的 OTel Counter。
        """
        with self._lock:
            return dict(self._labeled_counts)
