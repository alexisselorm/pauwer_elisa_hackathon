# 001 — Backend: context layer, time model, capture, validation

Status: **Revised 2026-11-06 (v2).** Changes from v1: Slack replaces the React console as the user surface (plan 002). An MCP server is the only interface for agents, ours included (plan 002). The time model was verified against Elisa's brief and diagram (§3). Validation now comes from the network as well as from people (§6). Acceptance tests and the eval are in plan 003. Open: Neo4j `SEARCH` syntax spike; whether Featherless honours `dimensions` (§10).

Companion plans: [`002-slack-and-mcp.md`](002-slack-and-mcp.md) (user and agent surfaces), [`003-acceptance-and-eval.md`](003-acceptance-and-eval.md) (success tests and eval).

## 1. Goal

The only source of truth is `Elisa_SinceAI2026/` (the brief, the example and `reference_architecture.png`). Everything below traces back to it.

| Brief requirement (§02, §06) | How we meet it | Tests (plan 003) |
|---|---|---|
| Capture knowledge "with minimal or no additional work for specialists", "ideally from the channels they already use" (**primary criterion**) | Passive capture from Slack channels and an email inbox. At most one bot message per thread. Zero required actions; one optional tap. | ST-01…ST-07 |
| "Bind it to the right network context" / "correctness of linking (right entity, right time validity)" | Each assertion is bound to the device it was observed on, the class it applies to and the activity concept (§5). The time rules are in §3. | ST-10…ST-29 |
| "Make it available to AI agents so autonomous workflows continue end to end" / "suitability for agentic network operations" | An MCP server with read and write tools. Our Slack ops agent uses MCP only, so any other agent can do the same. | ST-30…ST-37, ST-50…ST-55 |
| Strong demo step 3: "proposes the right next step… before hardware is replaced or the unit is rebooted" | The agent checks the assertion's conditions against telemetry before it recommends anything. `avoid[]` is enforced. | ST-30, ST-31 |
| Teams are encouraged to test "advice that later becomes outdated, or conflicting advice from two experts" | Supersession and contradiction are first-class. Outcomes from the network act as evidence. | ST-25…ST-27, ST-40…ST-44 |

**Who it's for:** Elisa's judges (review 2026-11-08 10:00–13:00), and, in the story, field engineers and NOC experts in Slack plus any autonomous-ops agent through MCP.

**Constraints:**
- The deadline is 2026-11-08 10:00.
- The stack is FastAPI, LangGraph, Neo4j, Slack (Bolt) and MCP.
- The LLM layer is provider-agnostic.
- Everything runs locally with `docker compose`, and Slack uses Socket Mode, so no public URL is needed.
- Data is synthetic only.

## 2. Decisions

