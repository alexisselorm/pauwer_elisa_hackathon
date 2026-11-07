# 002 — Surfaces: Slack app (people) and MCP server (agents)

Status: **Revised 2026-11-06 (v2). This replaces the React + shadcn frontend plan.** The experts are already in Slack, so the brief's primary criterion ("minimal additional work… from the channels they already use") is met where they already work. Agents, Elisa's or ours, use the MCP server.

Depends on [`001-backend-setup.md`](001-backend-setup.md) (time rules R1–R10, validation §6, write governance §7). Tests are in [`003-acceptance-and-eval.md`](003-acceptance-and-eval.md).

## 1. Goal

1. **Capture without work.** Experts talk in Slack (or email) as usual. Knowledge is captured from that conversation. The expert is never *required* to do anything.
2. **Recall where the work happens.** When a fault appears, the ops agent posts the next step in the incident thread, citing the expert's own message, before anyone replaces hardware.
3. **Any agent can use it.** Elisa's agents connect to our MCP server and get the same context, can report outcomes, and can raise gaps.

**Success test:** a judge watches Elisa's chat being typed in Slack, sees one quiet "captured" card with no forms, then sees the agent, a few minutes later, give the right next step on another node with a link back to that message. Then the same answer is retrieved from a *different* MCP client (Claude Desktop or MCP Inspector).

## 2. Decisions

| Decision | Reason |
|---|---|
| **Slack Bolt for Python 1.30.0 (MIT) in Socket Mode** (pypi.org/pypi/slack-bolt, 2026-11-06) | No public URL or tunnel is needed for a laptop demo. The Python stack is shared with the backend. |
| **Official MCP Python SDK `mcp` 1.30.0 (MIT)** with Streamable HTTP (stateless), served by the FastAPI app at `/mcp`, plus stdio for local clients. Not 2.x: `langchain-mcp-adapters` 0.3.2, the latest, requires `mcp<2`, and our ops agent shares the venv (checked on pypi.org, 2026-11-06). | It's the reference SDK, and the wire protocol doesn't depend on the SDK version, so any MCP client works. One process serves REST, MCP and the graph driver. |
| **`langchain-mcp-adapters` 0.3.2 (MIT)** for our ops agent (pypi.org/pypi/langchain-mcp-adapters, 2026-11-06) | Our agent sees only MCP tools, the same as Elisa's agent would. |
| **Bearer token per agent** (`MCP_TOKENS` maps agent_id to token) | Every write is attributable (001 §7). OAuth is post-hackathon. |
| **No web UI.** Graph visuals use Neo4j Browser with saved Cypher. Time travel uses the Slack App Home date picker. | Less to build. The judges see the product where the users are. |
| Usability budget (enforced in code) | At most 1 bot message per captured thread. At most 1 clarifying question per thread. 0 required actions. Experts are never DMed except to route a gap or flag aimed at them. |

## 3. Slack app

### 3.1 Manifest (checked into `slack/manifest.yaml`)

- Socket Mode on.
- Bot scopes: `channels:history`, `groups:history`, `im:history`, `chat:write`, `chat:write.customize` (only for the demo replay script that posts persona messages), `reactions:read`, `users:read`, `app_mentions:read`, `commands`.
- Events: `message.channels`, `message.groups`, `message.im`, `reaction_added`, `app_mention`, `app_home_opened`.
- Shortcut: message shortcut "Capture this as knowledge", so people can opt in manually as well as passively.

### 3.2 Capture flow (people do nothing)

1. Messages in opted-in channels (`SLACK_CAPTURE_CHANNELS`, e.g. `#noc-transport`, `#field-ops`, `#access-north`) are stored as `Message` events. `event_time` = Slack `ts` (R1).
2. The trigger fires on a resolution signal ("Traffic is back 👍", ✅/🎉 reaction, "fixed") or after idle time. A 60 s sweeper does the idle check, so the bot reads the channels on its own (001 WS2 "Proactive capture loop").
3. The bot remembers what it has read: a per-channel watermark (with a catch-up sweep after any restart, since Socket Mode does not replay missed events) and a per-thread decision (`captured` / `ignored` / `extraction_failed`, with a one-line reason). An ignored thread is re-read only when a new reply arrives. Ignore decisions are never posted to work channels; they are visible in `#context-log` and the App Home, so "why didn't it capture this?" always has an answer.
4. Triage keeps or ignores with a telecom-operations rubric (001 WS3); extraction runs in the background only on kept threads.
5. If an assertion was captured, the bot posts **one** threaded reply, a Block Kit "context card":

```
📌 Captured for TNC-1X (Gen1) · seen on TRN-042 · after SFP replacement
If interface UP + optics normal + counters at 0 → disable/enable the interface.
Avoid: replacing the SFP again, rebooting the unit.
Valid from 14:22 today · source: Erkki (this thread) · telemetry agrees ✓
[✅ Correct]  [✏️ Fix]  [🚫 Not knowledge]
```

