"""Per-process state opened by the lifespan, and the FastAPI dependency that serves it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from kaiku.config import Settings
from kaiku.graph.driver import GraphState
from kaiku.llm.embeddings import (
    EmbeddingProbe,
    EmbeddingUnavailableError,
    FeatherlessEmbedder,
)
from kaiku.logging import get_logger

log = get_logger(__name__)


@dataclass
class EmbeddingState:
    """The embedder (None without FEATHERLESS_API_KEY) and its startup probe result."""

    embedder: FeatherlessEmbedder | None
    probe: EmbeddingProbe | None = None
    error: str | None = None
    done: bool = field(default=False)

    async def run_probe(self) -> None:
        if self.embedder is None:
            self.done = True
            return
        try:
            self.probe = await self.embedder.probe()
        except EmbeddingUnavailableError as exc:
            # Not fatal: retrieval falls back to graph-only scoping.
            self.error = str(exc)
            log.warning("embedding_probe_failed_graph_only", error=self.error)
        except Exception as exc:  # noqa: BLE001  # e.g. too few dimensions: report, don't crash
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("embedding_probe_failed", error=self.error)
        finally:
            self.done = True


@dataclass(frozen=True)
class AppState:
    settings: Settings
    graph: GraphState
    checkpointer: AsyncSqliteSaver
    embeddings: EmbeddingState


def get_app_state(request: Request) -> AppState:
    state: AppState = request.app.state.ctx
    return state


AppStateDep = Annotated[AppState, Depends(get_app_state)]
