from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.services.ai_service.provider import AIProvider


def build_ai_provider(settings: AATSSettings) -> AIProvider | None:
    if not settings.ai_provider_configured:
        return None
    provider_name = settings.ai_provider
    if provider_name == "openai":
        from aats.services.ai_service.openai_provider import OpenAIProvider

        return OpenAIProvider(settings=settings)
    if provider_name == "deepseek":
        from aats.services.ai_service.deepseek_provider import DeepSeekProvider

        return DeepSeekProvider(settings=settings)
    return None
