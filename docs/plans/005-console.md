# 005 — Console: live knowledge graph, capture traces, agent runs (UI-1)

Slack stays the place where people capture and ask, and MCP stays the only agent interface
(plan 002). This adds a **read-only web console** for the jury and stakeholders. It shows
three things that chat cannot: the graph over time, why each capture was kept and how it
was linked, and what agents read. It reverses plan 002's "no React frontend" decision for
the demo only. Nothing in the console writes.

## 1. Views

| View | Shows | Elisa criterion |
|---|---|---|
| Graph | Entities, assertions coloured by status, sources and links (supersedes, corroborates, contradicts). An as-of slider, with an optional "known then". Clicking an assertion shows its scope, validity, conditions → action / avoid, quotes, confidence and history. | Linking: right entity, right time |
| Captures | Per thread: the six pipeline steps with timings, the V1–V6 gates per rule, the messages with the extracted quotes highlighted, and what was stored | Correct capture, zero extra work |
| Agent runs | Every MCP call (tool, arguments, latency, result summary), grouped per agent | Suitability for agentic operations |
| Scorecard | The latest `silentctx eval` report | Correctness, with n |

## 2. Backend

- `silentctx/ui/`: read-only router at `/api/ui/*` with pydantic response models. Its Cypher
  lives in `ui/queries.py`. Agents never use these endpoints.
- `GET /api/ui/stream`: server-sent events (sse-starlette). The API diffs a small
  snapshot of the graph every `UI_POLL_SECONDS`. It uses ids, not `recorded_at`, which is
  message time (R2) and so cannot mark what is new.
- **Capture trace:** `Extractor.run` streams the LangGraph steps (`stream_mode="updates"`) and
  times each node. The resulting `CaptureResult` (steps, items, trace id) is saved on the
  `Thread` as `capture_json`. Captures made before this change show what the stored
  assertion kept.
- **Tool calls:** every MCP tool except `ping` is wrapped so that each call writes a
  `(:ToolCall)` node in the background. The node holds the agent, tool, arguments, ms, ok,
  and the one-line summary the agent read. ToolCall nodes are operational, like `Thread`,
  and are never read by temporal queries.
- **Names:** Slack user and channel ids are resolved with the bot token and cached.

## 3. Traces

Arize Phoenix runs in compose (`phoenix`, http://localhost:6006, volume `phoenix-data`).
`phoenix.otel.register(auto_instrument=True)` loads the OpenInference LangChain
instrumentor in the api and the slackbot, so every LLM call, LangGraph node and agent tool
call is a span. The capture span's trace id is stored with the capture, and the console
links to `PHOENIX_PUBLIC_URL/redirects/traces/<id>`.

## 4. Not done

- No auth on the console: like every service here, it binds to 127.0.0.1.
- Agent runs are grouped by time gap, not by MCP session, because the adapter opens a
  session per call.
