# silentctx backend

FastAPI app that hosts the context layer and serves the MCP server at `/mcp`.
Plans: [`docs/plans/001`](../docs/plans/001-backend-setup.md), [`002`](../docs/plans/002-slack-and-mcp.md), [`003`](../docs/plans/003-acceptance-and-eval.md).

## Setup

```bash
cd backend
uv sync                      # Python 3.12, deps pinned in uv.lock
cp .env.example .env         # MCP_TOKENS at least; see the comments in the file
uv run pre-commit install    # ruff format/check + mypy --strict on commit
```

The whole stack (Neo4j, api, slackbot, sim) runs with `docker compose up --build` from the
repo root: see the root README. To run the API alone against a local Neo4j:

```bash
docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/silentctx-local-dev neo4j:2026.10.1-community
uv run silentctx-api         # http://localhost:8000  (API_RELOAD=true for --reload)
```

| Entry point | What |
|---|---|
| `silentctx-api` | FastAPI: health probes and the MCP server at `/mcp` |
| `silentctx-slackbot` | Bolt app in Socket Mode |
| `silentctx-sim` | Network simulator for the live demo |

| Endpoint | What it says |
|---|---|
| `GET /healthz` | Liveness. Always 200 while the process is up. |
| `GET /readyz` | 200 when Neo4j answers and the schema is applied; `degraded` (still 200) when an LLM key is missing; 503 while Neo4j is down (retried in the background with backoff, never a crash-loop) or when `EMBEDDING_DIM` doesn't match the vector indexes (not retried). |
| `POST /mcp` | MCP over Streamable HTTP. `Authorization: Bearer <token>` from `MCP_TOKENS`; the token identifies the agent. |
| `GET /docs` | OpenAPI UI (not in `ENV=prod`). |

Errors are RFC 9457 `application/problem+json` and carry the `X-Request-ID`
(asgi-correlation-id) that appears in the JSON logs.

Connect an agent (the same client our ops agent uses):

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient(
    {
        "silentctx": {
            "transport": "streamable_http",
            "url": "http://localhost:8000/mcp",
            "headers": {"Authorization": "Bearer <token>"},
        }
    }
)
tools = await client.get_tools()
```

## Seeding the graph

```bash
uv run silentctx seed --network                         # network layers (demo profile)
uv run silentctx seed --network --gold --profile full   # + gold answers, for the eval only
uv run silentctx seed --wipe-gold                       # drop gold before scoring extraction
```

Loads `DATA_DIR` (mock_data): devices, segments, sites, hardware models and generations,
as-running snapshots and links (all with validity), plus device events and alarms as
events (R1). Gold assertions and the access register are eval-only and refused with the
demo profile. Every run converges on the source: rows are MERGEd, rows that left the source
are pruned, and a re-run changes nothing. The temporal checks (`graph/checks.py`) run at the
end; the command exits 1 on any violation. The JSON report goes to stdout, logs to stderr.

The demo profile (`seed/profiles.py`) also defines what the historical backfill must skip:
every corpus source about the forwarding stall after an SFP swap (plan 001 §3.5).

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run pytest -m "not integration"   # fast, no Docker
uv run pytest                        # + Neo4j via testcontainers (Docker, ~1 min)
```

## Layout

```
src/silentctx/
  main.py config.py logging.py errors.py health.py state.py cli.py
  domain/       time rules R1-R10 (time.py), shared types (models.py)
  graph/        driver + readiness, schema.py, checks.py, cypher/vector.py, migrations/V*.cypher
  seed/         mock_data loader, demo/full profiles (silentctx seed)
  llm/          chat-model factory, Featherless embedder
  mcp/          MCP server (auth, tools)
  slack/        Bolt app (bot.py)
  sim/          network simulator (app.py)
  ingest/ extraction/ validation/ agents/ telemetry/ eval/
```

## Schema migrations

`graph/migrations/V<version>__<name>.cypher`, applied at startup by
[neo4j-python-migrations](https://github.com/booqoffsky/neo4j-python-migrations) (the
Neo4j-Migrations naming convention). Applied versions are `(:__Neo4jMigration)` nodes with a
checksum: re-running is a no-op and editing an applied file is refused, so add a new version
instead. Each file runs in one transaction, and the tool splits statements on every `;`,
including inside `//` comments (a test guards this).
