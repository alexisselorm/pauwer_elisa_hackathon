"""Command-line entry points.

- `kaiku-api`: the API (FastAPI + MCP at /mcp) under uvicorn.
- `kaiku seed ...`: load mock_data into Neo4j (BE-1.4).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from kaiku.config import get_settings
from kaiku.logging import configure_logging
from kaiku.seed.profiles import Profile

app = typer.Typer(help="kaiku: the context layer for agentic network operations.")


def serve() -> None:
    uvicorn.run(
        "kaiku.main:create_app",
        factory=True,
        host=os.environ.get("API_HOST", "0.0.0.0"),  # noqa: S104  # served inside the container
        port=int(os.environ.get("API_PORT", "8000")),
        reload=os.environ.get("API_RELOAD", "false").lower() == "true",
        log_config=None,  # kaiku.logging owns the handlers
        proxy_headers=True,
    )


@app.callback()
def _main() -> None:
    """kaiku commands."""


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
    from kaiku.mcp.stdio import run_stdio

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
    from kaiku.seed.service import SeedRequest
    from kaiku.seed.service import seed as run_seed

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


@app.command()
def backfill(
    profile: Annotated[
        Profile | None,
        typer.Option(help="demo (default when DEMO_PROFILE=true) or full (T1 eval)."),
    ] = None,
    elisa_dir: Annotated[
        Path | None,
        typer.Option(help="Elisa's example folder (teams_chat.json, email.txt); full only."),
    ] = None,
) -> None:
    """Load historical Teams messages, the Outlook .msg export, the expert PDF notes,
    Confluence pages and ITSM KB articles (and Elisa's example) as events with their
    original times. The capture loop in the slackbot then triages them. Idempotent."""
    from kaiku.graph.driver import create_driver
    from kaiku.ingest.backfill import backfill as run_backfill
    from kaiku.ingest.store import CaptureStore
    from kaiku.seed.sources import MockData

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json, stream=sys.stderr)
    chosen = profile or (Profile.DEMO if settings.demo_profile else Profile.FULL)

    async def go() -> str:
        driver = create_driver(settings)
        try:
            store = CaptureStore(driver, settings.neo4j_database)
            report = await run_backfill(
                store, MockData(settings.data_dir), chosen, elisa_dir=elisa_dir
            )
            return report.model_dump_json(indent=2)
        finally:
            await driver.close()

    typer.echo(asyncio.run(go()))


@app.command()
def replay(
    channel: Annotated[str, typer.Option(help="Channel id to post into (e.g. #noc-transport).")],
    elisa_dir: Annotated[Path, typer.Option(help="Elisa's example folder.")] = Path(
        "../Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026"
    ),
    speed: Annotated[float, typer.Option(help="How much faster than the original.")] = 30.0,
) -> None:
    """Post Elisa's thread into Slack as personas (plan 002 §3.6), for the live demo."""
    from slack_sdk.web.async_client import AsyncWebClient

    from kaiku.slack.replay import replay_elisa

    settings = get_settings()
    if settings.slack_bot_token is None:
        raise typer.BadParameter("SLACK_BOT_TOKEN is not set")
    client = AsyncWebClient(token=settings.slack_bot_token.get_secret_value())
    for p in asyncio.run(replay_elisa(client, channel, elisa_dir, speed=speed)):
        typer.echo(f"{p.seq:>2} {p.ts} {p.author}: {p.text.splitlines()[0][:60]}")


eval_app = typer.Typer(help="The eval (plan 003 §2): T0 Elisa, T2 held-out, time checks.")
app.add_typer(eval_app, name="eval")


@eval_app.command("run")
def eval_run(
    set_: Annotated[
        str, typer.Option("--set", help="elisa (T0) or all (T0 + T2 held-out).")
    ] = "all",
    elisa_dir: Annotated[Path, typer.Option(help="Elisa's example folder.")] = Path(
        "../Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026"
    ),
    heldout_dir: Annotated[Path, typer.Option(help="The held-out set.")] = Path("../eval/heldout"),
    out_dir: Annotated[Path, typer.Option(help="Where results go.")] = Path("../eval/results"),
    neo4j_uri: Annotated[
        str | None,
        typer.Option(help="An isolated Neo4j to use. Default: a throwaway container."),
    ] = None,
    neo4j_password: Annotated[str | None, typer.Option(help="Its password.")] = None,
) -> None:
    """Capture Elisa's thread and the held-out threads with the real models on an isolated
    graph (gold never loaded), ask the questions through MCP, and score them exactly."""
    from kaiku.eval.runner import run_eval, to_markdown, write

    if set_ not in ("elisa", "all"):
        raise typer.BadParameter("--set must be elisa or all")
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json, stream=sys.stderr)
    sets = {"elisa"} | ({"heldout"} if set_ == "all" else set())
    report = asyncio.run(
        run_eval(
            settings,
            sets=sets,
            elisa_dir=elisa_dir,
            heldout_dir=heldout_dir,
            neo4j_uri=neo4j_uri,
            neo4j_password=neo4j_password,
        )
    )
    js, md = write(report, out_dir)
    typer.echo(to_markdown(report))
    typer.echo(f"results: {js} and {md}", err=True)


@eval_app.command("time")
def eval_time() -> None:
    """The temporal checks (R-rules, V5 port) on the configured graph. Read-only, no LLM."""
    from kaiku.graph.checks import run_checks
    from kaiku.graph.driver import create_driver

    settings = get_settings()

    async def go() -> int:
        driver = create_driver(settings)
        try:
            results = await run_checks(driver, settings.neo4j_database)
        finally:
            await driver.close()
        for r in results:
            mark = "ok  " if not r.violations else "FAIL"
            typer.echo(f"{mark} {r.name}: {r.violations} {', '.join(r.examples)}")
        return sum(r.violations for r in results)

    if asyncio.run(go()):
        raise typer.Exit(code=1)
