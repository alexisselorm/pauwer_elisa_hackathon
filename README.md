# pauwer_elisa_hackathon — silentctx

Captures the operational knowledge experts share in Slack and email, binds it to the network
entities it is about with a validity period, and serves it to any AI agent over MCP.
Built for Elisa's challenge in `Elisa_SinceAI2026/` (the only source of truth).

| Where | What |
|---|---|
| [`docs/plans/`](docs/plans/) | 001 backend and time model, 002 Slack and MCP, 003 acceptance tests and eval |
| [`backend/`](backend/) | FastAPI + MCP server, Slack bot, network simulator ([README](backend/README.md)) |
| [`mock_data/`](mock_data/) | Synthetic network, chat, email and gold answers ([README](mock_data/README.md)) |
| [`eval/heldout/`](eval/heldout/) | Hand-written held-out threads for the eval |

## Run the demo

Needs Docker with Compose v2.24+.

1. `cp backend/.env.example backend/.env`, then set:
   - `MCP_TOKENS`: one bearer token per agent, e.g. `{"ops-agent": "<token>", "inspector": "<token>"}`
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`);
   - `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`) and `SLACK_CAPTURE_CHANNELS`
     from the Slack app (SL-0);
   - `ANTHROPIC_API_KEY` and `FEATHERLESS_API_KEY` (without them `/readyz` reports `degraded`).
2. `docker compose up --build`
3. Load the network into the graph (idempotent, safe to repeat):
   `docker compose exec api silentctx seed --network --profile demo`

| Service | URL | Healthy when |
|---|---|---|
| `neo4j` | http://localhost:7474 (bolt 7687) | `cypher-shell 'RETURN 1'` works |
| `api` | http://localhost:8000 — MCP at `/mcp` | `/readyz` is 200: Neo4j answers and the schema is applied |
| `slackbot` | Socket Mode, no public URL | connected to Slack (without tokens it idles and says so in its log) |
| `sim` | http://localhost:8100 | `/healthz` is 200 |

Everything binds to 127.0.0.1. Neo4j's password defaults to `silentctx-local-dev`; set
`NEO4J_PASSWORD` in your shell to change it (compose passes it to both Neo4j and the API).

**Connect MCP Inspector:** `npx @modelcontextprotocol/inspector`, transport *Streamable HTTP*,
URL `http://localhost:8000/mcp`, header `Authorization: Bearer <your inspector token>`.
Call `ping` to see which agent you are.

**Reset:** `docker compose down -v` deletes the graph and the checkpoints.

### Time conventions

All times are stored in UTC. A date stated in a message ("from Monday", "until 20 November")
means 00:00 Europe/Helsinki on that day; as an end it is exclusive. Elisa's example has local
times and no date, so its backfill uses 2025-10-14 in Europe/Helsinki (EEST, UTC+3) with
synthetic seconds to keep messages in the same minute ordered (plan 001 R8).
