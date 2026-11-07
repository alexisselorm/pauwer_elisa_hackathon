# kaiku

**An expert answers a question in Slack, and a minute later an AI agent cites that answer on site.**

Captures the operational knowledge experts share in Slack, Teams and email, binds it to the
network entities it is about with a validity period, and serves it to any AI agent over MCP.
Built for Elisa's Since AI 2026 challenge, *Capturing and Linking Human-Generated Contextual
Metadata for Autonomous Network Operations*; the brief and Elisa's example in
`Elisa_SinceAI2026/` are the only source of truth.

## The problem

Elisa's two examples (`Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026/use_case.md`):

- After an SFP replacement on TRN-042 the port is UP, optical levels are normal, alarms have
  cleared, and traffic is still zero. The field engineer suspects the new SFP. Erkki Expert
  knows that on this hardware it is a forwarding-state issue: disable and enable the
  interface, don't swap the SFP again, don't reboot. That rule lives only in a Teams chat
  and an email.
- An AI dispatches a field engineer to a site in Northern Finland and he finds a locked
  barrier on the private road. Where to get the key was written down only in an email.

An autonomous agent cannot use either piece of knowledge unless it is captured, tied to the
right devices and sites, dated, checked against the network, and retrievable at the moment
it is needed.

## How it works

```
 Slack threads ──┐
 email_drop/ ────┤                                       ┌─ Neo4j, five layers at time t ──┐
 Teams backfill ─┼─▶ triage ─▶ extract ─▶ gates V1–V6 ─▶ │ as-designed · as-built          │
 PDF, Confluence,│   (fast)    (chat)        │           │ as-running · context · events   │
 ITSM KB ────────┘                           │           └───────────────┬─────────────────┘
                                             │ V4                        │ MCP, bearer token per agent
                  network simulator ─▶ telemetry (DuckDB)                ▼
                                                    ops agent in Slack · any MCP client · console
```

1. **Capture without asking anyone.** The slackbot watches every channel it is in. When a
   thread is resolved ("Traffic is back 👍") or goes idle, a small model triages it; a
   kept thread is extracted by the chat model into assertions: conditions, action, what to
   avoid, scope, validity. One card appears in the thread, edited in place; its buttons
   are optional.
2. **Gates instead of trust** (plan 001 §6.1). V1 every field must quote the source
   verbatim; V2 a rule must come from an answer, not a question ("So another SFP?" becomes
   an *avoid*); V3 each bound entity must exist at `valid_from`; V4 the claimed symptoms
   are checked against telemetry in the last complete 15-minute interval; V5 the temporal
   invariants run in the same transaction as the write; V6 the same knowledge from a
   second channel corroborates instead of duplicating, a newer change supersedes, a
   disagreement becomes `disputed`.
3. **Five time-aware layers.** **As-designed** (planned nodes and links), **as-built**
   (devices, sites, segments, hardware models and generations), **as-running** (firmware,
   links, status, plus telemetry), **context** (the captured assertions, each bound to
   entities with `valid_from` / `valid_to` / `recorded_at`) and the **event stream**
   (alarms, device events, messages, outcomes, agent actions). One query cross-references
   all five at any instant, and *as known at* a second instant.
4. **The network confirms the knowledge.** When an agent's advice is carried out, the
   outcome comes back through MCP and the telemetry decides: recovery adds support,
   failure with the conditions met refutes it, failure with the conditions not met does
   not count. Confidence is Beta(1 + support, 1 + refute) and is always shown.
5. **Any agent reads it over MCP.** The Slack ops agent is only a reference client; it
   uses the same tools as everyone else.

**Slack stands in for Teams** in the live demo (Socket Mode needs no public URL). Elisa's
`teams_chat.json` and the synthetic Teams channels in `mock_data/teams/` flow through the same
ingest path via `kaiku backfill` (`backend/src/kaiku/ingest/backfill.py`); a Microsoft Graph
change-notification connector would plug into the same `MessageIn` interface
(`backend/src/kaiku/ingest/store.py`).

### MCP tools

Served at `http://localhost:8000/mcp`; the bearer token from `MCP_TOKENS` identifies the
agent, and every write is itself an event carrying that agent id. Times are ISO 8601 UTC;
`as_of` (what was valid then) and `known_at` (what had been recorded by then) default to now.

