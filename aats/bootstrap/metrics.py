from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Mapping


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
        """
        if not isinstance(labels, Mapping):
            raise TypeError("labels must be a Mapping[str, str]")
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
