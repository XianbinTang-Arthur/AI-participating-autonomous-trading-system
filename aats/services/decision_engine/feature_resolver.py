"""Feature snapshot 统一读取器。

保证 decision context 内行情基准一致性：按 context.feature_snapshot_ref
（即 event_id）精确取同一条 EventEnvelope，而不是 latest() 模式。

读取链:
  1. StreamSnapshotCache.get(event_id) — 内存精确匹配
  2. EventStore.get(event_id) — Postgres 回退（覆盖旧数据 / 非缓存 topic）

两步都 miss 则返回 None，由调用方决定是否 raise。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aats.schemas.common import EventEnvelope
from aats.storage.base import EventStore

if TYPE_CHECKING:
    from aats.storage.stream_snapshot_cache import StreamSnapshotCache


class FeatureSnapshotResolver:
    """按 event_id 精确读取 feature snapshot。

    供 BaselineStrategy、AIInferenceService 等所有需要按 decision context
    引用取 feature 的消费者统一使用，避免各自实现不同的读取 / 回退链。
    """

    def __init__(
        self,
        *,
        event_store: EventStore,
        stream_snapshot_cache: "StreamSnapshotCache | None" = None,
    ) -> None:
        self._stream_cache = stream_snapshot_cache
        self._event_store = event_store

    def resolve(self, ref: str) -> EventEnvelope | None:
        """按 event_id 精确解析 feature snapshot。

        Parameters
        ----------
        ref:
            ``DecisionContext.feature_snapshot_ref``，即目标 EventEnvelope
            的 ``event_id``。

        Returns
        -------
        匹配的 EventEnvelope，或 None（两级缓存均 miss）。
        """
        # 1. StreamSnapshotCache 精确匹配（内存 O(1)）
        if self._stream_cache is not None:
            event = self._stream_cache.get(ref)
            if event is not None:
                return event
        # 2. Postgres EventStore 回退
        return self._event_store.get(ref)
