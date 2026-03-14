from __future__ import annotations

from collections import defaultdict
from threading import Lock


class MetricsRegistry:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def increment(self, metric_name: str, value: int = 1) -> None:
        with self._lock:
            self._counts[metric_name] += value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
