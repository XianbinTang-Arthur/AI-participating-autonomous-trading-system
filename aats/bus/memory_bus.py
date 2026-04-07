from __future__ import annotations

import asyncio
from collections import defaultdict

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus, MessageHandler
from aats.schemas.common import EventEnvelope
from aats.storage.base import EventStore


class InMemoryEventBus(EventBus):
    def __init__(
        self,
        event_store: EventStore | None = None,
        *,
        persistence_mode: str = "strict",
    ) -> None:
        self._subs: dict[str, list[MessageHandler]] = defaultdict(list)
        self._event_store = event_store
        self._persistence_mode = persistence_mode
        self.logger = get_logger("aats.event_bus")

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        await self.publish_envelope(EventEnvelope.model_validate(payload), persist=True)

    async def publish_envelope(self, envelope: EventEnvelope, *, persist: bool = True) -> None:
        if persist and self._event_store is not None:
            try:
                # 把同步 DB 写入丢进 thread pool，避免在事件循环主线程上做
                # 阻塞 I/O。decision_engine.run_cycle 在一个周期里会调 5+ 次
                # publish_model，每次都会触发 event_store.append 的 SELECT+INSERT+
                # COMMIT。如果直接在 event loop 里跑，单个周期会把 HTTP 请求
                # （包括 dashboard bundle / 静态资源）卡住数秒到数十秒。
                # 用 asyncio.to_thread 后，每次 publish 都会让出 event loop，
                # API handler 有机会被调度。
                await asyncio.to_thread(self._event_store.append, envelope)
            except Exception as exc:
                log_event(
                    self.logger,
                    "event_persistence_failed",
                    level="error",
                    topic=envelope.topic,
                    key=envelope.key,
                    persistence_mode=self._persistence_mode,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if self._persistence_mode == "strict":
                    raise

        message = {"topic": envelope.topic, "key": envelope.key, "payload": envelope.model_dump(mode="json")}
        first_error: Exception | None = None
        for handler in tuple(self._subs[envelope.topic]):
            try:
                await handler(message)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                else:
                    # All handlers are called regardless of failures, but only the
                    # first exception is raised.  Log subsequent ones so they are
                    # visible in diagnostics instead of being silently discarded.
                    log_event(
                        self.logger,
                        "event_handler_error_suppressed",
                        level="error",
                        topic=envelope.topic,
                        key=envelope.key,
                        handler=getattr(handler, "__qualname__", str(handler)),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
        if first_error is not None:
            raise first_error

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self._subs[topic].append(handler)
