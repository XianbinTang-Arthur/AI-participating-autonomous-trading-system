"""进程内高频流式快照缓存。

替代 Postgres event_store 为 MARKET_SNAPSHOTS / FEATURE_SNAPSHOTS 提供
latest() 和 recent_by_key() 查询。每个进程维护自己的内存副本，由 bus
的发布路径和接收路径同步更新。

设计要点:
  - 纯 in-memory，无持久化；进程重启后由 NATS JetStream replay 补充。
  - latest(topic, key) 和 latest(topic) 两种查询模式均支持。
  - recent_by_key 使用 deque(maxlen) 自动淘汰旧条目。
  - 线程安全：dict/deque 的单次赋值/append 在 CPython GIL 下原子。
"""

from __future__ import annotations

from collections import defaultdict, deque

from aats.events import topics as _topics
from aats.schemas.common import EventEnvelope

# 由 StreamSnapshotCache 管理的 topic 集合。
# bus 层根据此集合决定"更新缓存 + 跳过 Postgres"。
STREAM_CACHE_TOPICS: frozenset[str] = frozenset({
    _topics.MARKET_SNAPSHOTS,
    _topics.FEATURE_SNAPSHOTS,
})

_DEFAULT_MAX_RECENT = 50


class StreamSnapshotCache:
    """进程内高频快照缓存，提供与 EventStore 兼容的同步读接口。"""

    def __init__(self, max_recent: int = _DEFAULT_MAX_RECENT) -> None:
        self._max_recent = max_recent
        # latest 值：(topic, key) → envelope，(topic, None) → 该 topic 全局最新
        self._latest: dict[tuple[str, str | None], EventEnvelope] = {}
        # 近期历史：(topic, key) → deque[envelope]
        self._recent: dict[tuple[str, str], deque[EventEnvelope]] = defaultdict(
            lambda: deque(maxlen=self._max_recent)
        )

    # ------------------------------------------------------------------
    # 写入（由 bus 调用）
    # ------------------------------------------------------------------

    def update(self, envelope: EventEnvelope) -> None:
        """记录一条新快照。由 bus 在 publish 和 receive 路径调用。"""
        topic = envelope.topic
        key = envelope.key
        self._latest[(topic, key)] = envelope
        self._latest[(topic, None)] = envelope  # 全局最新（无 key 过滤）
        if key is not None:
            self._recent[(topic, key)].append(envelope)

    # ------------------------------------------------------------------
    # 读取（由消费者调用，接口与 EventStore 对齐）
    # ------------------------------------------------------------------

    def latest(self, topic: str, key: str | None = None) -> EventEnvelope | None:
        """返回指定 topic（可选 key）的最新快照。"""
        return self._latest.get((topic, key))

    def recent_by_key(self, topic: str, key: str, limit: int) -> list[EventEnvelope]:
        """返回指定 topic+key 的最近 *limit* 条快照，按时间升序。"""
        items = self._recent.get((topic, key))
        if items is None:
            return []
        if limit >= len(items):
            return list(items)
        return list(items)[-limit:]