| Tool | What |
|---|---|
| `get_context` | Expert knowledge in scope for an entity at a time: the node, its model and generation, its segments, observations on devices of the same model |
| `search_context` | Semantic search over knowledge valid at a time (keyword ranking without embeddings) |
| `get_site_access` | How to physically reach a device or segment on the visit date: locks, keys, gates, roads, and the access register |
| `get_entity` | As-built and as-running facts: model, generation, firmware, status, segments, links |
| `get_events` | Alarms, device events and outcomes for an entity |
| `get_history` | How a piece of knowledge changed: versions, contradictions, corroborations, outcome evidence, flags |
| `check_conditions` | An assertion's symptoms checked against telemetry: each met, not met or unknown |
| `resolve_entities` | The graph entities a piece of text refers to, with a confidence each |
| `report_outcome` | What happened after an action; becomes evidence for or against the assertion |
| `propose_assertion` | New knowledge from an agent, stored `unverified` until a person confirms it |
| `open_knowledge_gap` | Records that something isn't documented and routes it to the most likely expert |
| `flag_assertion` | Asks a person to review a piece of knowledge |
| `ping` | The server version and your agent id |

### Results

The latest committed eval ([`eval/results/20261107T161529Z.md`](eval/results/20261107T161529Z.md))
captures every thread from scratch on an empty graph (gold never loaded) and asks the
questions through MCP, scored exactly. One run, so no variance is reported.

| Set | Passed |
|---|---|
| T0: Elisa's own example | 4/6 (both failures: `reboot_node` missing from the avoid list) |
| T2: held-out capture (hand-written, 9 threads) | 4/9 |
| T2: held-out QA (12 questions) | 8/12 |

The console's *Scorecard* page shows every run with the failed checks expanded.

## Repository

| Where | What |
|---|---|
| [`docs/plans/`](docs/plans/) | 001 backend and time model, 002 Slack and MCP, 003 acceptance tests and eval, 004 retrieval at time t, 005 console |
| [`docs/video/`](docs/video/) | The recap video and the HTML it is rendered from |
| [`slack/manifest.yaml`](slack/manifest.yaml) | The Slack app manifest |
| [`backend/`](backend/) | FastAPI + MCP server, Slack bot, network simulator, CLI ([README](backend/README.md)) |
| [`frontend/`](frontend/) | Web console: live knowledge graph, capture traces, agent runs, scorecard ([README](frontend/README.md)) |
| [`mock_data/`](mock_data/) | Synthetic network, chat, email and gold answers, all generated by us ([README](mock_data/README.md)) |
| [`demo/`](demo/) | Elisa's recap email as a `.eml` to drop during the demo |
| [`eval/heldout/`](eval/heldout/) | Hand-written held-out threads for the eval |
| [`eval/retrieval/`](eval/retrieval/) | Retrieval-only eval: search results scored against the gold assertions (`kaiku eval retrieval`) |
| [`eval/results/`](eval/results/) | Committed eval runs, shown on the console's Scorecard |

## Run the demo

Needs Docker with Compose v2.24+, a Slack workspace you can install an app into, and a
Featherless API key. Plan 30 minutes for the first run (image build, backfill triage).

