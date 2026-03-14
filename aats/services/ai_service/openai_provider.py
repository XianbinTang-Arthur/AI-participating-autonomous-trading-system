from __future__ import annotations

import json
from time import perf_counter

import httpx

from aats.bootstrap.settings import AATSSettings
from aats.services.ai_service.provider import AIProviderError, AIProviderResponse, AIProviderTimeoutError


class OpenAIProvider:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    async def generate_assessment(
        self,
        *,
        prompt: str,
        response_schema: dict[str, object],
    ) -> AIProviderResponse:
        if not self.settings.openai_api_key:
            raise AIProviderError("openai_api_key_not_configured")

        request_started = perf_counter()
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.ai_model_name,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a deterministic market analysis model. "
                        "Return only valid JSON that matches the provided schema."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ai_market_assessment",
                    "strict": True,
                    "schema": response_schema,
                },
            },
        }
        timeout = httpx.Timeout(self.settings.ai_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.openai_base_url.rstrip("/"),
                timeout=timeout,
            ) as client:
                response = await client.post("/v1/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AIProviderTimeoutError("openai_request_timeout") from exc
        except httpx.HTTPError as exc:
            raise AIProviderError(f"openai_http_error:{exc}") from exc

        data = response.json()
        choice = data["choices"][0]["message"]
        if choice.get("refusal"):
            raise AIProviderError(f"openai_refusal:{choice['refusal']}")
        content = choice.get("content")
        if not isinstance(content, str):
            raise AIProviderError("openai_missing_content")

        try:
            parsed_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AIProviderError("openai_invalid_json_content") from exc

        request_id = response.headers.get("x-request-id")
        latency_ms = (perf_counter() - request_started) * 1000.0
        return AIProviderResponse(
            provider_name="openai",
            request_id=request_id,
            latency_ms=latency_ms,
            payload=parsed_payload,
        )
