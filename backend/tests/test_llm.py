"""llm/: embeddings (Featherless) and the chat-model factory. No network."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable

import httpx
import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings

from silentctx.config import Settings
from silentctx.llm.chat import (
    FORBIDDEN_KWARGS,
    ChatModelUnavailableError,
    ModelRole,
    chat_model_kwargs,
    get_chat_model,
)
from silentctx.llm.embeddings import (
    EmbeddingDimensionError,
    EmbeddingUnavailableError,
    FeatherlessEmbedder,
    build_featherless_client,
    embed_query_or_none,
    mrl_truncate,
)


def _embedder(client: Embeddings, *, batch: int = 32, conc: int = 2) -> FeatherlessEmbedder:
    return FeatherlessEmbedder(client, dim=1024, batch_size=batch, max_concurrency=conc)


def _norm(v: list[float]) -> float:
    return math.sqrt(math.fsum(x * x for x in v))


# --- MRL truncation ------------------------------------------------------------------------


def test_mrl_truncate_keeps_the_head_and_renormalises() -> None:
    out = mrl_truncate([3.0, 4.0, 100.0], 2)
    assert out == pytest.approx([0.6, 0.8])


def test_mrl_truncate_refuses_too_few_dimensions() -> None:
    with pytest.raises(EmbeddingDimensionError):
        mrl_truncate([1.0] * 512, 1024)
    with pytest.raises(EmbeddingDimensionError, match="zero"):
        mrl_truncate([0.0, 0.0, 1.0], 2)


# --- Embedder ----------------------------------------------------------------------------------


async def test_correct_dimensions_pass_through() -> None:
    e = _embedder(DeterministicFakeEmbedding(size=1024))
    vectors = await e.embed_documents(["a", "b"])
    assert [len(v) for v in vectors] == [1024, 1024]
    probe = await e.probe()
    assert (probe.returned_dim, probe.truncated) == (1024, False)


async def test_ignored_dimensions_are_truncated_and_normalised() -> None:
    # Featherless returning the full 4096-dim vector despite dimensions=1024.
    e = _embedder(DeterministicFakeEmbedding(size=4096))
    probe = await e.probe()
    assert (probe.returned_dim, probe.truncated) == (4096, True)
    v = await e.embed_query("TRN-042")
    assert len(v) == 1024
    assert _norm(v) == pytest.approx(1.0)


async def test_too_few_dimensions_is_an_error() -> None:
    e = _embedder(DeterministicFakeEmbedding(size=512))
    with pytest.raises(EmbeddingDimensionError):
        await e.embed_query("x")


class _Recording(Embeddings):
    """Counts concurrent calls and fails on demand."""

    def __init__(self, fail: bool = False) -> None:
        self.active = 0
        self.peak = 0
        self.calls: list[list[str]] = []
        self.fail = fail

    def embed_documents(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.fail:
                raise httpx.ConnectError("featherless down")
            self.calls.append(texts)
            return [[float(len(t))] + [0.0] * 1023 for t in texts]
        finally:
            self.active -= 1

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.aembed_documents([text]))[0]


async def test_batches_respect_the_concurrency_cap_and_keep_order() -> None:
    client = _Recording()
    e = _embedder(client, batch=3, conc=2)
    texts = [str(i) * (i + 1) for i in range(10)]  # lengths 1..10
    vectors = await e.embed_documents(texts)
    assert [v[0] for v in vectors] == [float(i + 1) for i in range(10)]
    assert sorted(len(c) for c in client.calls) == [1, 3, 3, 3]
    assert client.peak == 2


async def test_empty_input_makes_no_call() -> None:
    client = _Recording()
    assert await _embedder(client).embed_documents([]) == []
    assert client.calls == []


async def test_provider_failure_is_reported_and_retrieval_falls_back() -> None:
    e = _embedder(_Recording(fail=True))
    with pytest.raises(EmbeddingUnavailableError, match="featherless down"):
        await e.embed_query("x")
    with pytest.raises(EmbeddingUnavailableError):
        await e.probe()
    assert await embed_query_or_none(e, "x") is None
    assert await embed_query_or_none(None, "x") is None


# --- The real client, over a mocked HTTP transport ------------------------------------------


def test_from_settings_is_none_without_a_key(make_settings: Callable[..., Settings]) -> None:
    assert FeatherlessEmbedder.from_settings(make_settings()) is None


async def test_featherless_request_on_the_wire(make_settings: Callable[..., Settings]) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.featherless.ai/v1/embeddings"
        assert request.headers["authorization"] == "Bearer fl-test-key"
        body = json.loads(request.content)
        seen.append(body)
        data = [
            {"object": "embedding", "index": i, "embedding": [0.5] * 4096}
            for i in range(len(body["input"]))
        ]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": body["model"],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    settings = make_settings(featherless_api_key="fl-test-key")
    assert settings.featherless_api_key is not None
    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = build_featherless_client(
        settings, settings.featherless_api_key, http_async_client=transport
    )
    e = FeatherlessEmbedder(client, dim=1024, batch_size=32, max_concurrency=2)

    vectors = await e.embed_documents(["Kemi gate", "TRN-042 forwarding stall"])
    assert seen[0]["model"] == "Qwen/Qwen3-Embedding-8B"
    assert seen[0]["dimensions"] == 1024
    assert seen[0]["input"] == ["Kemi gate", "TRN-042 forwarding stall"]  # strings, not tokens
    assert [len(v) for v in vectors] == [1024, 1024]  # 4096 returned -> truncated


# --- Chat models -------------------------------------------------------------------------------


def test_default_models_come_from_featherless(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(featherless_api_key="fl-test-key")
    for role in ModelRole:
        name, kwargs = chat_model_kwargs(s, role)
        assert name.startswith("openai:")  # Featherless's OpenAI-compatible API
        assert kwargs["base_url"] == "https://api.featherless.ai/v1"
        assert not FORBIDDEN_KWARGS & kwargs.keys()
    model = get_chat_model(s, ModelRole.CHAT)
    assert getattr(model, "model_name", None) == "moonshotai/Kimi-K2-Instruct-0905"
    assert getattr(model, "temperature", "unset") is None
    fast = get_chat_model(s, ModelRole.FAST)
    assert getattr(fast, "model_name", None) == "Qwen/Qwen3-4B-Instruct-2507"


def test_chat_models_never_set_sampling_parameters(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(
        chat_model="anthropic:claude-sonnet-5-5",
        fast_model="anthropic:claude-haiku-5-5",
        anthropic_api_key="sk-ant-test",
    )
    for role in ModelRole:
        name, kwargs = chat_model_kwargs(s, role)
        assert name.startswith("anthropic:")
        assert not FORBIDDEN_KWARGS & kwargs.keys()
    model = get_chat_model(s, ModelRole.CHAT)
    assert type(model).__name__ == "ChatAnthropic"
    assert getattr(model, "temperature", None) is None
    assert getattr(model, "top_p", None) is None
    assert getattr(model, "model", None) == "claude-sonnet-5-5"


def test_featherless_chat_goes_through_the_openai_compatible_api(
    make_settings: Callable[..., Settings],
) -> None:
    s = make_settings(fast_model="featherless:Qwen/Qwen3-32B", featherless_api_key="fl-test-key")
    name, kwargs = chat_model_kwargs(s, ModelRole.FAST)
    assert name == "openai:Qwen/Qwen3-32B"
    assert kwargs["base_url"] == "https://api.featherless.ai/v1"
    model = get_chat_model(s, ModelRole.FAST)
    assert type(model).__name__ == "ChatOpenAI"
    assert getattr(model, "temperature", "unset") is None


def test_missing_chat_key_is_a_clear_error(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ChatModelUnavailableError, match="featherless"):
        chat_model_kwargs(make_settings(), ModelRole.CHAT)
    with pytest.raises(ChatModelUnavailableError, match="anthropic"):
        chat_model_kwargs(make_settings(chat_model="anthropic:claude-sonnet-5-5"), ModelRole.CHAT)
