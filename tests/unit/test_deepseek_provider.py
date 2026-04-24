from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from aats.bootstrap.settings import AATSSettings
from aats.services.ai_service.deepseek_provider import DeepSeekProvider
from aats.services.ai_service.factory import build_ai_provider
from aats.services.ai_service.provider import AIProviderError, AIProviderTimeoutError


def _clean_env_settings(**overrides: Any) -> AATSSettings:
    env_backup = {key: value for key, value in os.environ.items()}
    for key in list(os.environ.keys()):
        if key.startswith("AATS_") or key in {"AI_SELECTOR"}:
            os.environ.pop(key, None)
    try:
        return AATSSettings.model_validate(overrides)
    finally:
        for key in list(os.environ.keys()):
            if key.startswith("AATS_") or key in {"AI_SELECTOR"}:
                os.environ.pop(key, None)
        os.environ.update(env_backup)


def _mock_async_client(response: httpx.Response) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=client)
    async_cm.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=async_cm)
    return factory, client


def _json_response(payload: dict[str, Any], status_code: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    return httpx.Response(
        status_code=status_code,
        headers=headers or {},
        content=json.dumps(payload).encode("utf-8"),
        request=request,
    )


class TestDeepSeekProviderHappyPath(unittest.IsolatedAsyncioTestCase):
    async def test_returns_parsed_assessment_from_chat_completions_response(self) -> None:
        settings = _clean_env_settings(
            ai_provider="deepseek",
            ai_model_name="deepseek-v4-pro",
            deepseek_api_key="sk-live-deepseek",
        )
        expected_payload = {
            "regime": "trend",
            "directional_edge": 0.42,
            "confidence": 0.71,
        }
        api_response = _json_response(
            {
                "id": "chatcmpl-fake",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(expected_payload),
                        },
                    }
                ],
            },
            headers={"x-request-id": "req-abc"},
        )
        factory, client = _mock_async_client(api_response)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            result = await provider.generate_assessment(
                prompt="what is the current regime?",
                response_schema={"type": "object", "title": "assessment"},
            )

        self.assertEqual(result.provider_name, "deepseek")
        self.assertEqual(result.request_id, "req-abc")
        self.assertEqual(result.payload, expected_payload)
        self.assertIsNotNone(result.latency_ms)

        factory.assert_called_once()
        client_kwargs = factory.call_args.kwargs
        self.assertEqual(client_kwargs["base_url"], "https://api.deepseek.com")

        client.post.assert_awaited_once()
        post_args, post_kwargs = client.post.call_args
        self.assertEqual(post_args[0], "/v1/chat/completions")
        self.assertEqual(post_kwargs["headers"]["Authorization"], "Bearer sk-live-deepseek")
        body = post_kwargs["json"]
        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("JSON schema", body["messages"][0]["content"])
        self.assertIn("assessment", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1], {"role": "user", "content": "what is the current regime?"})


class TestDeepSeekProviderErrorPaths(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_key_raises_before_http_call(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key=None)
        provider = DeepSeekProvider(settings=settings)

        with self.assertRaises(AIProviderError) as ctx:
            await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

        self.assertIn("deepseek_api_key_not_configured", str(ctx.exception))

    async def test_timeout_translates_to_provider_timeout(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key="sk-x")
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=client)
        async_cm.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=async_cm)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            with self.assertRaises(AIProviderTimeoutError):
                await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

    async def test_http_status_error_includes_api_error_message(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key="sk-x")
        err_response = _json_response(
            {"error": {"message": "invalid_api_key"}},
            status_code=401,
        )
        factory, _ = _mock_async_client(err_response)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            with self.assertRaises(AIProviderError) as ctx:
                await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

        self.assertIn("deepseek_http_error", str(ctx.exception))
        self.assertIn("invalid_api_key", str(ctx.exception))

    async def test_empty_content_raises_missing_content(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key="sk-x")
        api_response = _json_response(
            {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        )
        factory, _ = _mock_async_client(api_response)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            with self.assertRaises(AIProviderError) as ctx:
                await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

        self.assertIn("deepseek_missing_content", str(ctx.exception))

    async def test_non_json_content_raises_invalid_json(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key="sk-x")
        api_response = _json_response(
            {"choices": [{"message": {"role": "assistant", "content": "not json"}}]},
        )
        factory, _ = _mock_async_client(api_response)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            with self.assertRaises(AIProviderError) as ctx:
                await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

        self.assertIn("deepseek_invalid_json_content", str(ctx.exception))

    async def test_non_object_json_raises(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key="sk-x")
        api_response = _json_response(
            {"choices": [{"message": {"role": "assistant", "content": "[1, 2, 3]"}}]},
        )
        factory, _ = _mock_async_client(api_response)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            with self.assertRaises(AIProviderError) as ctx:
                await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

        self.assertIn("deepseek_non_object_json", str(ctx.exception))

    async def test_message_refusal_surfaces_as_provider_error(self) -> None:
        settings = _clean_env_settings(ai_provider="deepseek", deepseek_api_key="sk-x")
        api_response = _json_response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "refusal": "cannot comply",
                            "content": None,
                        },
                    }
                ]
            },
        )
        factory, _ = _mock_async_client(api_response)

        with patch("aats.services.ai_service.deepseek_provider.httpx.AsyncClient", factory):
            provider = DeepSeekProvider(settings=settings)
            with self.assertRaises(AIProviderError) as ctx:
                await provider.generate_assessment(prompt="p", response_schema={"type": "object"})

        self.assertIn("deepseek_refusal", str(ctx.exception))


