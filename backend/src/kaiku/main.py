"""FastAPI app factory. Run with `uvicorn kaiku.main:create_app --factory`.

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

from kaiku import __version__, health
from kaiku.config import Settings, get_settings
from kaiku.context.service import ContextService
from kaiku.errors import install_error_handlers
from kaiku.graph.driver import GraphState, create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.llm.embeddings import FeatherlessEmbedder
from kaiku.logging import REQUEST_ID_HEADER, configure_logging, get_logger
from kaiku.mcp.server import ServiceHolder, create_mcp_server
from kaiku.state import AppState, EmbeddingState
from kaiku.telemetry.store import TelemetryStore
from kaiku.tracing import setup_tracing
from kaiku.ui.names import SlackNames
from kaiku.ui.queries import UiQueries
from kaiku.ui.router import Console
from kaiku.ui.router import router as console_router

log = get_logger(__name__)
EMBED_INTERVAL_S = 60.0


def _load_aliases(settings: Settings) -> dict[str, str]:
    """Surface forms for entity resolution (mock_data/knowledge_graph/entity_aliases.csv)."""
    from kaiku.seed.sources import MockData

    try:
        return MockData(settings.data_dir).model_aliases()
    except (FileNotFoundError, KeyError) as exc:
        log.warning("entity_aliases_unavailable", error=str(exc))
        return {}


async def _embed_forever(
    graph: GraphState, embeddings: EmbeddingState, service: ContextService
) -> None:
    """Give every assertion a vector (seeded gold, proposals, captures), once Neo4j is up
    and the embedding probe passed. Runs every minute; failures only delay search."""
    await graph.ready.wait()
    while True:
        if embeddings.done and embeddings.error is None and embeddings.embedder is not None:
            try:
                await service.embed_missing()
            except Exception as exc:  # noqa: BLE001  # retried next round
                log.warning("embedding_backfill_failed", error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(EMBED_INTERVAL_S)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    setup_tracing(settings, "api")
    holder = ServiceHolder()
    mcp_server = create_mcp_server(settings.mcp_tokens, holder)
    mcp_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        driver = create_driver(settings)
        graph = GraphState(driver=driver, settings=settings)
        bootstrap = asyncio.create_task(graph.bootstrap(), name="neo4j-bootstrap")
        embeddings = EmbeddingState(embedder=FeatherlessEmbedder.from_settings(settings))
        probe = asyncio.create_task(embeddings.run_probe(), name="embedding-probe")
        telemetry = await asyncio.to_thread(TelemetryStore, settings.data_dir, settings.sim_url)
        holder.service = ContextService(
            ContextRepository(driver, settings.neo4j_database),
            telemetry,
            embedder=embeddings.embedder,
            aliases=_load_aliases(settings),
        )
        token = settings.slack_bot_token
        app.state.console = Console(
            settings=settings,
            service=holder.service,
            queries=UiQueries(driver, settings.neo4j_database),
            names=SlackNames(token.get_secret_value() if token else None),
        )
        embed_job = asyncio.create_task(
            _embed_forever(graph, embeddings, holder.service), name="embed-assertions"
        )
        # Outcomes the network couldn't judge when they were reported (plan 001 §6.2).
        verify_job = asyncio.create_task(
            holder.service.verifier.run_forever(graph.ready), name="verify-outcomes"
        )
        try:
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver:
                await saver.setup()
                app.state.ctx = AppState(
                    settings=settings, graph=graph, checkpointer=saver, embeddings=embeddings
                )
                async with mcp_server.session_manager.run():
                    log.info(
                        "startup_complete",
                        version=__version__,
                        demo_profile=settings.demo_profile,
                        llm_keys_missing=settings.missing_llm_keys(),
                    )
                    yield
        finally:
            holder.service = None
            for task in (verify_job, embed_job, probe, bootstrap):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await driver.close()
            log.info("shutdown_complete")

    app = FastAPI(
        title="kaiku",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )
    install_error_handlers(app)
    # Pure ASGI middleware: safe for MCP's streaming responses.
    app.add_middleware(CorrelationIdMiddleware, header_name=REQUEST_ID_HEADER)
    app.include_router(health.router)
    app.include_router(console_router)

    # MCP at /mcp. Added as a route (not a root mount) so that every other unknown path
    # still gets our problem+json 404. The sub-app keeps its own bearer-auth middleware.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app))

    return app
