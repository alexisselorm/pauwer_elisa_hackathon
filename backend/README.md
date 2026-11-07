# kaiku backend

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
docker run -d --name neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/kaiku-local-dev neo4j:2026.10.1-community
uv run kaiku-api         # http://localhost:8000  (API_RELOAD=true for --reload)
```

| Entry point | What |
|---|---|
| `kaiku-api` | FastAPI: health probes and the MCP server at `/mcp` |
| `kaiku-slackbot` | Bolt app in Socket Mode |
| `kaiku-sim` | Network simulator for the live demo |

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
        "kaiku": {
            "transport": "streamable_http",
            "url": "http://localhost:8000/mcp",
            "headers": {"Authorization": "Bearer <token>"},
        }
    }
)
tools = await client.get_tools()
```

## MCP tools (plan 002 §4)

`http://localhost:8000/mcp` (Streamable HTTP, `Authorization: Bearer <MCP_TOKENS[agent]>`),
or `uv run kaiku mcp --stdio --agent-id <name>` for local clients (Claude Desktop).
Every tool returns typed `structuredContent`; times are ISO-8601 UTC; `as_of` and `known_at`
default to now.

| Read | |
|---|---|
| `resolve_entities(text)` | "the Pori node" → TRN-017; aliases are fuzzy-matched |
| `get_entity(id, as_of)` | model, generation, firmware at t, segments, links |
| `get_context(id, as_of, known_at, topic, symptoms, statuses)` | knowledge in scope at t: the node, its model and generation, its segments, plus observations on same-model devices; ranked, with citations |
| `search_context(query, as_of, entity_id)` | semantic (Featherless embeddings), keyword fallback; entities named in the query are resolved and scoped as in `get_context` |
| `check_conditions(assertion_id, entity_id, port, at)` | each symptom met / not_met / unknown from telemetry (last complete 15-min interval) |
| `get_site_access(entity_or_site, as_of, known_at)` | access knowledge + register at the visit time; `known_at` shows what was believed then |
| `get_events(id, start, end, types)` | alarms, device events, outcomes |
| `get_history(assertion_id)` | supersession chain, contradictions, evidence, flags |

| Write (governed: no confirm / retract / validity edits) | |
|---|---|
| `report_outcome` | Outcome event; SUPPORTS if resolved, REFUTES only if the conditions were met |
| `open_knowledge_gap` | routed to the most frequent author on those entities |
| `propose_assertion` | `unverified`, must cite ≥ 1 existing event |
| `flag_assertion` | sets `review_due` |

Writes are idempotent per (agent, tool, `idempotency_key`) and each is recorded as an
`AgentAction` event attributed to the token's agent. Prompt `ops_triage`; resource
`context://entity/{id}`.

## Console API and traces (plan 005)

`/api/ui/*` is read-only and serves the web console (`frontend/`): `graph`, `assertions/{id}`,
`entities/{id}`, `captures`, `captures/{thread_id}`, `agent-runs`, `eval`, `meta`, and
`stream` (server-sent events). Each capture's `CaptureResult` (step timings, gates, trace id)
is kept on its `Thread`. Every MCP tool call is kept as a `(:ToolCall)` node. With
`PHOENIX_COLLECTOR_ENDPOINT` set, the api and the slackbot send OpenTelemetry traces
(OpenInference LangChain instrumentation) to Arize Phoenix.

## Ops agent

`kaiku.agents.ops_agent.OpsAgent`: LangChain `create_agent` on `CHAT_MODEL`
(Featherless Kimi-K2), whose only tools are the MCP read tools + `open_knowledge_gap`,
connected with its own token (`OPS_AGENT_ID`). Answers are a structured `OpsAnswer`; code
then drops any step without a citation the tools actually returned, and any step whose
conditions are `not_met`. In Slack: @mention the bot or DM it.

## Capture (Slack -> context card)

The slackbot captures every channel it is in (`SLACK_CAPTURE_CHANNELS` narrows that on
purpose). Messages, edits and deletes become `Message` events at their Slack `ts`
(`ingest/slack_events.py`). On startup a catch-up sweep reads what was posted while the bot
was down, from each channel's watermark. Every `CAPTURE_SWEEP_SECONDS` the capture loop
(`ingest/capture.py`) triages threads that have news and are resolved ("Traffic is back",
✅/🎉, the "Capture this as knowledge" shortcut) or idle for `CAPTURE_IDLE_MINUTES`. A
resolution signal triggers a sweep at once. The decision and its reason are remembered on
the `Thread`, so a thread with no new message costs no LLM call.