| Decision | Reason | Settled |
|---|---|---|
| **Slack is the human surface. There is no React app.** | The experts already work in Slack, and the brief's primary criterion is "from the channels they already use". A separate web console is extra work for them. | User, 2026-11-06 |
| **MCP is the only agent interface.** Our own ops agent goes through MCP too, never through Python imports. | "Whatever agent Elisa has" must get the same capability. Dog-fooding MCP proves it. | User, 2026-11-06 |
| Agents may **write evidence, gaps and proposals**. They may **not** confirm, retract or edit validity. | People and network outcomes own truth. Agents add evidence (§7). | This plan |
| FastAPI + LangGraph + Neo4j, self-hosted LangGraph inside FastAPI, `create_agent` rather than `create_react_agent`, `AsyncSqliteSaver` | Unchanged from v1 (licence and deprecation reasons are in v1's git history, commit 66d7bfe). | v1 |
| Embeddings: Featherless `Qwen/Qwen3-Embedding-8B` at 1024 dims behind an `Embedder` protocol | Unchanged from v1. Vector search is ranking only; retrieval is scoped by entity and time first. | User, v1 |
| Telemetry stays in NDJSON and is queried with DuckDB. Live-demo telemetry comes from the network simulator (WS6). | The demo happens "now", but the 2025 corpus is historical (§3.5). | This plan |
| **Retrieval at t: filter by validity first, then rank; a superseded assertion is never shown alone** ([004](004-temporal-aware-retrieval.md), R11–R12). No age decay, no temporal-type field, no reading the time from the question. | Elisa's model is binary validity ("still believed true"). An external temporal-retrieval design was reviewed, and only the parts that serve Elisa's criteria were kept (004 §4). | User, 2026-11-07 |
| **The SSE contract from v1 is dropped.** | There's no browser client. Slack handlers call the agent in-process and update one Slack message as it progresses. | This plan |

## 3. Time model — verified against Elisa's brief (critical)

### 3.1 What Elisa says (verbatim)

- Brief §04: *"Each context assertion is bound to a graph entity and carries valid_from / valid_to timestamps (valid_to defaults to 'still believed true'), so all layers can be cross-referenced for any point in time."*
- Diagram, context layer: *"each context / metadata assertion has superseded timestamp and expiry (defaults to infinity i.e. 'we still believe it's true')"*.
- Diagram, event stream (as-observed): *"record time = event time"*.
- Diagram, runtime layer: *"snapshot of topology & CM data for given time"* and *"time-series PM data is time-aware by nature"*.
- Diagram, top: *"For any given time, [we] can cross-reference all layers"*, with the binding *Context Assertion → Graph Entity*.
- Brief §06, strong demo: *"stored as a context assertion linked to the relevant entities (e.g. node TRN-042, older transmission units, SFP replacement) with its source and validity period."*

### 3.2 What that means

1. **Two kinds of time.** Events (the event stream) have **one** timestamp. The moment something happened is the moment it is on record, so an event needs no validity interval and is never revised. Everything else is a *state* with a validity window. That includes the design and as-built layers, runtime snapshots and context assertions.
2. **Why context needs `valid_from`/`valid_to`.** "Record time = event time" is stated *only* for the event stream. For context, the two differ: a lock is changed before anyone emails about it, and a change is announced in advance. That is exactly why the context layer carries an explicit window.
3. **`valid_from` is the "superseded timestamp".** It is the moment this assertion starts to hold, and therefore the moment it replaces its predecessor. **`valid_to` is the expiry.** Its default is null, which means ∞, "we still believe it's true".
4. **Validity is about the world, not about doubt.** A failed fix or a disagreeing expert does not mean the knowledge stopped being true at that moment. Doubt changes `status` and `confidence`, never `valid_to`.
5. **Linking includes the activity.** Elisa's own example binds to a node, a class ("older transmission units") *and* an activity ("SFP replacement"). We bind to all three (§5).
6. **Cross-referencing at t** means one query can answer all of these for one instant: which assertions are valid, whether the bound entity existed, what the as-running snapshot looked like (model, firmware) and what the telemetry said.

### 3.3 Our rules (enforced in code; the R-numbers are cited by tests)

| # | Rule |
|---|---|
| R1 | Every source item is an **Event** with a single `event_time` taken from the platform: Slack `ts`, the email `Date`, alarm raise time, telemetry interval, or the time an outcome was observed. Wall-clock time at ingestion is stored only as `ingested_at` for operations and is **never** used in a temporal query. |
| R2 | `assertion.recorded_at` = the `event_time` of the source event that completes the assertion (for Elisa's chat: Erkki's message 8). It is **derived from the event stream**, not read from a clock, so it stays inside Elisa's model and isn't a new timeline. |
| R3 | `valid_from` = an explicit start stated in the source, resolved against the source's `event_time` and the Europe/Helsinki timezone ("from Monday", "since 3 August", "from 1 December"). Otherwise it equals `recorded_at`. It is never extraction time. If it falls before the bound entity's own `valid_from`, it is clamped and flagged. |
| R4 | `valid_to` is null ("still believed true") unless one of these applies: (a) the source states an expiry ("until 20 November"); (b) a successor supersedes it, in which case `pred.valid_to = succ.valid_from`, Elisa's "superseded timestamp"; or (c) every bound observed entity ends (decommissioning), in which case it equals that entity's `valid_to` and the status becomes `expired`. |
| R5 | Doubt changes `status`, `confidence` or `review_due`, never `valid_to`. |
| R6 | Nothing is deleted or overwritten. **Correction** and **change** are different operations: (i) a world change means a new assertion supersedes the old one (R4b); (ii) an extraction mistake corrected by a person means the wrong version gets `retracted_at` = the event time of the correction, and the fixed version **keeps the original `valid_from`**. |
| R7 | Event order is `event_time` order, with ties broken by platform sequence (Slack `ts` has microseconds). Messages in a thread are strictly increasing. |
| R8 | Everything is stored in UTC. Elisa's example has local times and no date, so the backfill sets the date to 2025-10-14 and the timezone to Europe/Helsinki (EEST, UTC+3), and adds synthetic seconds to keep messages in the same minute ordered. This is stated in the README. |
| R9 | The `as_of` query rule: an assertion is in scope at t ⇔ `valid_from ≤ t < coalesce(valid_to, ∞)` **and** a bound entity exists at t **and** its qualifiers (model, generation, firmware) match the as-running snapshot at t. |
| R10 | (Optional extension, labelled as ours.) `known_at` adds `recorded_at ≤ known_at`. This is still a cross-reference to the event stream, because by R2 `recorded_at` is an event time. It answers "why did the AI dispatch on 08-10 without the code?" |
| R11–R12 | A superseded assertion is never shown without its successor (R11); filter by validity at t first, then rank (R12). See [plan 004](004-temporal-aware-retrieval.md) §3. |

### 3.4 Problems found in our data (2026-11-06) and their fixes

Our generator snapped every timestamp to a 15-minute grid. That broke R1, R2 and R7. Evidence:

| Problem | Evidence (before the fix) | Fix |
|---|---|---|
| 11 of Elisa's 12 chat messages had the same timestamp, so their order was lost | `teams/messages/chan-noc-transport.jsonl`: message 1 at 06:15Z, messages 2–12 all at 06:30Z | Elisa's minutes converted to UTC (06:14…06:27Z), with synthetic seconds so equal minutes stay ordered |
| The engineer said "Traffic is back" *before* the recorded recovery | Chat 06:30Z vs `TRN042_CASE.recovered` 06:45Z | Advice 06:22:08Z, bounce 06:23:00Z, re-enable 06:23:40Z, recovered 06:24Z, "Traffic is back" 06:25:04Z. Telemetry bins: 06:00 zero, 06:15 partial, 06:30 normal. |
| The gold assertion's time didn't match its source | ASR-0001 `valid_from`/`recorded_at` 06:15Z, but its `source_ref` (message 8) was at 06:30Z; Elisa's real time is 06:22Z | R2: 06:22:08Z |
| The recap email was snapped | Case time 08:40, `.msg` stamped 08:45 | Exact 08:40:00Z; the `.msg` `Date:` header now shows local time with `+0300` |
| Point events were stored as "active alarms" that never cleared | MODULE_INSERTED, LINK_UP and OPTICAL_POWER_OK in `trn042_case.json` | Stored as point events |

Also fixed: email dates depended on the generating machine's timezone, and 2023 firmware events had been clamped to 2025-10-01. The 15-minute grid now applies only to telemetry, because PM data is interval-based by nature, and a sample's timestamp marks the **start** of its interval. `scripts/verify_temporal.py` gained checks for R2, R7, "record time = event time" on event rows, and the TRN-042 causal order. These run in `generate.py` (and will gate CI once it exists). The detailed change log is in the mock_data README. The corpus was also trimmed to telecom content: 87 MB → 3.2 MB; 582 Teams messages in 4 channels; 145 emails; 24 gold assertions; 15 questions; 9 cases.

Linking errors found in the same review (the brief scores "right entity"), also fixed in the generator:

| Problem | Fix |
|---|---|
| Two chat messages named the wrong model: TRN-019 was called a TNC-2X (it is a TNC-1X), and the Gen2 scope story used TRN-009 "Pulkkila", which is a TNC-3X in Kuopio | The Gen2 story is now on TRN-016 (Mikkeli, TNC-2X), TRN-019 is called a TNC-1X, and the generator fails if a chat names a model the network doesn't have |
| The Ivalo access knowledge (ASR-0025, access register) was bound to SEG-012, the Joensuu segment, which isn't a private road | Bound to SEG-011 (Ivalo, private road); the register row says "gate, unlocked" as the source does |
| The network called the Kemi barrier a "boom" while every message calls it a gate; Kajaani was a "gate" while held-out H-02 says "boom" | Network barrier types now match the documents (Kemi and Ivalo gate, Kajaani boom) |

### 3.5 "Now" versus history

The corpus is historical (2025-10 to 2026-11-07), and Slack cannot post messages with past timestamps. So:
- **Backfill** ingests exported Teams JSON, `.msg` email, PDFs and KB pages with their **original** event times (R1).
- **Live** Slack messages get Slack `ts` (now). In the demo, Elisa's chat is captured **live**, so `valid_from` is a minute ago. It is then recalled minutes later on another node, whose telemetry comes from the simulator at the same "now". This is internally consistent, with no fake clocks.
- The demo profile excludes **every corpus source about the forwarding stall after an SFP swap** from the backfill, so the live capture really is the first time this knowledge enters the system: the 2025 TRN-042 thread and its follow-ups, Erkki's recap email (forwarded live in demo step 1 instead), the later "same thing again" messages, and the 2026-03 Gen2 scope widening (ASR-0006). Otherwise the agent would already know the rule, and ST-44 / H-09 (scope widening to TNC-2X) would have nothing to widen. The 2019 PDF note and the stale ITSM KB stay in, as corroboration and contradiction. The full backfill is used only for the T1 regression eval.

## 4. Current state (verified)

There's no application code yet. Inputs:

| Fact | Where |
|---|---|
| Elisa's chat; the fix is message seq 8 | `Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026/teams_chat.json` |
| Elisa's email | `…/email.txt` |
| Gold assertion schema and temporal rules | `mock_data/knowledge_graph/assertion_schema.json` |
| Temporal validator (ported to Cypher in WS1) | `mock_data/scripts/verify_temporal.py` |
| Devices: 9 × TNC-1X (Gen1, 2018–2019), 7 × TNC-2X, 6 × TNC-3X. TRN-042 is in Kemi, commissioned 2019-04-18. TRN-017 (Pori, TNC-1X) is the live-demo node. | `mock_data/network/as_built/devices.json` |
| Firmware-upgrade events (as-running changes) | `mock_data/network/events/device_events.jsonl` |
| Kemi access as a validity chain (padlock, then code lock; recorded after valid) | `mock_data/knowledge_graph/access_register.jsonl` |
| The "traffic zero" rule is `oper_status == UP and in_octets_mbps < 0.01` over 60 minutes. PM samples are stamped at the interval start. | `mock_data/telemetry/spec/interface_counters.yml` |
| Historical PM data covers only 2025-10-07 → 2025-10-21 (the TRN-042 case ± one week), so V4 on other backfilled threads returns `unknown`, not a disagreement | `mock_data/telemetry/interface_counters_trn_15min.ndjson.gz` |
| Answer key (regression set) | `mock_data/answer_key/` |
| Held-out set (hand-written, not generated; its node, city and model claims match the network data) | `eval/heldout/` |
| Standard-library `.msg` reader | `mock_data/scripts/verify_msg.py` |

## 5. Graph model

- **Events (as-observed):** `(:Event:Message|Email|Alarm|DeviceEvent|Outcome|AgentAction {event_id, event_time, ingested_at, channel, …})`. These are immutable and have no validity fields (R1). Example: `(:Message)-[:IN_THREAD]->(:Thread)`, `(:Event)-[:ON]->(:Entity)`.
- **Capture state (operational, never used in temporal queries):** `(:Thread {thread_id, channel, last_message_ts, last_triaged_ts, status: pending|captured|ignored|extraction_failed, triage_kind: rule|access|correction|none, triage_reason})` records what the capture loop has read and what it decided. `(:ChannelCursor {channel, last_seen_ts})` is the per-channel watermark (WS2).
- **Entities (design, as-built and as-running layers):** `(:Entity:Device|Port|Segment|Site|HwModel|Generation|Activity {entity_id, valid_from, valid_to})`. Devices connect to their model, generation, site and segment through edges that carry their own validity. Firmware is an as-running snapshot with its own validity.
- **Context assertion:** `(:ContextAssertion {assertion_id, topic, statement, qualifiers_json, kind, status, confidence, valid_from, valid_to, recorded_at, retracted_at, review_due, support_n, refute_n, origin: extracted|agent_proposal|gold, embedding})`
- **Binding edges (Elisa's "BINDING"):**
  - `-[:ABOUT {scope: observed_on|applies_to|activity, resolution_confidence, surface_form}]->(:Entity)`
  - For Elisa's case: `observed_on → TRN-042`, `applies_to → TNC-1X` / `Gen1` ("older transmission units"), `activity → Activity:sfp_replacement`.
- **Provenance:** `-[:DERIVED_FROM {quote, span}]->(:Event)-[:AUTHORED_BY]->(:Person)`.
- **Between assertions:** `SUPERSEDES`, `CONTRADICTS` and `CORROBORATES`, all symmetric where they should be.
- **Evidence:** `(:Outcome)-[:SUPPORTS|REFUTES {conditions_met}]->(:ContextAssertion)`.
- **Gaps:** `(:KnowledgeGap {gap_id, question, status, asked_at, routed_to})-[:ABOUT]->(:Entity)`, then `-[:ANSWERED_BY]->(:ContextAssertion)`.

## 6. Validation (fixed)

v1 relied on a human tap alone. v2 validates at three points, and most of it involves no person.

### 6.1 Capture-time gates (inside the extraction graph; each gate drops or flags, never blocks the channel)

| Gate | Check | When it fails |
|---|---|---|
| V1 Grounding | Every extracted field has a verbatim quote span from a source event | The field is dropped. An assertion without an action quote is dropped. |
| V2 Speaker | Rules come from an answer, not a question or guess. "So another SFP?" is a refuted hypothesis and becomes part of `avoid`, not a rule. | Not stored as knowledge |
| V3 Entity at time | Each bound entity exists at `valid_from` (R9) | The binding is stored as `candidate` and one clarifying question is asked (plan 002) |
| V4 Network cross-check | Symptoms claimed in the thread are compared with telemetry and alarms on the bound entity in the **last complete PM interval that ended at or before the message time**. Sample timestamps mark the interval start, so a bin that is still open may already contain post-fix traffic. For example, "counters basically zero" at 06:21:30Z is checked against the 06:00–06:15 bin of TRN-042 1/1/3, which is zero, and not the 06:15 bin, which is partial because the bounce happened at 06:23. | Agreement adds one `SUPPORTS` from the network. Disagreement is flagged on the Slack card ("telemetry disagrees"). |
| V5 Temporal invariants | R2–R7 plus the `verify_temporal` rules, as Cypher in the **same transaction** as the write | Rollback, and the item is parked as `extraction_failed` |
| V6 Reconcile | The same knowledge from Slack and email becomes `CORROBORATES`, not a duplicate. Same topic and same entity with a newer explicit change becomes `SUPERSEDES`. Disagreement becomes `CONTRADICTS` and the status becomes `disputed`. | — |

### 6.2 Outcome validation (the network confirms the knowledge)

1. When the agent's recommended action runs, a person reports it or the simulator executes it. Either way an `AgentAction` or `Outcome` event is written (MCP `report_outcome`).
2. **The verifier job** evaluates the assertion's expected effect on telemetry in `[t_action, t_action + 30 min]`:
   - counters recover → `resolved`;
   - counters stay at zero → `not_resolved`;
   - no data → `unknown`.
   It also records whether the assertion's **conditions were met** before the action (`check_conditions`).
3. **Effects:**
   - `resolved` adds `SUPPORTS` and adds the entity to `observed_on`. `unverified` becomes `believed_true` after one network-confirmed success, or after the expert's ✅.
   - `not_resolved` with conditions met adds `REFUTES`. `refute_n = 1` sets `review_due` and DMs the author one question. `refute_n ≥ 2` on in-scope entities sets `disputed` and opens a gap.
   - `not_resolved` with conditions not met is **not** counted against the rule, because the rule didn't apply.
   - `confidence` = the mean of Beta(1 + support_n, 1 + refute_n), weighted by source authority. This is shown, never hidden.
4. **Scope learning:** a success on an entity outside `applies_to` (for example a TNC-2X) creates an `agent_proposal` that widens the scope. It is `unverified`, linked `CONTRADICTS` to the narrower scope, and confirmed by a person.

### 6.3 Lifecycle validation (cross-layer at t)

- A firmware change, line-card swap or model change in the as-running layer on a device inside an assertion's scope sets `review_due = true` **for that device only**. The agent says "unverified since firmware v6.2 on TRN-005". Validity is unchanged (R5).
- Decommissioning a device closes assertions that are bound **only** to that device (R4c). Model-level assertions stay.

## 7. Agents writing to the context layer

Every write goes through MCP (plan 002) into `ContextRepository`, the single write path.

| Write | Who | What it becomes |
|---|---|---|
| `report_outcome` | Any agent, or the Slack bot on behalf of a person | An `Outcome` event, then evidence (§6.2) |
| `open_knowledge_gap` | Any agent | A `KnowledgeGap`, routed in Slack to the most frequent author on that entity or topic |
| `propose_assertion` | Any agent | `origin=agent_proposal`, `status=unverified`. It **must** cite at least one existing event. The Slack card asks a person. |
| `flag_assertion` | Any agent | `review_due`, routed to the author |
| Confirm, retract, edit validity | **People only**, via Slack | Version events (R6) |

Every write carries `agent_id` (from the MCP token), an optional `on_behalf_of` (Slack user), `trigger_event_id` and an `idempotency_key`. Every write is itself an event (R1).

## 8. Workstreams

Repo layout:

```
backend/src/kaiku/
  main.py config.py logging.py
  domain/        # pydantic models, time rules (R1–R10) as pure functions
  graph/         # driver, ContextRepository, cypher/, migrations/, checks.cypher
  ingest/        # slack_events, email (eml/msg/imap), backfill (teams json, pdf, kb), threads, trigger
  extraction/    # LangGraph: triage → extract → link → validate (V1–V6) → reconcile → persist
  validation/    # outcome verifier, lifecycle watcher, confidence
  mcp/           # MCP server (plan 002)
  slack/         # Bolt app (plan 002)
  agents/        # reference ops agent (MCP client only)
  sim/           # network simulator for the live demo
  telemetry/     # DuckDB
  eval/          # plan 003
  llm/
docker-compose.yml   # neo4j, api (FastAPI + MCP), slackbot, sim
```

### WS0 — Scaffold
uv, Python 3.12, ruff, mypy, pytest, testcontainers. Lifespan: Neo4j driver, migrations, checkpointer. Settings add `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CAPTURE_CHANNELS`, `MCP_TOKENS` (agent_id→token) and `DEMO_PROFILE`. `/healthz` and `/readyz` work as in v1.

### WS1 — Context store
- Migrations, constraints and the §5 shape.
- `ContextRepository`: `assert_`, `correct` (R6ii), `supersede` (R4b), `retract`, `add_evidence`, `valid_at(entity_ids, t, known_at=None)` (R9/R10), `history`.
- Every write runs the V5 checks in its transaction.
- Seed: network layers plus `--gold`, marked `origin=gold` (eval only, never in the demo profile).
- Spike (≤1 h): the vector `SEARCH` syntax.

### WS2 — Ingestion
- `slack_events`: Bolt `message` events in opted-in channels become `Message` events with `event_time` = `ts` (R1). Edits and deletes become new events (R6).
- `email`: a `.eml` drop folder, IMAP polling (optional) and the `.msg` backfill. `event_time` = the `Date` header.
- `backfill`: Teams JSON, PDF and Confluence/ITSM with their original times. The demo profile excludes the 2025 TRN-042 thread (§3.5).
- `threads`: by Slack `thread_ts`, Teams `conversation_id`, or email `In-Reply-To`.
- `trigger`: extract on a resolution signal (🎉/✅/👍 reaction, "fixed"/"traffic is back", or the FAST_MODEL classifier) or after N minutes idle.
- Idempotent on `(channel, event_id)`, and robust to Slack retry headers.
- **Proactive capture loop (the agent reads the channels on its own; plain code, the LLM only judges relevance):**
  - *Watermark:* each opted-in channel has a `ChannelCursor.last_seen_ts`, advanced after every stored message. Socket Mode does not replay events missed while the bot was down, so on startup a **catch-up sweep** reads `conversations.history` / `conversations.replies` from the watermark and ingests anything missed (idempotency makes overlap harmless).
  - *Sweeper:* every 60 s (`CAPTURE_SWEEP_SECONDS`), threads with `last_message_ts > last_triaged_ts` that are resolved or idle ≥ N minutes go to triage. This is also where the idle trigger runs.
  - *Memory of decisions:* every triage result is written to the `Thread` (status, kind, one-line reason). A thread already triaged is not re-read unless a new message arrives after `last_triaged_ts`; then it is re-triaged (an `ignored` thread can become `captured`, and a `captured` one updates its card in place, 002 §3.2).
  - Email (`.eml` drop folder, IMAP in BE-2.5) uses the same watermark pattern: the last processed `Message-ID` / `Date`.
  - Not an LLM agent with Slack tools: a deterministic loop keeps ST-01 latency, ST-03 precision and ST-61/62 idempotency testable.

### WS3 — Extraction graph
- Nodes: `triage → extract → link → validate (V1–V4) → reconcile (V6) → persist (V5)`.
- `triage` (FAST_MODEL) uses a telecom-operations rubric and returns `{relevant, kind: rule|access|correction|none, reason}`, written to the `Thread` (WS2):
  - **Keep:** symptom → action rules (the TRN-042 case); "avoid X" warnings; model, generation or firmware quirks; site access (keys, codes, gates, barriers, contacts), kept even when it is about one site only; workarounds; advice that corrects, contradicts or retires earlier advice ("actually…", "not any more", "since the upgrade").
  - **Ignore:** greetings and small talk; scheduling and logistics; status pings that teach nothing ("on my way", "ticket opened"); live incident updates with no reusable rule.
  - **Unanswered questions** are not knowledge, but are kept as gap candidates (`kind=none`, reason says so) for the ops agent's `open_knowledge_gap`.
- Time resolution (R3/R4) is a deterministic function: the LLM returns the quoted phrase ("from 1 December"), and code resolves it with `zoneinfo("Europe/Helsinki")` against `event_time`.
- Linking uses inventory attributes. `entity_aliases.csv` is for scoring only.
- No HITL `interrupt()` here. Confirmation is asynchronous in Slack (plan 002), so extraction never waits for a person.

### WS4 — Validation and lifecycle
- The outcome verifier (§6.2) is a scheduled job plus a trigger on `Outcome` events.
- The lifecycle watcher (§6.3) acts on `DeviceEvent`.
- Confidence calculation.

### WS5 — Reference ops agent
- A `create_agent` agent whose **only** tools come from `langchain-mcp-adapters` connected to our MCP server.
- Output `OpsAnswer {status: answered|not_documented|disputed|partial, steps[], avoid[], conditions[{name, met|not_met|unknown}], citations[], gaps[], review_notes[]}`.
- Hard rules:
  - no step without a citation;
  - no step whose `check_conditions` is `not_met`;
  - `unknown` conditions are stated as unknown;
  - disputed knowledge shows both sides.
- A proactive mode reacts to fault events from the simulator. The dispatch mode attaches site access (plan 002).

### WS6 — Network simulator (live demo)
- `sim` holds a small as-running state for the demo nodes (TRN-017 TNC-1X Pori, TRN-019 TNC-1X Lappeenranta, TRN-010 TNC-2X Lahti) and writes telemetry and alarms at **now** (R1).
- `POST /sim/fault {node, port, kind: sfp_swap_stall}` emits MODULE_INSERTED, LINK_UP and OPTICAL_POWER_OK, then zero counters.
- `POST /sim/action {node, port, action: bounce_interface}` recovers counters after about 30 s, unless `outcome=fail` is configured (used to demo refutation).
- DuckDB reads history from files and live data from the sim table through one view.

## 9. Sequencing

| When | Backend | Unblocks |
|---|---|---|
| Fri evening | WS0, WS1 (schema, repository, time rules R1–R10 as unit tests), data fixes (§3.4), vector spike | Everything |
| Sat morning | WS2 Slack + email + backfill. WS6 sim. MCP read tools (002). | Live capture and recall on seeded data |
| Sat afternoon | WS3 extraction + V1–V6. WS5 agent over MCP. MCP write tools. | Full loop |
| Sat evening | WS4 outcome and lifecycle validation. Eval (003). **Feature freeze 22:00.** | Slides |
| Sun 06:00–10:00 | Fixes, backup video, run-book, submit | — |

## 10. Risks and open questions

| Risk | Mitigation |
|---|---|
| Slack Socket Mode or the workspace fails during the review | Backup video. The replay script also runs against a local fake Slack (recorded event payloads) for the eval. |
| LLM latency in the live demo (extraction >60 s) | FAST_MODEL for triage. Extraction is async, and the card appears when ready. Cached runs are kept for the video. |
| Featherless `dimensions` or availability | A startup probe, MRL truncation, and graph-only retrieval as fallback (v1). |
| Neo4j `SEARCH` syntax | Spike, with `queryNodes` as fallback |
| Relative-date resolution errors (R3) | Deterministic resolver plus held-out tests H-02, H-03 and H-08 |
| Agents abusing write tools | Writes are evidence or proposals only (§7), with token per agent, idempotency and rate limit |
| The gold set is self-authored | Held-out set plus the caveat (003) |

## 11. Out of scope

- Real device actions (simulated only).
- Microsoft Teams live connector. The pitch says Teams works through the same `ingest/` interface (Graph change notifications); the backfill already reads Teams exports.
- Voice and calls.
- Multi-tenancy.
- Cloud deployment.
