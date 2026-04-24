from __future__ import annotations

import json
from time import perf_counter

import httpx

from aats.bootstrap.settings import AATSSettings
from aats.services.ai_service.provider import AIProviderError, AIProviderResponse, AIProviderTimeoutError


class DeepSeekProvider:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    async def generate_assessment(
        self,
        *,
        prompt: str,
        response_schema: dict[str, object],
    ) -> AIProviderResponse:
        if not self.settings.deepseek_api_key:
            raise AIProviderError("deepseek_api_key_not_configured")

        request_started = perf_counter()
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        schema_hint = json.dumps(response_schema, ensure_ascii=False, sort_keys=True)
        system_prompt = (
            "You are a deterministic market analysis model. "
            "Return only a single JSON object that strictly conforms to the "
            "following JSON schema. Do not include any prose, markdown fences, "
            "comments, or additional keys beyond those declared in the schema.\n"
            f"JSON schema:\n{schema_hint}"
        )
        payload = {
            "model": self.settings.ai_model_name,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        timeout = httpx.Timeout(self.settings.ai_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.deepseek_base_url.rstrip("/"),
                timeout=timeout,
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AIProviderTimeoutError("deepseek_request_timeout") from exc
        except httpx.HTTPStatusError as exc:
            detail = ""
            if exc.response is not None:
                try:
                    error_body = exc.response.json()
                    detail = error_body.get("error", {}).get("message") or exc.response.text
                except Exception:
                    detail = exc.response.text
            suffix = f":{detail}" if detail else ""
            raise AIProviderError(f"deepseek_http_error:{exc}{suffix}") from exc
        except httpx.HTTPError as exc:
            raise AIProviderError(f"deepseek_http_error:{exc}") from exc

        data = response.json()
        content = self._extract_message_content(data)
        if not isinstance(content, str) or not content.strip():
            raise AIProviderError("deepseek_missing_content")

        try:
            parsed_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AIProviderError("deepseek_invalid_json_content") from exc

        if not isinstance(parsed_payload, dict):
            raise AIProviderError("deepseek_non_object_json")

        request_id = response.headers.get("x-request-id")
        latency_ms = (perf_counter() - request_started) * 1000.0
        return AIProviderResponse(
            provider_name="deepseek",
            request_id=request_id,
            latency_ms=latency_ms,
            payload=parsed_payload,
        )

    @staticmethod
    def _extract_message_content(data: dict[str, object]) -> str | None:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        finish_reason = first.get("finish_reason")
        if finish_reason == "content_filter":
            raise AIProviderError("deepseek_refusal:content_filter")
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise AIProviderError(f"deepseek_refusal:{refusal}")
        content = message.get("content")
        if isinstance(content, str):
            return content
        return None
