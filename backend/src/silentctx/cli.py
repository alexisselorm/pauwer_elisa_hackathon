"""Command-line entry points.

- `silentctx-api`: the API (FastAPI + MCP at /mcp) under uvicorn.
- `silentctx seed ...`: load mock_data into Neo4j (BE-1.4).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from silentctx.config import get_settings
from silentctx.logging import configure_logging
from silentctx.seed.profiles import Profile

app = typer.Typer(help="silentctx: the context layer for agentic network operations.")


def serve() -> None:
    uvicorn.run(
        "silentctx.main:create_app",
        factory=True,
        host=os.environ.get("API_HOST", "0.0.0.0"),  # noqa: S104  # served inside the container
        port=int(os.environ.get("API_PORT", "8000")),
        reload=os.environ.get("API_RELOAD", "false").lower() == "true",
        log_config=None,  # silentctx.logging owns the handlers
        proxy_headers=True,
    )


@app.callback()
def _main() -> None:
    """silentctx commands."""


@app.command()
def mcp(
    stdio: Annotated[bool, typer.Option("--stdio", help="Serve MCP over stdin/stdout.")] = False,
    agent_id: Annotated[
        str, typer.Option(help="Who the writes are attributed to (stdio has no token).")
    ] = "local-stdio",
) -> None:
    """Run the MCP server over stdio, for local clients (Claude Desktop, MCP Inspector).
    Over HTTP the API serves it at /mcp with bearer tokens."""
    if not stdio:
        raise typer.BadParameter("only --stdio is supported here; HTTP is served by the API")
    from silentctx.mcp.stdio import run_stdio

    settings = get_settings()
    # stdout carries the MCP protocol: logs must go to stderr.
    configure_logging(settings.log_level, json=settings.log_json, stream=sys.stderr)
    asyncio.run(run_stdio(settings, agent_id))


@app.command()
def seed(
    network: Annotated[bool, typer.Option("--network", help="Load the network layers.")] = False,
    gold: Annotated[
        bool,
        typer.Option("--gold", help="Load the gold answers (eval only; needs --profile full)."),
    ] = False,
    wipe_gold: Annotated[
        bool, typer.Option("--wipe-gold", help="Remove gold nodes before scoring extraction.")
    ] = False,
    profile: Annotated[
        Profile | None,
        typer.Option(help="demo (default when DEMO_PROFILE=true) or full (T1 eval)."),
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option(help="mock_data directory (default: DATA_DIR).")
    ] = None,
) -> None:
    """Seed Neo4j from mock_data. Idempotent: a re-run changes nothing."""
    from silentctx.seed.service import SeedRequest
    from silentctx.seed.service import seed as run_seed

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json, stream=sys.stderr)
    if data_dir is not None:
        settings = settings.model_copy(update={"data_dir": data_dir})
    chosen = profile or (Profile.DEMO if settings.demo_profile else Profile.FULL)
    request = SeedRequest(network=network, gold=gold, wipe_gold=wipe_gold, profile=chosen)
    try:
        request.validate_rules()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    result = asyncio.run(run_seed(settings, request))
    typer.echo(result.model_dump_json(indent=2, exclude_none=True))
    if result.violations:
        typer.echo(f"{result.violations} temporal violation(s)", err=True)
        raise typer.Exit(code=1)
