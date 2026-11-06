"""Per-process state opened by the lifespan, and the FastAPI dependency that serves it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from silentctx.config import Settings
from silentctx.graph.driver import GraphState


@dataclass(frozen=True)
class AppState:
    settings: Settings
    graph: GraphState
    checkpointer: AsyncSqliteSaver


def get_app_state(request: Request) -> AppState:
    state: AppState = request.app.state.ctx
    return state


AppStateDep = Annotated[AppState, Depends(get_app_state)]
