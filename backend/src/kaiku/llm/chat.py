"""Chat models from `provider:model` strings (CHAT_MODEL, FAST_MODEL) via `init_chat_model`.

Sampling parameters (temperature, top_p) are never set: Claude 5 models reject them with a
400, and the defaults are right for every provider we use.
`featherless:<model>` is served through Featherless's OpenAI-compatible endpoint.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from kaiku.config import Settings

# Arguments that must never reach a model constructor.
FORBIDDEN_KWARGS = frozenset({"temperature", "top_p", "top_k"})


class ModelRole(StrEnum):
    CHAT = "chat"  # extraction, the ops agent
    FAST = "fast"  # triage, classification


class ChatModelUnavailableError(RuntimeError):
    """The provider's API key is missing."""


def _model_string(settings: Settings, role: ModelRole) -> str:
    return settings.chat_model if role is ModelRole.CHAT else settings.fast_model


def chat_model_kwargs(settings: Settings, role: ModelRole) -> tuple[str, dict[str, Any]]:
    """The model string and constructor kwargs for `init_chat_model` (no network)."""
    provider, _, model = _model_string(settings, role).partition(":")
    key: SecretStr | None
    kwargs: dict[str, Any] = {"max_retries": 2, "timeout": 120}
    if provider == "featherless":
        key = settings.featherless_api_key
        kwargs["base_url"] = settings.embedding_base_url
        name = f"openai:{model}"
    elif provider == "anthropic":
        key = settings.anthropic_api_key
        name = f"{provider}:{model}"
    elif provider == "openai":
        key = settings.openai_api_key
        name = f"{provider}:{model}"
    else:
        raise ValueError(f"unsupported chat provider {provider!r}")
    if key is None:
        raise ChatModelUnavailableError(f"no API key for {provider} ({role} model)")
    kwargs["api_key"] = key.get_secret_value()
    assert not FORBIDDEN_KWARGS & kwargs.keys()
    return name, kwargs


def get_chat_model(settings: Settings, role: ModelRole = ModelRole.CHAT) -> BaseChatModel:
    name, kwargs = chat_model_kwargs(settings, role)
    model: BaseChatModel = init_chat_model(name, **kwargs)
    return model
