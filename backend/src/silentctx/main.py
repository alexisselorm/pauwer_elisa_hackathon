"""FastAPI app factory. Run with `uvicorn silentctx.main:create_app --factory`.

Startup order: logging, Neo4j driver (+ background connect/migrate), LangGraph
checkpointer, MCP session manager. Shutdown runs in reverse.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.routing import Route

from silentctx import __version__
from silentctx.config import Settings, get_settings
from silentctx.errors import install_error_handlers
from silentctx.graph.driver import GraphState, create_driver
from silentctx.logging import RequestIdMiddleware, configure_logging, get_logger
from silentctx.mcp.server import create_mcp_server

log = get_logger(__name__)
READY_PING_TIMEOUT_S = 2.0


@dataclass
class AppState:
    settings: Settings
    graph: GraphState
    checkpointer: AsyncSqliteSaver


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    mcp_server = create_mcp_server(settings.mcp_tokens)
    mcp_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        driver = create_driver(settings)
        graph = GraphState(driver=driver, database=settings.neo4j_database)
        bootstrap = asyncio.create_task(
            graph.bootstrap(settings.neo4j_retry_initial_s, settings.neo4j_retry_max_s),
            name="neo4j-bootstrap",
        )
        try:
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver:
                await saver.setup()
                app.state.ctx = AppState(settings=settings, graph=graph, checkpointer=saver)
                async with mcp_server.session_manager.run():
                    missing = settings.missing_llm_keys()
                    log.info(
                        "startup_complete",
                        version=__version__,
                        demo_profile=settings.demo_profile,
                        llm_keys_missing=missing,
                    )
                    yield
        finally:
            bootstrap.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bootstrap
            await driver.close()
            log.info("shutdown_complete")

    app = FastAPI(
        title="silentctx",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never touches dependencies."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: Neo4j answers and migrations are applied. A missing LLM key is
        reported as degraded (200), so the seeded demo still works offline."""
        ctx: AppState = request.app.state.ctx
        checks: dict[str, dict[str, Any]] = {}

        try:
            await ctx.graph.ping(READY_PING_TIMEOUT_S)
            checks["neo4j"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001  # any failure means "not ready"
            checks["neo4j"] = {"status": "down", "error": type(exc).__name__}

        if ctx.graph.migrated:
            checks["migrations"] = {"status": "ok"}
        else:
            checks["migrations"] = {"status": "down", "error": ctx.graph.last_error}

        missing = settings.missing_llm_keys()
        checks["llm"] = {"status": "degraded", "missing": missing} if missing else {"status": "ok"}

        if any(c["status"] == "down" for c in checks.values()):
            status, code = "down", 503
        elif any(c["status"] == "degraded" for c in checks.values()):
            status, code = "degraded", 200
        else:
            status, code = "ok", 200
        return JSONResponse({"status": status, "checks": checks}, status_code=code)

    # MCP at /mcp. Added as a route (not a root mount) so that every other unknown path
    # still gets our problem+json 404. The sub-app keeps its own bearer-auth middleware.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app))

    return app
