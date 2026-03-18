from __future__ import annotations

from copy import deepcopy
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
        strict_schema = self._strict_json_schema(response_schema)
        payload = {
            "model": self.settings.ai_model_name,
            "temperature": 0,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are a deterministic market analysis model. "
                        "Return only valid JSON that matches the provided schema."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ai_market_assessment",
                    "strict": True,
                    "schema": strict_schema,
                },
            },
        }
        timeout = httpx.Timeout(self.settings.ai_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.openai_base_url.rstrip("/"),
                timeout=timeout,
            ) as client:
                response = await client.post("/v1/responses", headers=headers, json=payload)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AIProviderTimeoutError("openai_request_timeout") from exc
        except httpx.HTTPStatusError as exc:
            detail = ""
            if exc.response is not None:
                try:
                    payload = exc.response.json()
                    detail = payload.get("error", {}).get("message") or exc.response.text
                except Exception:
                    detail = exc.response.text
            suffix = f":{detail}" if detail else ""
            raise AIProviderError(f"openai_http_error:{exc}{suffix}") from exc
        except httpx.HTTPError as exc:
            raise AIProviderError(f"openai_http_error:{exc}") from exc

        data = response.json()
        content = self._extract_text_payload(data)
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

    def _extract_text_payload(self, data: dict[str, object]) -> str | None:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        output = data.get("output")
        if not isinstance(output, list):
            return None
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "refusal":
                refusal = item.get("refusal") or item.get("content")
                raise AIProviderError(f"openai_refusal:{refusal}")
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    refusal = part.get("refusal") or part.get("text")
                    raise AIProviderError(f"openai_refusal:{refusal}")
                if part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        return text
        return None

    @classmethod
    def _strict_json_schema(cls, schema: dict[str, object]) -> dict[str, object]:
        normalized = deepcopy(schema)
        cls._normalize_schema_node(normalized)
        return normalized

    @classmethod
    def _normalize_schema_node(cls, node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
                node["additionalProperties"] = False
                for child in properties.values():
                    cls._normalize_schema_node(child)

            for key in ("items", "additionalProperties", "contains", "if", "then", "else", "not"):
                if key in node:
                    cls._normalize_schema_node(node[key])

            for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                value = node.get(key)
                if isinstance(value, list):
                    for item in value:
                        cls._normalize_schema_node(item)

            for key, value in node.items():
                if key not in {
                    "properties",
                    "items",
                    "additionalProperties",
                    "contains",
                    "if",
                    "then",
                    "else",
                    "not",
                    "allOf",
                    "anyOf",
                    "oneOf",
                    "prefixItems",
                }:
                    cls._normalize_schema_node(value)
        elif isinstance(node, list):
            for item in node:
                cls._normalize_schema_node(item)