6. Buttons are optional. With no response, the assertion stays `unverified` and is still usable, with that label. ✏️ opens a modal prefilled with scope and validity. Saving it is a **correction** (R6ii), not a supersession. 🚫 retracts it.
7. **Clarifying question**, only when a binding is a `candidate` (V3) or a date is ambiguous: one question in the thread, with buttons. Example: "Does this apply to all TNC-1X units, or only TRN-042?" [All TNC-1X] [Only TRN-042]. The answer is captured as an event.
8. Thread replies after the card that change the knowledge ("actually it was 1/1/4") are re-extracted, and the card is **updated in place** (`chat.update`), never re-posted.

### 3.3 Email

- A `.eml` drop folder and optional IMAP polling of a shared mailbox (e.g. `noc-knowledge@`, which experts CC as they do today). The `.msg` backfill is also supported.
- Email captures are **not** announced in work channels. They appear in `#context-log`, a quiet audit channel. If the email corroborates a Slack assertion, the original Slack card gains "+1 source (email)".

### 3.4 Ops agent in Slack (a reference agent that uses MCP only)

- **Asked:** `@ops` mention or a DM, e.g. "@ops TRN-017 1/1/2 up, optics ok, zero traffic after SFP swap".
- **Proactive:** a fault event from the simulator (or a real alarm feed) posts in `#noc-alarms` with a thread containing:
  - **Conditions:** oper UP ✓, RX −4.2 dBm ✓, counters 0 for 41 min ✓ (from `check_conditions`).
  - **Next step:** disable/enable 1/1/2. **Avoid:** SFP swap, reboot.
  - **Why:** Erkki in #noc-transport (permalink), Erkki's email, the 2019 notes PDF. Support 1 ✓ / refute 0.
  - Buttons: [Run bounce (simulated)] [Done manually ✓] [Didn't work ✗]. Each click calls `report_outcome` through MCP, so validation needs only one optional click, or none if the agent ran the action.
- **Dispatch:** when the answer involves a site visit, the dispatch message includes access information valid at the **planned visit time** (R9), for example "Kemi: code lock since 2026-08-03, code from site security (Heikki Lehtonen)".
- **Not documented:** "I found no record of this. Asking @Erkki in #noc-transport" [Ask] [Don't ask]. The expert's reply in that thread *is* the capture, and the gap closes (`ANSWERED_BY`).
- **Disputed:** both views with authors and dates, and the lower-risk step flagged as disputed.
- Citations: Slack sources use `chat.getPermalink`; backfilled sources show the file path and quote.

### 3.5 App Home (read-only explorer)

- Pick an entity and an `as_of` date (Block Kit datepicker + timepicker), plus an optional `known_at`.
- It lists the assertions valid at that time, with status, validity window, sources, support/refute counts and the supersession chain.
- This replaces the React timeline.

### 3.6 Demo replay script

`slack/replay.py --thread elisa --speed 10x` posts Elisa's 12 messages verbatim into `#noc-transport` using personas (`chat:write.customize`). Event times are the real Slack `ts`. For a more natural demo, two teammates can type the messages instead. Author resolution supports both real users (profile name and title) and personas (`username` override).

## 4. MCP server

Streamable HTTP at `http://localhost:8000/mcp`, plus stdio (`silentctx mcp --stdio`). Every tool returns `structuredContent` (JSON) plus a short text summary. Times are ISO-8601 UTC. `as_of` defaults to now and `known_at` defaults to now.

### 4.1 Read tools

| Tool | Returns |
|---|---|
| `resolve_entities(text, as_of?)` | Candidate entities with confidence ("the old Kemi unit" → TRN-042) |
| `get_entity(entity_id, as_of?)` | As-built + as-running snapshot at t: model, generation, site, segment, firmware, status |
| `get_context(entity_id, as_of?, known_at?, topic?, symptoms?, statuses?)` | Assertions in scope at t (R9), node → model → generation → site → segment, ranked. Each has validity, status, confidence, support/refute, `review_due`, qualifiers (symptoms/action/avoid) and citations. |
| `search_context(query, as_of?, entity_id?)` | Semantic search, still filtered by validity at t |
| `check_conditions(assertion_id, entity_id, port?, at?)` | Each symptom: `met`/`not_met`/`unknown`, with the telemetry values |
| `get_site_access(entity_or_site, as_of?, known_at?)` | Access assertions valid at the visit time |
| `get_events(entity_id, from, to, types?)` | Alarms, device events and outcomes (event stream) |
| `get_history(assertion_id)` | Versions, supersession, corrections and evidence over time |

### 4.2 Write tools (governed by 001 §7)

| Tool | Effect |
|---|---|
| `report_outcome(assertion_id?, entity_id, port?, action, result: resolved\|not_resolved\|unknown, observed_at, evidence_ref?, idempotency_key)` | An `Outcome` event, then outcome validation (001 §6.2) |
| `open_knowledge_gap(question, entity_ids, context?, idempotency_key)` | A gap routed to an expert in Slack |
| `propose_assertion(statement, entity_ids, qualifiers?, valid_from?, valid_to?, evidence_event_ids[≥1], rationale, idempotency_key)` | An `unverified` agent proposal, plus a Slack confirmation card in the relevant channel |
| `flag_assertion(assertion_id, reason, evidence_event_ids, idempotency_key)` | `review_due`, routed to the author |

**Deliberately not exposed:** confirm, retract, delete, or set validity. A write with an unknown entity, or a proposal without evidence, gets a tool error. The same idempotency key yields the same result. Each call records `agent_id` from the token.

### 4.3 Prompt and resource

- Prompt `ops_triage`: a short system prompt that tells any client agent the rules: call `get_context` and `check_conditions` before recommending; never recommend without a citation; say "not documented" and call `open_knowledge_gap` when nothing is found; call `report_outcome` after any action.
- Resource `context://entity/{id}` holds the current context as a Markdown summary, for clients that prefer resources.

### 4.4 Connecting Elisa's agents (README snippet)

- **Remote MCP clients:** URL `http://<host>:8000/mcp`, header `Authorization: Bearer <token>`.
- **Claude Desktop / Claude Code:** add the server to the MCP config. **LangGraph:** `MultiServerMCPClient({"silentctx": {"url": …, "transport": "streamable_http", "headers": …}})`.

## 5. Workstreams

### WS-S0 — Slack workspace and app
Manifest, tokens, and channels `#noc-transport`, `#field-ops`, `#access-north`, `#noc-alarms`, `#context-log`. A fake-Slack test harness replays recorded event payloads into the Bolt handlers (used by 003).

### WS-S1 — Capture handlers and the context card
Message, reaction and shortcut handlers. Card rendering, button actions (confirm, correct modal, retract), the clarifying question and in-place updates.

### WS-S2 — Ops agent bot
Mention and DM handling, proactive fault threads, outcome buttons, the dispatch access block, gap routing and the disputed layout.

### WS-S3 — App Home explorer
Entity picker, `as_of`/`known_at` pickers and the assertion list.

### WS-M1 — MCP read tools
Plus contract tests: tool schemas are snapshotted, and MCP Inspector can call every tool.

### WS-M2 — MCP write tools
Auth, idempotency, governance errors and the Slack side effects (proposal and flag cards).

### WS-S4 — Demo script
Replay script, sim fault and action buttons, saved Neo4j Browser queries, a run-book and the backup video.

## 6. Demo script (about 4 minutes)

1. **Capture.** Elisa's chat is replayed live in `#noc-transport`. After "Traffic is back 👍" the card appears in the thread: valid from now, bound to TRN-042 / TNC-1X Gen1 / SFP replacement, with "telemetry agrees ✓". No one clicks anything. Then forward Erkki's email to the mailbox, and the card shows "+1 source (email)".
2. **Linking.** Show the graph in Neo4j Browser (assertion → TRN-042, TNC-1X, Gen1, `Activity:sfp_replacement`; source message; author).
3. **Recall.** Inject a fault on TRN-017 (Pori, TNC-1X). The agent posts in `#noc-alarms` with conditions ✓✓✓, the next step bounce, avoid SFP swap and reboot, and a permalink to Erkki's message. Click [Run bounce (simulated)]. Counters recover, and the card shows "network-confirmed on TRN-017 · support 1".
4. **Access.** "@ops dispatch to Kemi on 2026-08-10": the reply gives the code lock. In App Home, with `known_at = 2026-08-10`, it shows the padlock, which explains why a dispatch on that date failed.
5. **Outdated or conflicting advice.** The stale ITSM KB says "reboot the node". The agent shows it as contradicted by newer expert knowledge. The KMAX-4K experts disagree, so the answer is marked disputed.
6. **Honesty.** "@ops which firmware fixes the forwarding bug?" gets "Not documented, asking @Erkki". Erkki replies, the gap closes, and that reply is the capture.
7. **Any agent.** In Claude Desktop (or MCP Inspector) on the same server, ask "What do we know about TRN-019 before I reboot it?". It returns the same context through `get_context`.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Workspace admin approval or rate limits | Use our own free workspace. Rate limits don't matter at demo volume. Retries are idempotent. |
| `chat:write.customize` personas look staged | Prefer two teammates typing live, and keep the script as backup |
| The card feels intrusive | Usability budget (§2). Per-channel opt-in. `/silentctx quiet` mutes the cards but keeps capturing. |
| MCP client compatibility | Test with MCP Inspector, Claude Desktop and `langchain-mcp-adapters` before the freeze |

## 8. Out of scope
- OAuth for MCP.
- Slack Enterprise Grid.
- A Microsoft Teams bot. The pitch says the capture interface is channel-agnostic, and Teams exports are already backfilled.
- A web UI.
