from __future__ import annotations

from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class SchemaBase(BaseModel):
    model_config = ConfigDict()
    schema_version: str = "1.0.0"
    created_at: datetime = Field(default_factory=utc_now)


def dump_payload_exact(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return dump_payload_exact(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): dump_payload_exact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [dump_payload_exact(item) for item in value]
    return value


class EventEnvelope(SchemaBase):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    event_type: str
    event_timestamp: datetime = Field(default_factory=utc_now)
    source_component: str
    topic: str
    key: str
    payload: dict[str, Any]


class SymbolTimeframe(BaseModel):
    symbol: str
    timeframe: Literal["15m", "1h"]
