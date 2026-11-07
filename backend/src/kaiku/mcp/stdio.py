"""MCP over stdio (`kaiku mcp --stdio`), for local clients such as Claude Desktop.

Same tools as the HTTP server; writes are attributed to `--agent-id` because stdio has no
bearer token. Reads and writes go through the same ContextService.
"""

from __future__ import annotations

import asyncio

from kaiku.config import Settings
from kaiku.context.service import ContextService
from kaiku.graph.driver import create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.llm.embeddings import FeatherlessEmbedder
from kaiku.mcp.server import ServiceHolder, create_mcp_server
from kaiku.seed.sources import MockData
from kaiku.telemetry.store import TelemetryStore


async def run_stdio(settings: Settings, agent_id: str) -> None:
    holder = ServiceHolder(stdio_agent_id=agent_id)
    server = create_mcp_server(settings.mcp_tokens, holder)
    driver = create_driver(settings)
    try:
        await driver.verify_connectivity()
        try:
            aliases = MockData(settings.data_dir).model_aliases()
        except FileNotFoundError:
            aliases = {}
        holder.service = ContextService(
            ContextRepository(driver, settings.neo4j_database),
            await asyncio.to_thread(TelemetryStore, settings.data_dir),
            embedder=FeatherlessEmbedder.from_settings(settings),
            aliases=aliases,
        )
        await server.run_stdio_async()
    finally:
        await driver.close()
