# 001 — Backend setup: FastAPI + LangGraph agents + Neo4j context layer

Status: **Drafted 2026-11-06, `main`. Open: Neo4j `SEARCH` syntax spike; whether Featherless honours `dimensions` for the embedding model (§8). Resolved 2026-11-06: TRN-042 timeline alignment, alias canonical ids, embedding provider (Featherless AI).**

Companion plan: [`002-frontend-setup.md`](002-frontend-setup.md). The SSE event contract in WS5 is shared by both plans.

## 1. Goal

Build a backend that:

1. **Captures** expert knowledge from the channels people already use (Teams and email exports, plus PDFs and KB pages), so specialists do no documentation work.
2. **Stores** each piece of knowledge as a bitemporal `ContextAssertion` in Neo4j. Each assertion is bound to the right network entity and records where it came from.
3. **Serves** an operations agent that retrieves the assertions valid at a given time and proposes the right next step. Every answer cites its sources. When nothing in the store covers the question, the agent says "not documented — ask <person>".

**Who it's for:**
- The Elisa judges, at the Since AI demo and review on **2026-11-08 10:00–13:00**.
- In the story the demo tells: field engineers and NOC specialists (the people producing knowledge) and the autonomous-ops agent (which consumes it).

**Constraints:**
- The submission deadline is **2026-11-08 10:00**.
- The stack is fixed: FastAPI, LangGraph, Neo4j, React.
- The LLM layer is **provider-agnostic**.
- The demo runs **locally with docker compose**.
- Data is synthetic only (`mock_data/README.md`).

**Success test:**
- **Demo flow:** a stranger runs `docker compose up`, opens the UI, and replays the TRN-042 Teams thread. They see a `ContextAssertion` appear, bound to `TNC-1X`/`TRN-042` with `valid_from` set. Then they open a new fault on another `TNC-1X` node, and the agent recommends "check counters → bounce interface → don't reboot". It cites the Teams message and the email, without anyone re-typing the knowledge.
- **Eval:** the CLI prints a score table over `mock_data/answer_key/` (21 questions, 11 cases). Every `not_documented` item is answered "not documented".

## 2. Decisions

| Decision | Reason | Settled by / when |
|---|---|---|
| FastAPI + LangGraph + Neo4j (+ React) | Team choice. Neo4j is also named in the Elisa brief. | User, 2026-11-06 |
| LLM access is provider-agnostic. No vendor SDK is imported outside `backend/src/silentctx/llm/`. | User requirement. | User, 2026-11-06 |
| The demo target is local `docker compose`, with a recorded backup video. | User. | User, 2026-11-06 |
| LangGraph is self-hosted inside FastAPI. No `langgraph-api` / LangSmith Deployment. | `langgraph-api` is Elastic-2.0, and production self-hosting is reported to need a licence key (pypi.org/pypi/langgraph-api/json, 2026-11-06). Plain `langgraph` is MIT. | This plan |
| Agents use `langchain.agents.create_agent` or a hand-built `StateGraph` + `ToolNode`, **not** `create_react_agent`. | `create_react_agent` is deprecated in LangGraph v1 (docs.langchain.com/oss/python/migrate/langgraph-v1, verified 2026-11-06). | This plan |
| Streaming uses FastAPI's native SSE (`fastapi.sse.EventSourceResponse`) with our own event contract. | Native since FastAPI 0.135.0 (fastapi.tiangolo.com/tutorial/server-sent-events/, verified 2026-11-06). It sets `X-Accel-Buffering: no` and sends keep-alive pings. Our own contract avoids depending on the undocumented `useStream` wire format (§4). | This plan |
| The checkpointer is `AsyncSqliteSaver` on a volume. No Postgres service. | HITL `interrupt()` needs a durable checkpointer, and SQLite is enough for a single-laptop demo. The swap to Postgres is one line in `agents/runtime.py`. | This plan |
| **Embeddings come from Featherless AI**: `Qwen/Qwen3-Embedding-8B` through its OpenAI-compatible `POST https://api.featherless.ai/v1/embeddings`, requested at **1024 dimensions**. The client is the `openai` SDK with `base_url`, wrapped behind an `Embedder` protocol in `llm/embeddings.py`. | Featherless documents OpenAI-compatible embeddings with this model, batch input and an optional `dimensions` field (featherless.ai/docs/embeddings, 2026-11-06). The model is Apache-2.0 and supports output dimensions of 32–4096 (MRL) (huggingface.co/Qwen/Qwen3-Embedding-8B, 2026-11-06). 1024 keeps the Neo4j index small; 4096 is Neo4j's maximum. | User, 2026-11-06 |
| Chat models stay provider-agnostic. `CHAT_MODEL`/`FAST_MODEL` are `provider:model` strings resolved by `init_chat_model`. Featherless is a valid chat provider too, through the same OpenAI-compatible base URL. | Featherless says native function calling works on the Qwen 3 family and `moonshotai/Kimi-K2-Instruct` (featherless.ai/docs/tool-calling, 2026-11-06). Any Featherless chat model used for the agent must come from those families. | This plan |
| Telemetry stays in the gzipped NDJSON files and is queried with DuckDB, not loaded into Neo4j. | 1.7M rows add nothing to the graph, and condition checks are time-window scans. | Earlier session decision, 2026-11-06 |
| Bitemporal fields: `valid_from`/`valid_to` (valid time) and `recorded_at`/`retracted_at` (transaction time). Never delete: invalidate instead. | Elisa's reference architecture, plus the Graphiti pattern (`valid_at`/`invalid_at`/`created_at`/`expired_at`, getzep/graphiti `edges.py`, 2026-11-06). | This plan |

