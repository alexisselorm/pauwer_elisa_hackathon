# silentctx backend

FastAPI app that hosts the context layer and serves the MCP server at `/mcp`.
Plans: [`docs/plans/001`](../docs/plans/001-backend-setup.md), [`002`](../docs/plans/002-slack-and-mcp.md), [`003`](../docs/plans/003-acceptance-and-eval.md).

## Setup

```bash
cd backend
uv sync                      # Python 3.12, deps pinned in uv.lock
cp .env.example .env         # fill in NEO4J_PASSWORD and MCP_TOKENS at least
uv run pre-commit install    # from the repo: ruff format/check + mypy --strict on commit
```

## Run

```bash
docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/change-me-please neo4j:2026.10.1-community
uv run silentctx-api         # http://localhost:8000  (API_RELOAD=true for --reload)
```

| Endpoint | What it says |
|---|---|
| `GET /healthz` | Liveness. Always 200 while the process is up. |
| `GET /readyz` | 200 when Neo4j answers and migrations are applied; `degraded` (still 200) when an LLM key is missing; 503 while Neo4j is down. The app retries Neo4j in the background with backoff and never crash-loops. |
| `POST /mcp` | MCP over Streamable HTTP. `Authorization: Bearer <token>` from `MCP_TOKENS`; the token identifies the agent. |
| `GET /docs` | OpenAPI UI (not in `ENV=prod`). |

Errors are RFC 9457 `application/problem+json` and carry the `x-request-id` that appears in the JSON logs.

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

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run pytest -m "not integration"   # fast, no Docker
uv run pytest                        # + Neo4j via testcontainers (Docker, ~1 min)
```

## Layout

```
src/silentctx/
  main.py config.py logging.py errors.py cli.py
  graph/        driver + readiness, migration runner, cypher_migrations/NNNN_*.cypher
  mcp/          MCP server (auth, tools)
  domain/ ingest/ extraction/ validation/ slack/ agents/ sim/ telemetry/ eval/ llm/
```

Migrations are forward-only `.cypher` files, applied in order at startup and recorded as
`(:SchemaMigration {id})`. Each statement must be idempotent (`IF NOT EXISTS`).