Kept threads go through `extraction/graph.py`: triage (FAST_MODEL) -> extract (CHAT_MODEL,
a verbatim quote per field) -> link (V1/V2 gates, times by code, entities from the
inventory, V3) -> validate (V4 telemetry) -> reconcile (V6: corroborates, supersedes,
corrects or contradicts, by source authority) -> persist (one transaction with the V5
checks). The thread then gets **one** context card, edited in place. Its buttons are
optional: ✅ confirms, ✏️ saves a correction (R6 ii), 🚫 retracts. Ignore decisions and
captures from outside Slack go to #context-log (`SLACK_CONTEXT_LOG_CHANNEL`), never to a
work channel.

## Seeding the graph

```bash
uv run kaiku seed --network                         # network layers (demo profile)
uv run kaiku seed --network --gold --profile full   # + gold answers, for the eval only
uv run kaiku seed --wipe-gold                       # drop gold before scoring extraction
```

Loads `DATA_DIR` (mock_data): devices, segments, sites, hardware models and generations,
as-running snapshots and links (all with validity), plus device events and alarms as
events (R1). Gold assertions and the access register are eval-only and refused with the
demo profile. Every run converges on the source: rows are MERGEd, rows that left the source
are pruned, and a re-run changes nothing. The temporal checks (`graph/checks.py`) run at the
end; the command exits 1 on any violation. The JSON report goes to stdout, logs to stderr.

The demo profile (`seed/profiles.py`) also defines what the historical backfill must skip:
every corpus source about the forwarding stall after an SFP swap (plan 001 §3.5).

```bash
uv run kaiku backfill                                # Teams, email, documents (demo profile)
uv run kaiku backfill --profile full --elisa-dir ../Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026
```

The backfill stores historical messages as events with their original times (Elisa's
example is dated 2025-10-14 Europe/Helsinki, R8). The slackbot's capture loop then triages
them. A re-run is a no-op.

Documents come in with both profiles, one event per document: the expert PDF notes
(`documents/expert_notes/`, at their CreationDate), the Confluence pages
(`documents/confluence/`, at their last edit) and the ITSM KB articles
(`itsm/knowledge_articles.jsonl`, at their publication date). Wiki and KB pages are tagged
`authority: low`: when one contradicts expert chat or email, the page is the side that is
disputed, and it never supersedes the expert, however recent its date.

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run pytest -m "not integration"   # fast, no Docker
uv run pytest                        # + Neo4j via testcontainers (Docker, ~1 min)
uv run pytest -m live -s             # real models from .env: ST-01 latency, ST-03 (~4 min)
```

## Layout

```
src/kaiku/
  main.py config.py logging.py errors.py health.py state.py cli.py
  domain/       time rules R1-R10 (time.py), shared types (models.py)
  graph/        driver + readiness, schema.py, checks.py, cypher/vector.py, migrations/V*.cypher
  seed/         mock_data loader, demo/full profiles (kaiku seed)
  llm/          chat-model factory, Featherless embedder
  mcp/          MCP server (auth, tools)
  slack/        Bolt app (bot.py), fault thread and buttons (actions.py), capture + card (capture.py)
  ingest/       Message events, threads, trigger, Slack + backfill, capture loop
  extraction/   triage -> extract -> link -> validate -> reconcile -> persist (graph.py)
  sim/          network simulator (network.py state, app.py endpoints)
  validation/ agents/ telemetry/ eval/
```

## Schema migrations

`graph/migrations/V<version>__<name>.cypher`, applied at startup by
[neo4j-python-migrations](https://github.com/booqoffsky/neo4j-python-migrations) (the
Neo4j-Migrations naming convention). Applied versions are `(:__Neo4jMigration)` nodes with a
checksum: re-running is a no-op and editing an applied file is refused, so add a new version
instead. Each file runs in one transaction, and the tool splits statements on every `;`,
including inside `//` comments (a test guards this).