## 3. Current state (verified)

There is no application code yet. The repo holds the brief, Elisa's example and our generated corpus.

| Fact | Where |
|---|---|
| Elisa's original Teams example: the expert's fix is message seq 8. It is the only input we didn't generate. | `Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026/teams_chat.json:65` |
| Elisa's original email, with the "check the traffic counters" instruction | `Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026/email.txt:17` |
| Teams export: one JSON object per line with `message_id`, `conversation_id`, `created_at`, author, role, text, attachments | `mock_data/teams/messages/chan-noc-transport.jsonl:1` |
| The TRN-042 case thread in our corpus | `mock_data/teams/messages/chan-noc-transport.jsonl:13` |
| A standard-library OLE2/CFB `.msg` reader we can reuse for email ingestion (returns the property map) | `mock_data/scripts/verify_msg.py:17` |
| MAPI property ids for subject, body, sender and message-id | `mock_data/scripts/verify_msg.py:89` |
| Gold assertion schema: required fields | `mock_data/knowledge_graph/assertion_schema.json:5` |
| Gold schema: `recorded_at`, `superseded_by`, `corroborates` | `mock_data/knowledge_graph/assertion_schema.json:76` |
| Gold schema: temporal rules | `mock_data/knowledge_graph/assertion_schema.json:110` |
| Gold assertions (34). ASR-0001 is the TRN-042 rule; ASR-0024 is the Kemi code lock. | `mock_data/knowledge_graph/assertions.jsonl:1` |
| Validity convention (`NETWORK_EPOCH`, `SNAPSHOT_AT`) | `mock_data/generators/common.py:40` |
| Temporal validator. Its rules are what WS1 ports to Cypher. | `mock_data/scripts/verify_temporal.py:54` |
| Network layers carry `valid_from`/`valid_to` | `mock_data/generators/generate_network.py:499` |
| Inverse-link derivation (`superseded_by`, symmetric `contradicts`/`corroborates`), which WS3's reconcile step must reproduce | `mock_data/generators/generate_context.py:154` |
| TRN-042 as-built record (Kemi, `TNC-1X`, commissioned 2019-04-18) | `mock_data/network/as_built/devices.json:231` |
| Access register as a validity chain (Kemi padlock, then code lock) | `mock_data/knowledge_graph/access_register.jsonl:1` |
| Telemetry spec: "traffic zero" means `oper_status == UP and in_octets_mbps < 0.01` over 60 min | `mock_data/telemetry/spec/interface_counters.yml:42` |
| ITSM incident for the case | `mock_data/itsm/tickets.jsonl:25` |
| Jira task "Document TRN-042 workaround", still open (the pitch hook) | `mock_data/jira/issues.jsonl:18` |
| Answer key: a `not_documented` question (QA-034) | `mock_data/answer_key/questions.jsonl:17` |
| Answer key: CASE-001 (TRN-042) | `mock_data/answer_key/cases.jsonl:1` |

## 4. How the industry does it

