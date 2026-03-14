from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class SchemaBase(BaseModel):
    schema_version: str = "1.0.0"
    created_at: datetime = Field(default_factory=utc_now)


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

