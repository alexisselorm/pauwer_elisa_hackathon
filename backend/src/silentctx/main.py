"""FastAPI app factory. Run with `uvicorn silentctx.main:create_app --factory`.

Startup order: logging, Neo4j driver (+ background connect/migrate), LangGraph
checkpointer, MCP session manager. Shutdown runs in reverse.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.routing import Route

from silentctx import __version__, health
from silentctx.config import Settings, get_settings
from silentctx.errors import install_error_handlers
from silentctx.graph.driver import GraphState, create_driver
from silentctx.logging import REQUEST_ID_HEADER, configure_logging, get_logger
from silentctx.mcp.server import create_mcp_server
from silentctx.state import AppState

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    mcp_server = create_mcp_server(settings.mcp_tokens)
    mcp_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        driver = create_driver(settings)
        graph = GraphState(driver=driver, settings=settings)
        bootstrap = asyncio.create_task(graph.bootstrap(), name="neo4j-bootstrap")
        try:
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver:
                await saver.setup()
                app.state.ctx = AppState(settings=settings, graph=graph, checkpointer=saver)
                async with mcp_server.session_manager.run():
                    log.info(
                        "startup_complete",
                        version=__version__,
                        demo_profile=settings.demo_profile,
                        llm_keys_missing=settings.missing_llm_keys(),
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
    # Pure ASGI middleware: safe for MCP's streaming responses.
    app.add_middleware(CorrelationIdMiddleware, header_name=REQUEST_ID_HEADER)
    app.include_router(health.router)

    # MCP at /mcp. Added as a route (not a root mount) so that every other unknown path
    # still gets our problem+json 404. The sub-app keeps its own bearer-auth middleware.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app))

    return app