This is set-up work for a product whose analogue is agent memory and temporal knowledge graphs. A competitor feature survey doesn't apply, so I researched the **industry method and tooling** instead. Everything below was accessed on 2026-11-06.

| Tool / pattern | Version, licence | Bearing | Source |
|---|---|---|---|
| langgraph | 1.2.20, MIT | Graph runtime, `interrupt()`, streaming `version="v2"` | pypi.org/pypi/langgraph/json |
| langchain `create_agent` + `HumanInTheLoopMiddleware` | langchain 1.4.9, MIT | Replaces `create_react_agent` → WS4 | docs.langchain.com/oss/python/langchain/agents |
| langchain-anthropic | 1.7.10, MIT | Claude 5 ids are plain strings. Use `with_structured_output(..., method="json_schema")`. **Don't set temperature** on Claude 5 models (400 error). | docs.langchain.com/oss/python/integrations/chat/anthropic |
| FastAPI native SSE | fastapi 0.162.0, MIT | Drop sse-starlette → WS5 | fastapi.tiangolo.com/tutorial/server-sent-events/ |
| Neo4j Community image | `neo4j:2026.10.1-community`, GPLv3 | Pin this tag. Cypher 25 is the default from 2026.02. | hub.docker.com/_/neo4j |
| Featherless AI | OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`). Flat-rate plans with **concurrency limits** (Basic 2 units, Premium 4) and **32K context** on Basic/Premium. | Batch embedding inputs; cap concurrency; keep agent prompts under 32K tokens if a Featherless chat model is used | featherless.ai/llms.txt, featherless.ai/docs/embeddings |
| Neo4j Python driver | `neo4j` 6.4.1, Apache-2.0 | Use one `AsyncGraphDatabase.driver` per app and never share sessions across tasks | neo4j.com/docs/api/python-driver/current/async_api.html |
| Vector search | Cypher 25 `SEARCH … IN (VECTOR INDEX …)`. `db.index.vector.queryNodes` is deprecated since 2026.04. | **Unverified by me:** the page wouldn't render for a direct check. Needs a spike in WS1. | neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/ |
| Graphiti (bitemporal edges) | graphiti-core 0.31.2, Apache-2.0 | Borrow the field model and invalidate-don't-delete; **don't adopt it**, because its own extraction schema conflicts with our gold scoring | github.com/getzep/graphiti `graphiti_core/edges.py` |
| `@langchain/langgraph-sdk` `useStream` against a custom backend | 1.14.0, MIT | The wire format comes from reading the source, not from a spec, and is moving to a new protocol. **Use our own SSE contract instead** → WS5 | forum.langchain.com/t/…/3271 |

**Verdict:** the bitemporal assertion graph matches the industry approach (Graphiti) and is not a differentiator in itself. The differentiators are **zero-effort capture** and **"not documented" honesty**, so WS2–WS4 carry the most weight.

## 5. Principles

1. **Neo4j is the source of truth for context.** `mock_data/` files are inputs and gold labels only. The API never reads gold labels at runtime, except in `seed` mode (WS1).
2. **Every assertion has provenance and validity, and there is one write path.** All writes go through `ContextRepository.assert_(…)`. It closes the predecessor's `valid_to` and sets `superseded_by` in the *same transaction*. Nothing is ever deleted; records are invalidated (`retracted_at`).
3. **No source, no claim.** Agent output that recommends an action must cite at least one `Source`. Otherwise the agent returns `not_documented` and writes a `KnowledgeGap`.
4. **Zero extra work for specialists.** Capture is passive. A one-tap confirmation is optional. Unconfirmed knowledge is stored as `unverified`, not dropped.
5. **Vendors stay behind the `llm/` boundary.** Models are named by config strings, not by imports.
6. **Temporal rules are executable.** The rules in `verify_temporal.py:54` run as Cypher checks in CI and after every ingest.

## 6. Workstreams

Repo layout produced by WS0. All paths are new.

```
backend/
  pyproject.toml  uv.lock  Dockerfile  .env.example
  src/silentctx/
    main.py            # app factory + lifespan
    config.py          # pydantic-settings
    logging.py         # structlog JSON, request_id
    api/               # routers: health, sources, assertions, entities, agent, gaps, eval
    domain/            # pydantic models (no I/O)
    graph/             # neo4j driver, repositories, cypher/, migrations/
    ingest/            # connectors + thread assembly + capture trigger
    extraction/        # LangGraph extraction graph
    agents/            # ops agent, tools, runtime (checkpointer), sse adapter
    llm/               # provider factory: chat + embeddings
    telemetry/         # DuckDB over mock_data/telemetry
    eval/              # scorer vs answer_key + gold assertions
  tests/ unit/ integration/ contract/ eval/