class TestAIProviderSelectorAndFactory(unittest.TestCase):
    def test_factory_returns_deepseek_provider_when_configured(self) -> None:
        settings = _clean_env_settings(
            ai_provider="deepseek",
            deepseek_api_key="sk-real-deepseek",
        )

        provider = build_ai_provider(settings)

        self.assertIsInstance(provider, DeepSeekProvider)

    def test_factory_returns_openai_provider_when_configured(self) -> None:
        from aats.services.ai_service.openai_provider import OpenAIProvider

        settings = _clean_env_settings(
            ai_provider="openai",
            openai_api_key="sk-real-openai",
        )

        provider = build_ai_provider(settings)

        self.assertIsInstance(provider, OpenAIProvider)

    def test_factory_returns_none_when_disabled(self) -> None:
        settings = _clean_env_settings(ai_provider="disabled")

        self.assertIsNone(build_ai_provider(settings))

    def test_factory_returns_none_when_deepseek_key_missing(self) -> None:
        settings = _clean_env_settings(
            ai_provider="deepseek",
            deepseek_api_key=None,
        )

        self.assertIsNone(build_ai_provider(settings))

    def test_ai_provider_configured_detects_placeholder_deepseek_key(self) -> None:
        settings = _clean_env_settings(
            ai_provider="deepseek",
            deepseek_api_key="REPLACE_WITH_DEEPSEEK_API_KEY",
        )

        self.assertFalse(settings.ai_provider_configured)

    def test_ai_selector_env_overrides_yaml_ai_provider(self) -> None:
        with patch.dict(os.environ, {"AI_SELECTOR": "DEEPSEEK"}, clear=False):
            settings = AATSSettings.model_validate({"ai_provider": "openai", "deepseek_api_key": "sk-dp"})

        self.assertEqual(settings.ai_provider, "deepseek")
        self.assertTrue(settings.ai_provider_configured)

    def test_ai_selector_env_accepts_openai_normalization(self) -> None:
        with patch.dict(os.environ, {"AI_SELECTOR": "openai"}, clear=False):
            settings = AATSSettings.model_validate({"ai_provider": "disabled", "openai_api_key": "sk-oa"})

        self.assertEqual(settings.ai_provider, "openai")

    def test_ai_selector_env_rejects_unknown_value(self) -> None:
        with patch.dict(os.environ, {"AI_SELECTOR": "GROK"}, clear=False):
            with self.assertRaises(Exception) as ctx:
                AATSSettings.model_validate({"ai_provider": "openai"})

        self.assertIn("unknown_ai_selector_value", str(ctx.exception))

    def test_ai_selector_env_empty_string_is_noop(self) -> None:
        with patch.dict(os.environ, {"AI_SELECTOR": "  "}, clear=False):
            settings = AATSSettings.model_validate({"ai_provider": "openai", "openai_api_key": "sk-oa"})

        self.assertEqual(settings.ai_provider, "openai")


if __name__ == "__main__":
    unittest.main()
