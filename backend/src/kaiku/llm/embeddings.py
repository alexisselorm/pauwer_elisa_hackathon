"""Embeddings: Featherless `Qwen/Qwen3-Embedding-8B` through its OpenAI-compatible API.

Built on `langchain_openai.OpenAIEmbeddings` (retries, batching, `dimensions`). This module
adds what the provider doesn't guarantee:
- a process-wide concurrency cap (`EMBEDDING_MAX_CONCURRENCY`, the plan's limit);
- a startup probe that checks the returned length;
- Matryoshka (MRL) truncation + L2 re-normalisation when the provider ignores
  `dimensions` and returns the full 4096-dim vector (Qwen3-Embedding is MRL-trained);
- `EmbeddingUnavailableError`, so retrieval can fall back to graph-only scoping instead of
  blocking when Featherless is down or out of credit.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Protocol

import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel, SecretStr

from kaiku.config import Settings
from kaiku.logging import get_logger

PROBE_TEXT = "TRN-042 forwarding state after SFP replacement"
log = get_logger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """The embedding provider failed. Callers fall back to graph-only retrieval."""


class EmbeddingDimensionError(RuntimeError):
    """The provider returned fewer dimensions than the index needs: can't be fixed here."""


class EmbeddingProbe(BaseModel):
    target_dim: int
    returned_dim: int
    truncated: bool


class Embedder(Protocol):
    """What the rest of the code depends on. No vendor types leak out of `llm/`."""

    @property
    def dim(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def mrl_truncate(vector: Sequence[float], dim: int) -> list[float]:
    """Keep the first `dim` components and L2-normalise them (valid for MRL-trained models)."""
    if len(vector) < dim:
        raise EmbeddingDimensionError(f"got {len(vector)} dimensions, need {dim}")
    head = list(vector[:dim])
    norm = math.sqrt(math.fsum(x * x for x in head))
    if norm == 0.0:
        raise EmbeddingDimensionError("zero vector after truncation")
    return [x / norm for x in head]


def build_featherless_client(
    settings: Settings, api_key: SecretStr, *, http_async_client: httpx.AsyncClient | None = None
) -> Embeddings:
    return OpenAIEmbeddings(
        http_async_client=http_async_client,
        model=settings.embedding_model,
        openai_api_base=settings.embedding_base_url,
        openai_api_key=api_key,
        dimensions=settings.embedding_dim,
        chunk_size=settings.embedding_batch_size,
        request_timeout=settings.embedding_timeout_s,
        max_retries=2,
        # tiktoken length checks are for OpenAI models; send the raw strings.
        check_embedding_ctx_length=False,
        tiktoken_enabled=False,
    )


class FeatherlessEmbedder:
    """`Embedder` over any LangChain `Embeddings` client (Featherless in production)."""

    def __init__(
        self, client: Embeddings, *, dim: int, batch_size: int, max_concurrency: int
    ) -> None:
        self._client = client
        self._dim = dim
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._warned_truncation = False

    @classmethod
    def from_settings(cls, settings: Settings) -> FeatherlessEmbedder | None:
        """None when FEATHERLESS_API_KEY isn't set (retrieval is then graph-only)."""
        if settings.featherless_api_key is None:
            return None
        return cls(
            build_featherless_client(settings, settings.featherless_api_key),
            dim=settings.embedding_dim,
            batch_size=settings.embedding_batch_size,
            max_concurrency=settings.embedding_max_concurrency,
        )

    @property
    def dim(self) -> int:
        return self._dim

    def _fit(self, vector: Sequence[float]) -> list[float]:
        if len(vector) == self._dim:
            return list(vector)
        if len(vector) > self._dim and not self._warned_truncation:
            self._warned_truncation = True
            log.warning(
                "embedding_dimensions_ignored_truncating",
                returned=len(vector),
                target=self._dim,
            )
        return mrl_truncate(vector, self._dim)

    async def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        async with self._semaphore:
            try:
                vectors = await self._client.aembed_documents(list(batch))
            except Exception as exc:
                raise EmbeddingUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        if len(vectors) != len(batch):
            raise EmbeddingUnavailableError(f"asked for {len(batch)} vectors, got {len(vectors)}")
        return [self._fit(v) for v in vectors]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        batches = [texts[i : i + self._batch_size] for i in range(0, len(texts), self._batch_size)]
        results = await asyncio.gather(*(self._embed_batch(b) for b in batches))
        return [v for batch in results for v in batch]

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]

    async def probe(self) -> EmbeddingProbe:
        """Embed one string and report what the provider really returns."""
        async with self._semaphore:
            try:
                raw = await self._client.aembed_query(PROBE_TEXT)
            except Exception as exc:
                raise EmbeddingUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        fitted = self._fit(raw)
        result = EmbeddingProbe(
            target_dim=self._dim, returned_dim=len(raw), truncated=len(raw) != len(fitted)
        )
        log.info("embedding_probe", **result.model_dump())
        return result


async def embed_query_or_none(embedder: Embedder | None, text: str) -> list[float] | None:
    """For retrieval: a vector, or None (graph-only fallback). Never raises, never blocks
    longer than the client's timeout."""
    if embedder is None:
        return None
    try:
        return await embedder.embed_query(text)
    except (EmbeddingUnavailableError, EmbeddingDimensionError) as exc:
        log.warning("embedding_unavailable_graph_only_fallback", error=str(exc))
        return None