docker-compose.yml  (repo root: neo4j, api, web)
```

### WS0 — Scaffold, tooling and compose

**Why first:** every other workstream needs one command that runs, lints and tests.

**Changes:**
- **Package and tools:** a uv project (uv 0.13.x), Python 3.12, `src/` layout.
  - Pinned runtime dependencies: `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `neo4j`, `langgraph`, `langchain`, `langgraph-checkpoint-sqlite`, `openai` (Featherless client), `duckdb`, `structlog`.
  - Dev dependencies: `ruff`, `mypy` (strict on `src/`), `pytest`, `pytest-asyncio`, `httpx`, `testcontainers[neo4j]`.
- **Lifespan in `main.py`:**
  - on startup: create the one async Neo4j driver and call `verify_connectivity()`, open the checkpointer, and run the migrations;
  - on shutdown: close them in reverse order.
- **`config.py`:** `Settings` with fields:
  - `NEO4J_URI/USER/PASSWORD`
  - `CHAT_MODEL` (e.g. `"anthropic:claude-sonnet-5-5"`) and `FAST_MODEL` (e.g. `"anthropic:claude-haiku-5-5"`)
  - `EMBEDDING_BASE_URL` (default `https://api.featherless.ai/v1`), `EMBEDDING_MODEL` (default `Qwen/Qwen3-Embedding-8B`), `EMBEDDING_DIM` (default 1024), `FEATHERLESS_API_KEY`, `EMBEDDING_MAX_CONCURRENCY` (default 2, the Basic plan's limit)
  - `DATA_DIR`
  - `SEED_MODE`
  - `CORS_ORIGINS`
- **`docker-compose.yml`:**
  - `neo4j:2026.10.1-community` with a `/data` volume, healthcheck `cypher-shell 'RETURN 1'`, ports 7474/7687;
  - `api` with `depends_on: service_healthy`, uvicorn `--reload` in dev, and `mock_data` mounted read-only;
  - `web` (see plan 002).
- **Quality gates:**
  - pre-commit: ruff format + check, mypy.
  - GitHub Actions: lint, type-check, unit tests, and integration tests against a Neo4j service container.
- **Errors:** RFC 9457 `application/problem+json` handler; request-id middleware.

**Tests:**
- `GET /healthz` (liveness) and `GET /readyz` (Neo4j reachable and migrations applied).
- Settings fail fast when a required env var is missing.

**Off-nominal:**
- Neo4j not up: `/readyz` returns 503 and the API keeps retrying. It doesn't crash-loop.
- LLM key missing: the app starts, and `/readyz` reports the `llm` check as degraded. That keeps the seed/explore demo working offline.

### WS1 — Context store: schema, temporal repository, seed loader

**Why here:** WS2–WS5 all read or write the graph. With **seed mode**, the gold data is in Neo4j on day one, so the agent and the UI can be built before extraction works.

**Changes:**
- **`graph/migrations/*.cypher`:** idempotent and numbered. A `:Migration {id}` node records each one applied.
  - Uniqueness constraints on `Entity.entity_id`, `ContextAssertion.assertion_id`, `Source.source_id`, `Person.person_id`, `KnowledgeGap.gap_id`.
  - Range indexes on `valid_from`, `valid_to`, `recorded_at` and `status`.
  - A vector index on `ContextAssertion.embedding` and `SourceChunk.embedding`, with dimensions taken from settings.
- **Graph shape**, aligned with the gold schema (`assertion_schema.json:5`):
  - **Nodes:** `(:Entity:Device|Segment|Site|HwModel|Topic {entity_id, valid_from, valid_to, …})`
  - **Assertion:** `(:ContextAssertion {assertion_id, topic, statement, qualifiers_json, kind, status, confidence, valid_from, valid_to, recorded_at, retracted_at, embedding})`
  - **Assertion edges:** `-[:ABOUT {scope: observed_on|applies_to, resolution_confidence, surface_form}]->(:Entity)` and `-[:DERIVED_FROM {quote}]->(:Source)-[:AUTHORED_BY]->(:Person)`
  - **Assertion-to-assertion edges:** `SUPERSEDES`, `CONTRADICTS` and `CORROBORATES` between assertions
  - **Network topology:** `(:Device)-[:IS_MODEL]->(:HwModel)`, `(:Device)-[:LOCATED_AT]->(:Site)`, `(:Segment)-[:CONNECTS]->(:Device)`, each with its own validity
  - **Gaps:** `(:KnowledgeGap {gap_id, question, status, first_asked, routed_to})-[:ABOUT]->(:Entity)`
- **`graph/repositories/context.py`:** `ContextRepository` with these methods:
  - `assert_(a: AssertionIn) -> AssertionOut`: one transaction. Merge the entities, create the assertion and its edges, close the predecessor when `supersedes` is set (`valid_to = new.valid_from`), and mirror the `contradicts`/`corroborates` links. This reproduces `generate_context.py:154` at write time.
  - `retract(assertion_id, at)`
  - `valid_at(entity_ids, t, known_at=None, statuses=…) -> list[AssertionOut]`. The scope expands from node to `IS_MODEL` to `LOCATED_AT` to segment.
  - `neighbourhood(entity_id, t)`
  - `history(entity_id)`: every version, for the timeline UI.
- **`graph/checks.cypher`:** the `verify_temporal.py:54` rules as queries returning violations. `GET /admin/temporal-check` exposes them.
- **`graph/seed.py`**, a CLI: `silentctx seed --network --gold`.
  - Loads entities and topology from `mock_data/network/*` and `knowledge_graph/entities.jsonl`.
  - With `--gold`, also loads `assertions.jsonl` and `access_register.jsonl` (`access_register.jsonl:1`) **marked `origin: "gold"`**, so they can be wiped before an extraction run is scored.
- **Spike (≤1 h):** confirm the vector query syntax on `neo4j:2026.10.1-community`. If `SEARCH` doesn't work, fall back to `db.index.vector.queryNodes` (deprecated but functional) behind `graph/cypher/vector.py`.

**Tests:**
- Integration tests against Neo4j in testcontainers:
  - superseding closes the predecessor in the same transaction;
  - `valid_at` at a time before the Kemi lock change returns the padlock; after it, the code lock;
  - the `known_at` filter hides the code lock before 2026-08-18;
  - `contradicts` stays symmetric;
  - re-running seed is a no-op (idempotent).
- Unit tests for the Cypher builders.

**Off-nominal:**
- An unknown `subject_entity_id` on write: reject with 422. Never create orphan entities silently.
- Vector index dimension mismatch: refuse to start. The embedding dimension is checked in the lifespan.

### WS2 — Ingestion: connectors, thread assembly, capture trigger

**Why:** this is the "no extra work" half of the usability criterion.

**Changes:**
- **`ingest/connectors/`**, each yielding `SourceMessage {source_id, channel, thread_id, author, role, sent_at, text, attachments, raw_ref}`:
  - `teams_jsonl.py`, reading the format at `chan-noc-transport.jsonl:1`
  - `elisa_example.py`, for Elisa's original `teams_chat.json` and `email.txt`. These have times but no dates, so pass a demo date in config.
  - `outlook_msg.py`: reuse `read_cfb` (`verify_msg.py:17`) and the property ids (`verify_msg.py:89`). Move them into `ingest/msg_reader.py` and keep `verify_msg.py` importing them.
  - `pdf_notes.py`: text extraction for `documents/expert_notes/*.pdf`
  - `confluence_html.py` and `itsm_kb.py`: low-authority sources
- **`ingest/threads.py`:** group by `conversation_id`, or by email `In-Reply-To`/subject.
- **`ingest/trigger.py`:** decide when a thread is ready to extract.
  - Extract when there is a resolution signal: the `FAST_MODEL` classifier, plus cheap heuristics such as "traffic is back", 👍, or "fixed".
  - Or when the thread has been idle for N minutes (configurable).
  - Most of the ~800 chatter messages never reach extraction.
- **API:**
  - `POST /sources/replay {channel, thread_id|file}` replays a source into the pipeline, for the demo.
  - `POST /sources/ingest` runs a bulk import.
  - `GET /sources/{id}` returns a source with its quote spans.

**Tests:**
- Each connector parses its fixture.
- The `.msg` connector reads all 148 files (the same count as `verify_msg`).
- Thread assembly reproduces the TRN-042 thread.
- The trigger fires on the case thread and not on chatter. Report precision on a labelled sample.

**Off-nominal:**
- An unparseable file is logged and counted in an ingest report. It doesn't abort the batch.
- A duplicate `source_id` is a no-op (idempotent ingest).

### WS3 — Extraction graph (LangGraph)

**Why:** this turns conversations into linked, time-bound assertions, which is demo step 2 of the brief.

**Changes:** `extraction/graph.py` is a `StateGraph` over `ExtractionState {thread, candidates, links, decisions}`.

1. **`triage`** (FAST_MODEL): does the thread contain operational knowledge, and of which kind (diagnostic rule, access info, caveat, policy, known issue)?
2. **`extract`** (CHAT_MODEL, `with_structured_output(AssertionDraft, method="json_schema")`): produces the statement, conditions, actions and `avoid`. Every field carries a `quote` taken from the source. Drafts with no quote are dropped.
3. **`link`**: resolve surface forms to entities from **inventory attributes** (model, generation, install year, city) through Cypher lookups.
   - Exact IDs score 1.0.
   - "older units, 2019" resolves to `TNC-1X` via `HwModel.generation`.
   - **`entity_aliases.csv` is never read here.** It is scoring-only (§8).
4. **`reconcile`**: vector search plus a same-topic, same-entity lookup against assertions valid at the draft's `valid_from`. An LLM judge classifies the relation as `corroborates`, `supersedes`, `contradicts` or `new`, using source authority:
   - recency first;
   - then author role;
   - then channel (an expert's chat or email outranks an ITSM or Confluence page).
5. **`confirm`** (optional HITL): `interrupt({draft, links})`. The UI renders a Teams-style 👍/✏️ card.
   - With no response, `persist` writes `status="unverified"`.
   - Side effects before the interrupt must be idempotent, because the node re-runs on resume (docs.langchain.com/oss/python/langgraph/interrupts).
6. **`persist`**: `ContextRepository.assert_`, then compute the embedding.
7. **Gaps:** a later answer that closes a `KnowledgeGap` links the gap to the new assertion (`ANSWERED_BY`).

**Tests:**
- Golden test on Elisa's original `teams_chat.json`: one assertion, bound to TRN-042 and `TNC-1X`, `action = bounce_interface`, `avoid ⊇ {replace_sfp, reboot_node}`.
- The supersession chain on the Kemi barrier threads.
- The KMAX-4K pair comes out as `disputed` + `contradicts`.
- An extraction eval vs `assertions.jsonl:1` (precision and recall by subject, topic and status), run as `silentctx eval extract`.

**Off-nominal:**
- LLM timeout or invalid JSON: retry once (ModelRetry). Then park the thread as `extraction_failed`, visible in the UI, and never write half an assertion.
- Low-confidence link (<0.6): store the `ABOUT` edge with `scope="candidate"` and show it for confirmation.

### WS4 — Ops agent and tools

**Why:** demo step 3. It's the payoff, and it measures "suitability for agentic operations".

**Changes:**
- **`agents/ops_agent.py`:** `create_agent(model=CHAT_MODEL, tools=…, system_prompt=…, response_format=OpsAnswer, middleware=[HumanInTheLoopMiddleware(on=["execute_action"])])`. If the middleware API differs from the docs at implementation time, fall back to a `StateGraph` + `ToolNode` with an explicit `interrupt()` in an `approve` node.
- **Tools** (`agents/tools/`):

| Tool | Does |
|---|---|
| `get_entity_context(entity_id, as_of)` | Device record, model, site and segment from the graph, valid at `as_of` |
| `search_context(entity_id, symptom_text, as_of, known_at?)` | `ContextRepository.valid_at` + vector search, ranked by confidence × condition match |
| `check_telemetry(ne_id, port, metric_conditions, window)` | DuckDB over `mock_data/telemetry/*.ndjson.gz`. Evaluates the assertion's conditions against the spec at `interface_counters.yml:42`. |
| `get_alarm_trace(ne_id, from, to)` | Reads `network/alarms` |
| `get_site_access(site_or_segment, as_of)` | Access assertions and register |
| `record_knowledge_gap(question, entity_ids)` | Writes a `KnowledgeGap` and routes it to the most frequent author on that topic or entity |
| `execute_action(kind, target)` | **Simulated** (bounce interface, dispatch). Always interrupts for human approval. |

- **Output `OpsAnswer`:**
  - `{status: answered|not_documented|disputed`
  - `steps[]: {order, kind, text, citations[]}`
  - `avoid[]`
  - `citations: [{assertion_id, source_id, quote, author, recorded_at}]`
  - `gaps[]}`
- **`agents/runtime.py`:** builds the graph once at startup with `AsyncSqliteSaver`. `thread_id` is the incident or run id.

**Tests:**
- Scripted-model unit tests (a fake chat model), per tool.
- End-to-end for CASE-001 (`cases.jsonl:1`) on a *different* `TNC-1X` node: the recommendation includes the counters check and the bounce, and avoids the reboot.
- QA-034 (`questions.jsonl:17`) returns `not_documented` and creates a gap.
- A Kemi dispatch with `as_of=2026-08-10`:
  - with default `known_at=now`, returns the code lock;
  - with `known_at=2026-08-10`, returns the padlock (the bitemporal demo).

**Off-nominal:**
- A tool error is returned to the model as a tool message, and the run continues.
- Three consecutive tool errors end the run with `status="error"` over SSE. No fabricated answer.
- Telemetry is missing for a node, so a condition is `unknown`. The agent must say so and must not treat it as met.

### WS5 — API surface and SSE contract

**Why:** the frontend can only be built in parallel against a stable contract.

**Changes:**
- **Routers:**

| Route | Purpose |
|---|---|
| `GET /entities/{id}`, `GET /entities/{id}/neighbourhood?as_of=` | Explorer |
| `GET /entities/{id}/assertions?as_of=&known_at=&status=` | Explorer timeline |
| `GET /assertions/{id}`, `GET /assertions/{id}/history` | Detail and version chain |
| `POST /assertions/{id}/confirm`, `POST /assertions/{id}/retract` | HITL from the UI |
| `POST /runs/extract` (SSE), `POST /runs/agent` (SSE), `POST /runs/{run_id}/resume` | Streaming runs and interrupt resume |
| `GET /gaps`, `GET /conflicts` | Boards |
| `GET /sources/{id}`, `POST /sources/replay` | Provenance and demo replay |
| `GET /eval/latest` | Score table for the slides |

- **OpenAPI:** `generate_unique_id_function` gives clean operation ids for the typed client (fastapi.tiangolo.com/advanced/generate-clients/).
- **SSE events.** These are versioned (`"v": 1`) and every event carries `run_id` and `seq`:

| `event:` | `data` |
|---|---|
| `run.started` | `{run_id, kind: agent\|extract, input}` |
| `step` | `{node, status: started\|finished}` (from `stream_mode="updates"`) |
| `token` | `{text, node}` (from `stream_mode="messages"`, agent node only) |
| `tool.call` / `tool.result` | `{call_id, name, args}` / `{call_id, ok, summary, data_ref}` |
| `assertion.draft` / `assertion.saved` | `AssertionOut` (extraction) |
| `interrupt` | `{interrupt_id, kind: confirm_assertion\|approve_action, payload}` |
| `answer` | `OpsAnswer` |
| `run.completed` / `run.failed` | `{status}` / problem+json |

- **Adapter:** `agents/sse.py` maps `astream(..., stream_mode=["updates","messages","custom"], version="v2", subgraphs=True)` chunks to these events, and resumes after a reconnect via `Last-Event-ID`.
- **Contract test:** `tests/contract/sse_schema.json` is shared with the frontend fixtures (plan 002).

**Tests:**
- Contract tests on the event JSON Schemas.
- An `httpx.AsyncClient` test that reads a full agent SSE run from a scripted model.
- Resume after an `interrupt`.

**Off-nominal:**
- The client disconnects mid-run: the run continues to its checkpoint, and `GET /runs/{id}` returns its final state.
- The Vite dev proxy buffers SSE: the API sets `X-Accel-Buffering: no` (native), and plan 002 configures the proxy.

### WS6 — Observability and eval

**Why:** the judges score correctness of linking, and the slides need numbers.

**Changes:**
- **Logging:** structlog JSON with `request_id`/`run_id`, plus per-node timings. LangSmith tracing turns on only when `LANGSMITH_API_KEY` is set (optional).
- **`silentctx eval`** produces three outputs:
  - `extract`: precision and recall vs `assertions.jsonl` on subject, topic, status and validity (±15 min);
  - `qa`: the agent over the 21 questions and 11 cases, scored per `answer_key/README.md`;
  - `elisa`: Elisa's original example only.

  Results go to `eval/results/<date>.json`, which `GET /eval/latest` serves.
- **Score table:** state the provenance caveat next to it (we wrote the gold data ourselves, `mock_data/README.md`), plus a hand-written held-out set in `backend/tests/eval/heldout/`.

**Tests:** the scorer is unit-tested on hand-built prediction fixtures.

**Off-nominal:** an eval that runs with no LLM key reports `skipped` and doesn't return 0%.

## 7. Sequencing

The calendar is fixed, so the order matters.

| When | Backend | Unblocks |
|---|---|---|
| Fri 2026-11-06 evening | WS0, WS1 schema + seed `--gold`, vector spike, WS5 contract frozen (JSON Schemas committed) | Frontend can build against seeded data and fixtures |
| Sat 2026-11-07 morning | WS2 connectors + trigger. WS4 tools on seeded data. | Agent demo works without extraction |
| Sat 2026-11-07 afternoon | WS3 extraction, then WS4 HITL, then WS5 SSE end to end | Full capture → recall loop |
| Sat 2026-11-07 evening | WS6 eval and numbers; **feature freeze 22:00** | Slides |
| Sun 2026-11-08 06:00–10:00 | Bug fixes only, backup video, README run-book, submit | — |

## 8. Risks and open questions

| Risk / question | Impact | Owner / resolution |
|---|---|---|
| ~~TRN-042 timelines disagree~~ **Resolved 2026-11-06.** One shared timeline (`common.py:65`) now drives alarms, telemetry, Teams, email, ITSM and the vendor case: SFP inserted 05:15 UTC, ticket 06:15, chat 06:14–06:27, bounce 06:30, recovered 06:45 (`trn042_case.json:4`, `_case.json:2`) | — | Done |
| ~~Alias canonical column held phrases~~ **Resolved 2026-11-06.** Every alias now maps to an `entity_id` or an explicit `concept:` kind, and generation fails if a canonical id is unknown (`generate_knowledge_graph.py:38`, `generate_knowledge_graph.py:76`, `entity_aliases.csv:2`). Linking is scored on `kind=entity` rows only. | — | Done |
| Featherless might ignore `dimensions` for Qwen3-Embedding-8B and return 4096-dim vectors | Index dimension mismatch, so writes fail | A startup probe embeds one string and checks the length. If it gets 4096, truncate to the first 1024 and re-normalise (valid for MRL-trained models), or set `EMBEDDING_DIM=4096` |
| Featherless is down, out of credits, or rate-limited (concurrency units) | No new embeddings; vector search fails | Embeddings are computed at ingest and stored in Neo4j, so retrieval of existing assertions only needs the query embedding. If that fails, fall back to the graph-only scope query (`valid_at`) with no vector ranking. Never block the agent. |
| Neo4j `SEARCH` syntax unverified (§4) | Vector retrieval | WS1 spike; fallback `queryNodes` |
| Claude 5 models reject a non-default temperature (400) | Runtime errors | The `llm/` factory never sets sampling parameters |
| `interrupt()` re-runs the node on resume | Duplicate writes | Idempotent keys on `assert_` (source_id + statement hash) |
| SQLite checkpointer growth / single writer | Fine for the demo | Swap to `AsyncPostgresSaver` after the hackathon |
| Neo4j Community is GPLv3 | Matters for redistribution only. Elisa gets a licence to the outputs (brief §07). | Note in the README |
| The gold set is self-authored | Scores overstate quality | Held-out set + caveat on the slide (WS6) |

## 9. Out of scope

- Live Microsoft Graph / Exchange connectors, OAuth and webhooks. The connectors read exports. The pitch shows where Graph change notifications would plug in.
- Authentication and multi-tenancy. The local demo runs without auth (`CORS_ORIGINS` is locked to the web origin).
- Real device actions: `execute_action` is simulated.
- Voice and call transcription. These channels are marked dropped at source in `mock_data/knowledge_graph/drop_rate_sheet.csv`.
- Kubernetes or cloud deployment.
