from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class AIProviderError(RuntimeError):
    pass


class AIProviderTimeoutError(AIProviderError):
    pass


class AIProviderResponse(BaseModel):
    provider_name: str
    request_id: str | None = None
    latency_ms: float | None = None
    payload: dict[str, Any]


class AIProvider(Protocol):
    async def generate_assessment(
        self,
        *,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> AIProviderResponse:
        ...
