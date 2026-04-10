"""进程内高频流式快照缓存。

替代 Postgres event_store 为 MARKET_SNAPSHOTS / FEATURE_SNAPSHOTS 提供
latest() 和 recent_by_key() 查询。每个进程维护自己的内存副本，由 bus
的发布路径和接收路径同步更新。

设计要点:
  - 纯 in-memory，无持久化；进程重启后由 bootstrap 从 HotStateStore（Redis）
    恢复 latest + recent 条目，再由 NATS JetStream replay 补充后续增量。
  - latest(topic, key) 和 latest(topic) 两种查询模式均支持。
  - recent_by_key 使用 deque(maxlen) 自动淘汰旧条目。
  - 线程安全：dict/deque 的单次赋值/append 在 CPython GIL 下原子。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Sequence

from aats.events import topics as _topics
from aats.schemas.common import EventEnvelope
from aats.storage.hot_state_store import make_key

if TYPE_CHECKING:
    from aats.storage.hot_state_store import HotStateStore

# 由 StreamSnapshotCache 管理的 topic 集合。
# bus 层根据此集合决定"更新缓存 + 跳过 Postgres"。
STREAM_CACHE_TOPICS: frozenset[str] = frozenset({
    _topics.MARKET_SNAPSHOTS,
    _topics.FEATURE_SNAPSHOTS,
})

_DEFAULT_MAX_RECENT = 50

_NS_STREAM_CACHE = "stream_cache"
_KEY_LATEST = "latest"
_KEY_RECENT = "recent"


def _redis_key_latest(topic: str, key: str) -> str:
    """Redis key：``aats:hot:stream_cache:latest:<topic>:<key>``。"""
    safe_topic = topic.replace(".", "_")
    return make_key(_NS_STREAM_CACHE, _KEY_LATEST, safe_topic, key)


def _redis_key_recent(topic: str, key: str) -> str:
    """Redis key：``aats:hot:stream_cache:recent:<topic>:<key>``。"""
    safe_topic = topic.replace(".", "_")
    return make_key(_NS_STREAM_CACHE, _KEY_RECENT, safe_topic, key)


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
        # bootstrap / flush 支持
        self._hot_state_store: HotStateStore | None = None
        self._logger: logging.Logger | None = None
        self._dirty_keys: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Bootstrap（启动期 Redis hydration）
    # ------------------------------------------------------------------

    async def bootstrap(
        self,
        *,
        hot_state_store: HotStateStore,
        symbols: Sequence[str],
        logger: logging.Logger | None = None,
    ) -> None:
        """从 HotStateStore（Redis）恢复每个 topic+symbol 的 latest 和 recent。

        高频 topic 不落 Postgres，NATS durable consumer 重启后只从上次 ack
        续收新消息。bootstrap 从 Redis 读取上次 flush 写入的 latest 快照和
        recent 窗口，消除重启后到新 tick 到来之间的空窗。

        latest 恢复保证 decision context 能拿到行情/特征快照；recent 恢复
        保证 spot_grid / pullback-only DCA 的 lookback 窗口不会误判历史不足。

        所有失败都走 best-effort 路径（log warning），不阻塞 build_runtime。
        """
        self._hot_state_store = hot_state_store
        self._logger = logger
        hydrated_latest = 0
        hydrated_recent = 0
        for topic in STREAM_CACHE_TOPICS:
            for symbol in symbols:
                # ── latest ──
                latest_key = _redis_key_latest(topic, symbol)
                try:
                    stored: Any = await hot_state_store.get(latest_key)
                except Exception as exc:
                    if logger:
                        logger.warning(
                            "stream_snapshot_cache_bootstrap_latest_failed "
                            "topic=%s symbol=%s error=%s",
                            topic, symbol, exc,
                        )
                    stored = None
                if isinstance(stored, dict):
                    try:
                        envelope = EventEnvelope.model_validate(stored)
                        self._latest[(topic, symbol)] = envelope
                        self._latest[(topic, None)] = envelope
                        hydrated_latest += 1
                    except Exception as exc:
                        if logger:
                            logger.warning(
                                "stream_snapshot_cache_bootstrap_latest_parse_failed "
                                "topic=%s symbol=%s error=%s",
                                topic, symbol, exc,
                            )

                # ── recent ──
                recent_key = _redis_key_recent(topic, symbol)
                try:
                    stored_list: Any = await hot_state_store.get(recent_key)
                except Exception as exc:
                    if logger:
                        logger.warning(
                            "stream_snapshot_cache_bootstrap_recent_failed "
                            "topic=%s symbol=%s error=%s",
                            topic, symbol, exc,
                        )
                    stored_list = None
                if isinstance(stored_list, list):
                    dq = self._recent[(topic, symbol)]
                    restored = 0
                    for item in stored_list:
                        if not isinstance(item, dict):
                            continue
                        try:
                            env = EventEnvelope.model_validate(item)
                            dq.append(env)
                            restored += 1
                        except Exception:
                            continue
                    if restored > 0:
                        hydrated_recent += restored
                        # latest 可能因 Redis 时序比 recent 旧，用 recent
                        # 最后一条补齐
                        last = dq[-1]
                        existing_latest = self._latest.get((topic, symbol))
                        if (
                            existing_latest is None
                            or last.event_timestamp >= existing_latest.event_timestamp
                        ):
                            self._latest[(topic, symbol)] = last
                            self._latest[(topic, None)] = last

        if logger:
            logger.info(
                "stream_snapshot_cache_bootstrap_complete "
                "hydrated_latest=%d hydrated_recent=%d",
                hydrated_latest, hydrated_recent,
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
            self._dirty_keys.add((topic, key))

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

    # ------------------------------------------------------------------
    # 远端订阅（保持非 producer role 的缓存新鲜）
    # ------------------------------------------------------------------

    async def register_remote_subscription(self, bus: Any) -> None:
        """订阅 MARKET_SNAPSHOTS / FEATURE_SNAPSHOTS 远端事件。

        production 路径入口：``_wire_event_subscriptions`` 在 ``_CollectingBus``
        上调本方法。bus 层的 receive 路径在分发 handler 之前就会调
        ``stream_snapshot_cache.update(envelope)``，所以这里的 handler 只需
        作为占位，确保本进程对这些 topic 建立 NATS 订阅、持续收到消息。

        和 PortfolioSnapshotCache / ObligationHotStateCache 同模板：bootstrap
        时 subscribe=False，订阅推迟到 _wire_event_subscriptions 走
        _CollectingBus 聚合，避免 NATS durable binding 冲突。
        """
        for topic in STREAM_CACHE_TOPICS:
            try:
                await bus.subscribe(topic, self._noop_handler)
            except Exception as exc:
                if self._logger:
                    self._logger.warning(
                        "stream_snapshot_cache_subscribe_failed "
                        "topic=%s error=%s",
                        topic, exc,
                    )

    @staticmethod
    async def _noop_handler(message: Any) -> None:
        """占位 handler。缓存更新由 bus 层在分发前完成。"""

    # ------------------------------------------------------------------
    # 异步 flush（由后台任务定期调用，将 latest + recent 持久化到 Redis）
    # ------------------------------------------------------------------

    async def flush_to_hot_state(self) -> None:
        """将 dirty latest + recent 条目 best-effort 写入 Redis。

        由 Runtime._flush_stream_cache_loop 定期调用。update() 是 sync 路径
        不能 await，所以 Redis 持久化只能在独立的 async flush 循环里做。
        失败的 key 会重新加入 dirty 集合，下次 flush 重试。
        """
        if self._hot_state_store is None:
            return
        dirty = list(self._dirty_keys)
        self._dirty_keys.clear()
        for topic, key in dirty:
            failed = False
            # ── latest ──
            envelope = self._latest.get((topic, key))
            if envelope is not None:
                try:
                    await self._hot_state_store.set(
                        _redis_key_latest(topic, key),
                        envelope.model_dump(mode="json"),
                    )
                except Exception as exc:
                    failed = True
                    if self._logger:
                        self._logger.warning(
                            "stream_snapshot_cache_flush_latest_failed "
                            "topic=%s key=%s error=%s",
                            topic, key, exc,
                        )

            # ── recent ──
            dq = self._recent.get((topic, key))
            if dq is not None and len(dq) > 0:
                try:
                    serialized = [e.model_dump(mode="json") for e in dq]
                    await self._hot_state_store.set(
                        _redis_key_recent(topic, key),
                        serialized,
                    )
                except Exception as exc:
                    failed = True
                    if self._logger:
                        self._logger.warning(
                            "stream_snapshot_cache_flush_recent_failed "
                            "topic=%s key=%s error=%s",
                            topic, key, exc,
                        )

            if failed:
                self._dirty_keys.add((topic, key))
