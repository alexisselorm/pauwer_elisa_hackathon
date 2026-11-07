# pauwer_elisa_hackathon — silentctx

Captures the operational knowledge experts share in Slack and email, binds it to the network
entities it is about with a validity period, and serves it to any AI agent over MCP.
Built for Elisa's challenge in `Elisa_SinceAI2026/` (the only source of truth).

| Where | What |
|---|---|
| [`docs/plans/`](docs/plans/) | 001 backend and time model, 002 Slack and MCP, 003 acceptance tests and eval, 004 retrieval at time t |
| [`backend/`](backend/) | FastAPI + MCP server, Slack bot, network simulator ([README](backend/README.md)) |
| [`mock_data/`](mock_data/) | Synthetic network, chat, email and gold answers ([README](mock_data/README.md)) |
| [`eval/heldout/`](eval/heldout/) | Hand-written held-out threads for the eval |

## Run the demo

Needs Docker with Compose v2.24+.

1. `cp backend/.env.example backend/.env`, then set:
   - `MCP_TOKENS`: one bearer token per agent, e.g. `{"ops-agent": "<token>", "inspector": "<token>"}`
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`);
   - `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`), `SLACK_CAPTURE_CHANNELS`,
     `SLACK_ALARM_CHANNEL` and `SLACK_GAP_CHANNEL`
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

**Inject the demo fault** (simulator, SIM-1; nodes TRN-017 / TRN-019 / TRN-010, ports 1/1/2 and 1/1/3):

```sh
curl -XPOST localhost:8100/sim/fault -H 'content-type: application/json' \
  -d '{"node":"TRN-017","port":"1/1/2","kind":"sfp_swap_stall"}'   # started 75 min ago by default
curl -XPOST localhost:8100/sim/action -H 'content-type: application/json' \
  -d '{"node":"TRN-017","port":"1/1/2","action":"bounce_interface"}'  # add "outcome":"fail" to refute
```

`check_conditions` pulls the simulator's 15-minute samples into the same DuckDB view as the
history; `GET /sim/live?node=TRN-017&port=1/1/2` is the fine series and `GET /sim/events` the
event feed. Restarting `sim` resets the network to healthy.

The bot watches the network all the time and posts each fault in `#noc-alarms` (or
`SLACK_ALARM_CHANNEL`, else the first capture channel) with the ops agent's advice in the
thread: conditions, next step, avoid and sources, plus [Run bounce (simulated)] [Done manually ✓] [Didn't work ✗]. A click reports the outcome
through MCP; Run bounce also checks the live counters first, so the thread ends with
"network-confirmed on TRN-017 1/1/2 · support 1". When the agent finds nothing documented,
its answer offers [Ask <expert>], which posts the question in `SLACK_GAP_CHANNEL`.

**Reset:** `docker compose down -v` deletes the graph and the checkpoints.

### Time conventions

All times are stored in UTC. A date stated in a message ("from Monday", "until 20 November")
means 00:00 Europe/Helsinki on that day; as an end it is exclusive. Elisa's example has local
times and no date, so its backfill uses 2025-10-14 in Europe/Helsinki (EEST, UTC+3) with
synthetic seconds to keep messages in the same minute ordered (plan 001 R8).
