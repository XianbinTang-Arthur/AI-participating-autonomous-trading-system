from __future__ import annotations

from typing import Any, Mapping, TypeVar

from pydantic import BaseModel

from aats.bus.base import EventBus
from aats.schemas.common import EventEnvelope, dump_payload_exact

ModelT = TypeVar("ModelT", bound=BaseModel)


def build_envelope(
    *,
    topic: str,
    key: str,
    payload_model: BaseModel,
    source_component: str,
) -> EventEnvelope:
    return EventEnvelope(
        event_type=payload_model.__class__.__name__,
        source_component=source_component,
        topic=topic,
        key=key,
        payload=dump_payload_exact(payload_model),
    )


async def publish_model(
    *,
    bus: EventBus,
    topic: str,
    key: str,
    payload_model: BaseModel,
    source_component: str,
) -> EventEnvelope:
    envelope = build_envelope(
        topic=topic,
        key=key,
        payload_model=payload_model,
        source_component=source_component,
    )
    await bus.publish(topic=topic, key=key, payload=envelope.model_dump(mode="json"))
    return envelope


def parse_envelope(message: Mapping[str, Any]) -> EventEnvelope:
    return EventEnvelope.model_validate(message["payload"])


def parse_payload(message: Mapping[str, Any], model_type: type[ModelT]) -> ModelT:
    envelope = parse_envelope(message)
    return model_type.model_validate(envelope.payload)
