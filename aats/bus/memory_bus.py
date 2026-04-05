from __future__ import annotations

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
                self._event_store.append(envelope)
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