1. **Environment.** `cp backend/.env.example backend/.env`, then set:
   - `MCP_TOKENS`: one bearer token per agent, e.g. `{"ops-agent": "<token>", "inspector": "<token>"}`
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`);
   - the Slack tokens and channel ids from the [Slack app](#slack-app) below;
   - `FEATHERLESS_API_KEY`. With the default models it is the only LLM key used: chat
     (Kimi-K2), triage (Qwen3-4B) and embeddings (Qwen3-Embedding-8B) all go through
     Featherless. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` matter only when `CHAT_MODEL` or
     `FAST_MODEL` name those providers. See [Without an LLM key](#without-an-llm-key) for
     what still works without it.
2. **Start.** `docker compose up --build`. Wait until the table below is green
   (`docker compose ps`); the slackbot's log says when it is connected to Slack.
3. **Load the network.** `docker compose exec api kaiku seed --network --profile demo`
   (idempotent, safe to repeat). The five layers are now in Neo4j; the console shows the
   entities and no knowledge yet.
4. **Load the history.** Either
   - `docker compose exec api kaiku backfill` (demo profile): Elisa's chat is **not** in it
     (plan 001 §3.5 keeps every source about the forwarding stall after an SFP swap out, so
     step 5 is the first time that knowledge enters), but the Kemi access history, the
     expert PDF note, the Confluence pages and the ITSM KB are. The backfill only stores
     events; the slackbot's capture loop then triages them, 50 threads per
     `CAPTURE_SWEEP_SECONDS` (60 s) with the fast model, and the knowledge appears as it
     goes. Watch progress in `#context-log` (one line per decision) and on the console's
     **Captures** page. Expect 10 to 20 minutes for the demo profile; the Kemi code-lock
     knowledge that demo chat 1 (step 8) relies on is there when a Kemi capture shows
     "code lock" on the Captures page.
   - or press **Reset** in the console header (`POST /api/ui/reset`, localhost only): it
     wipes everything captured or recorded, loads the network again and the gold
     assertions from `mock_data/knowledge_graph/` **minus** the forwarding-stall knowledge,
     so the graph is at the demo baseline in seconds without any LLM call. The Slack
     channel cursors are kept, so old threads are not captured again.
5. **Capture Elisa's chat** (strong demo step 1). Either
   `docker compose exec api kaiku replay --channel '#noc-transport'` posts Elisa's 12
   messages into the thread as the two personas (Veikko, Erkki), with the original gaps
   shortened 30× (`--speed`), or two people type them from the brief's Appendix A2. After
   "Did that. Traffic is back 👍" the capture runs; within about a minute the thread gets
   **one** card: *Captured for TNC-1X (Gen1) · seen on TRN-042 · after SFP replacement*,
   with the conditions, the action, the avoid list, the validity and "telemetry agrees ✓".
   Nobody has to click anything. The console's graph adds the assertion live; open it to
   see the quotes and the gates.
6. **Capture the email** (the same knowledge by a second channel).
   `cp demo/elisa_email.eml email_drop/`. The slackbot polls the folder every
   `EMAIL_POLL_SECONDS` (10 s), stores the mail with its `Date` header as event time and
   triages it at the next sweep. The capture is logged in `#context-log`, never in a work
   channel, and the Slack card from step 5 gains "+1 source (email)" instead of a
   duplicate rule.
7. **Recall on another node** (strong demo step 3). Inject the fault on TRN-017 (Pori, the
   same model):

   ```sh
   curl -XPOST localhost:8100/sim/fault -H 'content-type: application/json' \
     -d '{"node":"TRN-017","port":"1/1/2","kind":"sfp_swap_stall"}'   # started 75 min ago by default
   ```

   The bot posts the fault in `#noc-alarms` and, in its thread, the ops agent's advice:
   conditions checked on live telemetry (oper UP ✓, RX power ✓, counters at zero ✓), the
   next step (check the counters, then disable and enable 1/1/2), what to avoid (SFP swap,
   reboot), and the citation to Erkki's message from step 5. Click **Run bounce
   (simulated)**: the simulator recovers the counters, the agent reports the outcome
   through MCP and the thread ends with "network-confirmed on TRN-017 1/1/2 · support 1".
   (`{"node":"TRN-017","port":"1/1/2","action":"bounce_interface"}` on `/sim/action`
   does the same by curl; add `"outcome":"fail"` to show a refutation.)
8. **The two demo chats** (from the host, in `backend/`, they read `backend/.env`):
   - `uv run python scripts/demo_existing_context.py`: Veikko asks about the Kemi gate and
     Erkki answers with what the graph already holds (padlock, then the code lock); the
     card corroborates the existing knowledge ("+1 source"). Then ask yourself in the
     channel: `@kaiku How do I get through the gate at the Kemi site?`
   - `uv run python scripts/demo_new_knowledge.py`: facilities announce an eleven-day
     renovation at Lappeenranta from next Monday; the card shows a new assertion with a
     validity window in the future. Drag the console's *As of* slider into and out of it.
   - `uv run python scripts/demo_pori_access.py` (the video's scene 2): street works close
     the Pori front gate until a Friday about two weeks out, and the back gate's key box
     code comes from the NOC duty phone. kaiku captures a new assertion on TRN-017 with
     that end date; the script prints next Monday's date so you can ask the bot about a
     visit inside the window.

   Before a recorded take, `uv run python scripts/fill_channel.py` posts everyday chatter
   (no node, site or fix, so nothing is captured) to push earlier test threads out of
   view; press **Reset** within `CAPTURE_IDLE_MINUTES` of it.
9. **Any agent.** `npx @modelcontextprotocol/inspector`, transport *Streamable HTTP*, URL
   `http://localhost:8000/mcp`, header `Authorization: Bearer <your inspector token>`.
   Call `ping` to see which agent you are, then `get_context` with `entity_id: TRN-017` to
   read the same knowledge the Slack agent used, and `get_site_access` with
   `entity_or_site: Kemi`, `as_of: 2026-08-10`, `known_at: 2026-08-10` to see why a
   dispatch on that date would have met a padlock.
10. **Time travel.** In the console (http://localhost:8080) drag *As of* to see what was
    valid at any moment; tick *Only what was known then* to also hide what had not been
    recorded yet. *Captures* shows each thread's gates (V1–V6) with the quotes
    highlighted, *Agent runs* every MCP call, *Scorecard* the latest eval. *Trace ↗* opens
    the same run in Phoenix.

| Service | URL | Healthy when |
|---|---|---|
| `neo4j` | http://localhost:7474 (bolt 7687) | `cypher-shell 'RETURN 1'` works |
| `api` | http://localhost:8000 — MCP at `/mcp` | `/readyz` is 200: Neo4j answers and the schema is applied |
| `slackbot` | Socket Mode, no public URL | connected to Slack (without tokens it idles and says so in its log) |
| `sim` | http://localhost:8100 | `/healthz` is 200 |
| `web` | http://localhost:8080 | the console loads; the header says **Live** |
| `phoenix` | http://localhost:6006 | traces appear under project `kaiku` |

Everything binds to 127.0.0.1. Neo4j's password defaults to `kaiku-local-dev`; set
`NEO4J_PASSWORD` in your shell to change it (compose passes it to both Neo4j and the API).
An existing `neo4j-data` volume keeps the password it was created with (the old default was
`silentctx-local-dev`): `docker compose down -v` resets it.

### Slack app

1. Create the app at https://api.slack.com/apps → *Create New App* → *From a manifest*,
   paste [`slack/manifest.yaml`](slack/manifest.yaml). It enables Socket Mode, the bot
   scopes, the events and the "Capture this as knowledge" message shortcut.
2. *Basic Information* → *App-Level Tokens*: generate one with `connections:write`. That is
   `SLACK_APP_TOKEN` (`xapp-…`).
3. *Install App* → install to the workspace. The *Bot User OAuth Token* is
   `SLACK_BOT_TOKEN` (`xoxb-…`).
4. Create the channels and invite the bot (`/invite @kaiku`) to each:
   - `#noc-transport`: the work channel the demo captures from. Its id goes in
     `SLACK_CAPTURE_CHANNELS` (comma-separated channel ids; the bot captures every channel
     it is in unless this narrows it);
   - `#noc-alarms`: where faults are posted with the agent's advice (`SLACK_ALARM_CHANNEL`;
     unset = a channel named `#noc-alarms`, else the first capture channel);
   - `#knowledge-gaps` or `#noc-transport` again: where [Ask <expert>] posts an
     undocumented question (`SLACK_GAP_CHANNEL`; unset = the channel the question was asked
     in);
   - `#context-log`: ignore decisions, email and backfill captures, never work-channel
     noise (`SLACK_CONTEXT_LOG_CHANNEL`; unset = a channel named `#context-log`, else the
     log only).

   A channel id starts with `C` and is at the bottom of the channel's *About* tab.
   Other names work; only the ids in `.env` matter.
5. Email capture also runs inside the slackbot (it polls `email_drop/` and posts to
   `#context-log`), so it needs the Slack tokens too.

### Without an LLM key

`/readyz` says `degraded` and the stack runs, but: no thread is triaged or extracted (the
sweeper retries them every `CAPTURE_SWEEP_SECONDS`, and they are captured once a key is
added and the slackbot restarted), an `@kaiku` mention answers "Sorry, I couldn't answer
that right now", and the fault thread posts a warning instead of advice. What still works:
seeding, the backfill (events are stored), **Reset** (gold knowledge, no LLM), MCP reads
(`search_context` falls back to keyword ranking), the console and the simulator.

### What the bot does on its own

The slackbot captures every channel it is in (`SLACK_CAPTURE_CHANNELS` narrows that). Each
`CAPTURE_SWEEP_SECONDS` it triages threads that have news and are resolved ("Traffic is
back", ✅/🎉, the shortcut) or idle for `CAPTURE_IDLE_MINUTES`; a resolution signal triggers
a sweep at once. A kept thread gets one card, edited in place; its buttons (✅ confirm,
✏️ fix, 🚫 not knowledge) are optional.

It watches the network all the time and posts each fault in `#noc-alarms` with the ops
agent's advice in the thread: conditions, next step, avoid and sources, plus
[Run bounce (simulated)] [Done manually ✓] [Didn't work ✗]. A click reports the outcome
through MCP; Run bounce also checks the live counters first. When the agent finds nothing
documented, its answer offers [Ask <expert>], which posts the question in
`SLACK_GAP_CHANNEL`; the expert's reply in that thread is the capture and closes the gap.

`check_conditions` pulls the simulator's 15-minute samples into the same DuckDB view as the
history; `GET /sim/live?node=TRN-017&port=1/1/2` is the fine series and `GET /sim/events` the
event feed. Restarting `sim` resets the network to healthy. Fault nodes: TRN-017 / TRN-019
(TNC-1X) and TRN-010 (TNC-2X, outside the rule's scope), ports 1/1/2 and 1/1/3.

**Email capture:** drop a `.eml` (or Outlook `.msg`) into `email_drop/`; `.txt` files are
ignored, and a mail without `Date` or `Message-ID` is rejected, which is why Elisa's
`email.txt` ships as [`demo/elisa_email.eml`](demo/elisa_email.eml). The 145 `.msg` files in
`mock_data/email_outlook/` come in with `kaiku backfill`.

**Scorecard:** the console's *Scorecard* page shows the runs committed under
`eval/results/` (compose mounts the folder into the api). A new run adds one: from the
host, `cd backend && uv run kaiku eval run --set elisa` (needs the Featherless key in
`backend/.env` and Docker; `--set all` adds the held-out set) captures the threads on a
throwaway Neo4j started by testcontainers, gold never loaded, asks the questions through
MCP and scores them exactly. It runs on the host because the api image has neither Docker
nor the dev dependencies; inside the container it would need `--neo4j-uri` pointing at a
Neo4j you are willing to fill with eval captures. `kaiku eval rescore eval/results/<run>.json`
re-scores a saved run with the current scorer in seconds (no Neo4j, no LLM), `kaiku eval
retrieval` scores `search_context` alone against the gold graph, and
`docker compose exec api kaiku eval time` runs the temporal checks on the live graph
without an LLM.

**Reset:** the console's **Reset** button (`POST /api/ui/reset`, accepted only from
localhost origins) puts the graph back to the demo baseline between runs: every capture,
agent run and outcome is deleted, and the network and the gold assertions minus the
forwarding-stall knowledge are loaded again. The demo chats in `backend/scripts/` start from
that state. `docker compose down -v` deletes everything, checkpoints included.

### Time conventions

All times are stored in UTC. A date stated in a message ("from Monday", "until 20 November")
means 00:00 Europe/Helsinki on that day; as an end it is exclusive. Elisa's example has local
times and no date, so its backfill uses 2025-10-14 in Europe/Helsinki (EEST, UTC+3) with
synthetic seconds to keep messages in the same minute ordered (plan 001 R8). A live replay
gets the real Slack `ts`, so its validity starts "a minute ago", and the fault injected
afterwards is recalled against the same "now".

## Development

```sh
cd backend && uv sync && uv run pre-commit install   # ruff, mypy --strict, unit tests on commit
uv run pytest -m "not live"                          # unit + Neo4j integration (testcontainers)
cd ../frontend && npm ci && API_URL=http://localhost:8000 npm run dev   # :5173, /api proxied
```

CI (`.github/workflows/ci.yml`) runs the same checks plus the frontend's typecheck, lint and
tests on every push. Tests marked `live` call real LLMs and Slack and run only by hand.
Details: [`backend/README.md`](backend/README.md), [`frontend/README.md`](frontend/README.md).
