# 002 — Frontend setup: React + shadcn/ui operations console

Status: **Drafted 2026-11-06, `main`. Open: graph-view library final pick after the WS3 spike; depends on the SSE contract frozen in [001 WS5](001-backend-setup.md#ws5--api-surface-and-sse-contract).**

## 1. Goal

A web console that shows the judges the whole loop in under three minutes:

1. Knowledge is **captured** from a Teams or email thread, with no extra work for the specialist.
2. It is **linked** to network entities, with a visible validity period.
3. An **agent recalls it** on a new fault and proposes the right next step, with citations, or says "not documented".

**Who it's for:**
- Elisa's judges, watching a live demo on a team laptop (1080p, sometimes a projector).
- In the story the demo tells: NOC specialists and field engineers.

**Constraints:**
- Submission is due **2026-11-08 10:00**.
- The stack is React + shadcn/ui.
- The backend is FastAPI with SSE (plan 001).
- The demo runs locally with docker compose.
- The UI must work when the LLM is offline (seeded data).

**Success test:** someone who has never seen the app can follow the demo script (§6 WS7) from a cold start without touching dev tools. Every recommendation on screen links to its source quote.

## 2. Decisions

| Decision | Reason | Settled |
|---|---|---|
| React + shadcn/ui | Team choice | User, 2026-11-06 |
| **Vite 8 + React 19 + TypeScript (strict)** as an SPA. No SSR framework. | A local demo needs no SEO or server rendering. Vite is shadcn's documented `-t vite` template (ui.shadcn.com/docs/installation/vite, 2026-11-06). | This plan |
| **Tailwind v4** via `@tailwindcss/vite`; shadcn `new-york` style, `tw-animate-css`, OKLCH tokens; **sonner** for toasts | shadcn's Tailwind v4 defaults. The old toast component is deprecated (ui.shadcn.com/docs/tailwind-v4, 2026-11-06). | This plan |
| **TanStack Query 5** for all server state. No global client store. | Server data is cached and invalidated in one place. Local UI state stays in components. | This plan |
| **TanStack Router** (file-based, typed search params) | Time travel (`as_of`, `known_at`) and filters live in the URL, so the time state can be validated, linked and replayed during the demo. Note: we did not research which router is "recommended" in 2026; this choice rests on typed search params alone. | This plan |
| **Hey API** (`@hey-api/openapi-ts`) client generated from FastAPI's OpenAPI | FastAPI's own docs recommend it (fastapi.tiangolo.com/advanced/generate-clients/, 2026-11-06). Types can't drift from the backend. | This plan |
| A **custom `useRunStream` hook** over `fetch` + `ReadableStream` parses our SSE contract. **Not** `useStream`, **not** AI Elements. | `useStream` against a custom backend relies on an undocumented wire format. AI Elements "will be deprecated at the end of November 2026" (elements.ai-sdk.dev, verified 2026-11-06). `EventSource` can't POST. | This plan |
| Chat and agent UI built from shadcn primitives (Card, ScrollArea, Badge, Collapsible, Alert, Tooltip) | No dependency on a component set being deprecated. Full control of the citation rendering. | This plan |

## 3. Current state (verified)

No frontend exists. The UI consumes:

| Fact | Where |
|---|---|
| Elisa's Teams thread (12 messages, roles, times) is the "capture" scene input | `Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026/teams_chat.json:65` |
| The resolution message ("Traffic is back 👍") is the visual trigger moment | `Elisa_SinceAI2026/ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026/teams_chat.json:72` |
| Assertion fields the UI renders: `valid_from` / `valid_to` | `mock_data/knowledge_graph/assertion_schema.json:31` |
| `status` enum (believed_true, superseded, disputed, expired, unverified), which maps to badge variants | `mock_data/knowledge_graph/assertion_schema.json:67` |
| `recorded_at` (transaction time), shown next to valid time | `mock_data/knowledge_graph/assertion_schema.json:76` |
| Kemi access chain: padlock until 2026-08-03, code lock afterwards, recorded 2026-08-18. This is the time-slider demo. | `mock_data/knowledge_graph/access_register.jsonl:1` |
| TRN-042 rule (ASR-0001) with symptom/action/avoid qualifiers, which drive the recommendation card | `mock_data/knowledge_graph/assertions.jsonl:1` |
| `not_documented` question QA-034, the honesty demo | `mock_data/answer_key/questions.jsonl:17` |
| API routes and the SSE event contract (`run.started` … `run.completed`) | `docs/plans/001-backend-setup.md` (WS5) |

## 4. How the industry does it

These are tooling findings from live registries and documentation, accessed 2026-11-06. A competitor survey doesn't apply to a project set-up plan.

| Tool | Version, licence | Bearing |
|---|---|---|
| Vite | 8.3.6, MIT | Build/dev server (npm registry) |
| React | 19.3.1, MIT | npm registry |
| shadcn CLI | 4.23.0, MIT | `pnpm dlx shadcn@latest init -t vite` (ui.shadcn.com/docs/installation/vite) |
| Tailwind CSS | 4.3.x, MIT | v4 changes: `@import "tailwindcss"`, `@theme inline`, `tw-animate-css`, `data-slot`, no `forwardRef` (ui.shadcn.com/docs/tailwind-v4) |
| TanStack Query | 5.106.1, MIT | npm registry |
| TanStack Router | 1.170.46, MIT | npm registry |
| @hey-api/openapi-ts | 0.99.0, MIT | fastapi.tiangolo.com/advanced/generate-clients/ |
| @xyflow/react (React Flow) | 12.12.1, MIT | Entity-neighbourhood graph. Laid-out, readable, good for small graphs. |
| react-force-graph-2d | 1.29.2, MIT | Alternative for the "whole graph" overview |
| Vitest / Testing Library / Playwright | 5.1.0 / 16.3.3 / 1.65.0 | Unit, component and e2e testing |
| AI Elements (Vercel) | ai 7.0.163, Apache-2.0 | **Avoid:** being deprecated in November 2026 and moving into shadcn/ui |

**Verdict:** this is the standard 2026 React SPA stack, and nothing here differentiates the product. The differentiating UI is the **validity timeline** and the **citation-first agent answer**, so WS3 and WS4 get the most care.

## 5. Principles

1. **The server owns the data.** Every fetch goes through generated Hey API calls wrapped in TanStack Query hooks in `src/api/queries/`. Components never call `fetch` directly.
2. **The URL owns view state.** `as_of`, `known_at`, entity, filters and the selected run live in typed search params. Reloading or sharing a link reproduces the screen.
3. **No claim without a source.** A recommendation, step or assertion renders with its citation chip (author, channel, time, quote on hover). If the citation is missing, the UI shows an `Unsourced` warning badge instead of hiding the problem (see §7).
4. **Generated shadcn components stay vendored.** `src/components/ui/*` is shadcn output and is only updated through the CLI. Product components compose those primitives and live in feature folders.
5. **The UI says when it is unsure.** Streaming, empty, error, `unverified`, `disputed` and `unknown` telemetry each have a designed state. Nothing renders blank.
6. **Accessible by default.** Radix-based shadcn primitives, keyboard-reachable timeline and graph controls, `prefers-reduced-motion` respected, and contrast checked in both themes.

## 6. Workstreams

Layout produced by WS0. All paths are new.

```
frontend/
  package.json  pnpm-lock.yaml  vite.config.ts  tsconfig*.json  components.json  openapi-ts.config.ts  Dockerfile
  src/
    main.tsx  router.tsx  routeTree.gen.ts
    routes/            # file-based: __root, index (demo home), capture, entities.$id, runs.$id, gaps, conflicts, eval
    api/
      client/          # generated by Hey API — do not edit
      queries/         # queryOptions factories + hooks per resource
      sse/             # parseSse.ts, useRunStream.ts, events.ts (contract types)
    features/
      capture/         # thread replay, captured-knowledge card, confirm card
      context/         # entity header, assertion list, validity timeline, time controls
      graph/           # neighbourhood graph view
      agent/           # incident form, run transcript, tool-call items, answer card, approval dialog
      gaps/  conflicts/  eval/
    components/
      ui/              # shadcn (generated)
      common/          # CitationChip, StatusBadge, TimeRange, EmptyState, ErrorState
    lib/               # time.ts (UTC <-> Europe/Helsinki), cn.ts, env.ts (zod-validated)
    test/              # msw handlers, sse fixtures shared with backend contract
  e2e/                 # Playwright demo script
```

### WS0 — Scaffold, tooling and dev loop

**Why first:** every feature depends on a typed client and a working proxy to the API.

**Changes:**
- **Scaffold:** `pnpm create vite frontend -- --template react-ts`, then `shadcn init -t vite`. Base components:
  - `button card badge tabs table dialog sheet tooltip popover command scroll-area separator skeleton alert collapsible slider calendar sonner`
- **TypeScript and lint:**
  - `strict`, `noUncheckedIndexedAccess`, `@/*` path alias in tsconfig and vite.
  - ESLint (typescript-eslint, react-hooks, jsx-a11y) and Prettier with the Tailwind class-sorting plugin.
- **Routing:** TanStack Router with its Vite plugin (file-based routes, code-split per route).
- **Server state:** TanStack Query with `QueryClientProvider` and devtools in dev only.
- **Typed client:**
  - `openapi-ts.config.ts` points at `http://localhost:8000/openapi.json`. The `pnpm gen:api` script writes `src/api/client`.
  - CI fails if the generated client differs from the committed one.
- **Vite dev proxy:** `/api` → `http://api:8000` in compose. Turn off compression and buffering for `text/event-stream`, and verify that tokens arrive incrementally (plan 001 WS5 off-nominal).
- **Env:** `src/lib/env.ts` parses `import.meta.env` with zod (`VITE_API_BASE`). A missing value fails at startup.
- **Theme:** shadcn light and dark tokens, with dark as the default for the projector. Fonts are self-hosted.
- **Docker:** a `web` service in the root `docker-compose.yml` (dev: `vite --host 0.0.0.0`). The production build is served by a static nginx stage with `proxy_buffering off` for `/api/runs`.
- **CI:** typecheck, lint, Vitest, build, and Playwright smoke against compose.

**Tests:**
- The app boots.
- The 404 route renders.
- The env validation error renders a readable screen.

**Off-nominal:**
- API unreachable: a global banner from a `/readyz` query (refetch every 10 s). Pages keep showing cached data.

### WS1 — API layer and the run-stream hook

**Why:** every screen consumes it. Building it against fixtures lets the UI move before the backend is ready.

**Changes:**
- **Query factories** in `api/queries/`:
  - `entityQueries.detail(id, {asOf})`, `.neighbourhood(id, {asOf})`, `.assertions(id, {asOf, knownAt, status})`
  - `assertionQueries.detail/history`, `gapQueries.list`, `conflictQueries.list`, `evalQueries.latest`
  - Query keys include the time parameters, so time travel is cached for each time.
- **Mutations:** `confirmAssertion`, `retractAssertion`, `replaySource`, `resumeRun`. Each invalidates the affected entity and assertion keys. Use optimistic updates only for confirm (👍).
- **SSE parsing:**
  - `api/sse/parseSse.ts` is a spec-compliant parser for `event:`/`data:`/`id:` lines and multi-line data.
  - `api/sse/events.ts` holds discriminated-union types that mirror 001 WS5 (`run.started | step | token | tool.call | tool.result | assertion.draft | assertion.saved | interrupt | answer | run.completed | run.failed`).
- **`useRunStream({kind, input})`** returns:
  - `{status, events, transcript, pendingInterrupt, answer, error, start, resume, cancel}`
  - It uses an `AbortController` and reconnects with `Last-Event-ID` after a network drop.
  - Events are reduced into a stable view model with `useReducer`, so components never parse raw events.
- **Test fixtures:** recorded `.sse` files in `src/test/fixtures/`, validated against `backend/tests/contract/sse_schema.json` (plan 001) in CI.

**Tests:**
- `parseSse` handles chunk boundaries mid-line, CRLF, and comments/keep-alive pings.
- The reducer, given a full fixture, produces the expected transcript.
- Reconnect resumes from the last `seq` without duplicating tokens.
- An MSW-backed hook test covers the interrupt then resume path.

**Off-nominal:**
- An unknown event type is logged once in dev and ignored (forward-compatible).
- `run.failed` renders the problem+json detail in an `Alert` with a retry button.
- The user navigates away: the request is aborted, and the run keeps going server-side and can be reopened from `/runs/$id`.

### WS2 — Capture scene ("zero extra work")

**Why first among the features:** usability is the primary judging criterion, and this screen tells that story.

**Changes:**
- **`routes/capture.tsx`:** a two-pane layout.
  - **Left:** a Teams-style thread replay, built from source messages through the API (`POST /sources/replay`). Controls: play, step, speed. A source picker offers Elisa's original thread, the TRN-042 corpus thread, the Kemi barrier threads and the KMAX conflict.
  - **Right:** a live extraction transcript from `useRunStream({kind: "extract"})`, with the steps triage → extract → link → reconcile → confirm.
- **Captured-knowledge card**, shown when `assertion.draft` arrives:
  - statement;
  - conditions (symptom chips) and actions as an ordered list, with `avoid` in a destructive-tinted list;
  - linked entities with link confidence;
  - `valid_from`, with source quote highlights;
  - **Teams-style confirm card** (👍 / ✏️ / ignore), which resumes the `interrupt`. Doing nothing leaves the assertion `unverified`, and the UI says so.
- **Relation banners** from `reconcile`:
  - "Corroborates the 2025-10-14 email";
  - "Supersedes: 'Pekka has the key' (valid until 2026-08-03)";
  - "Contradicts Juhani (2026-06-02)".

**Tests:**
- A component test of the card with fixture drafts for each relation type.
- A Playwright test: replay Elisa's thread, the card appears, 👍 is pressed, and the assertion is visible on the TRN-042 entity page.

**Off-nominal:**
- Extraction failed: the card shows "Could not extract — kept for review" with the error, and the thread is marked in the picker.
- No knowledge found (chatter): the card says "Nothing operational in this thread". The UI never shows a fabricated card.

### WS3 — Context explorer: entity, validity timeline, time travel

**Why:** this shows "correctness of linking (right entity, right time validity)", the second judging criterion.

**Changes:**
- **`routes/entities.$id.tsx`** with search params `{as_of?: ISO, known_at?: ISO, status?: Status[], view?: "timeline"|"graph"}`, validated with zod in the route's `validateSearch`.
- **Header:**
  - entity facts (model, generation, site, commissioned / decommissioned) and a breadcrumb `Site › Device › Model`;
  - "assertions valid now / total".
- **Time controls:**
  - A slider plus a date-time popover (shadcn `Calendar`) for **"As of"** (valid time).
  - A second, collapsed control for **"Known at"** (record time), with a one-line explanation. The Kemi preset jumps to 2026-08-10, so the padlock-vs-code-lock gap is a single click.
- **Validity timeline** (`features/context/ValidityTimeline.tsx`), rendered in SVG or CSS grid:
  - one row per assertion;
  - the bar spans `valid_from` to `valid_to` (an open end fades to the right);
  - a recorded-at tick mark;
  - colour comes from the status token;
  - `SUPERSEDES` arrows join consecutive bars;
  - a vertical "as of" line. This reproduces Elisa's reference-architecture figure for the actual data.
  - Each row is keyboard-focusable, and Enter opens the detail `Sheet`.
- **Assertion detail sheet:**
  - statement and qualifiers;
  - the citation list (quotes, author, role, channel, time);
  - version history (`/assertions/{id}/history`);
  - links to corroborating and contradicting assertions;
  - confirm and retract actions.
- **Graph view** (`features/graph/`): the entity neighbourhood at `as_of`, drawn with React Flow as device → model → site, with assertion nodes. Fall back to react-force-graph-2d only if layout quality needs it (half-day spike).

**Tests:**
- Timeline geometry is unit-tested (dates to x-positions, open-ended bars).
- Search-param validation rejects bad dates.
- Playwright on the Kemi segment:
  - `as_of=2026-08-10`, default known_at → code lock;
  - known_at = 2026-08-10 → padlock shown with the "not yet recorded" note.

**Off-nominal:**
- An entity with no assertions shows "No captured knowledge yet". When open gaps exist, it adds the gap list and the "who to ask" person.
- An unknown entity id gives a 404 route.
- A slow query shows skeleton rows, keeping the previous data visible (`placeholderData: keepPreviousData`).

### WS4 — Agent console (recall on a new fault)

**Why:** demo step 3 and the payoff of the pitch.

**Changes:**
- **`routes/runs.$id.tsx`**, plus an "Open incident" `Dialog` on the home page with presets:
  - "TRN-014 (TNC-1X): SFP swapped, link UP, counters zero";
  - "Dispatch to Kemi 2026-08-10";
  - "Night emergency change during sales freeze" (QA-034).
- **Run transcript:**
  - streamed reasoning tokens, collapsed by default;
  - **tool-call items** (name, arguments, result summary, duration) in `Collapsible`s;
  - the `check_telemetry` result renders a small inline sparkline (Recharts via the shadcn `chart` component) of `in_octets_mbps` with the "zero" threshold line.
- **Answer card** (`answer` event):

  | `status` | Renders |
  |---|---|
  | `answered` | Numbered steps, each with citation chips; an "Avoid" list (destructive styling); a confidence label (low/medium/high, never a percentage) |
  | `disputed` | The competing assertions side by side with authors and dates, plus "Escalate to <person>" |
  | `not_documented` | An `Alert`: "No record found in captured knowledge. Ask <person/team>." A link to the new `KnowledgeGap`. **No invented steps.** |

- **Approval dialog:** `interrupt{kind: approve_action}` opens an `AlertDialog` ("Bounce interface 1/1/3 on TRN-014?") with Approve or Reject, which resumes the run.

**Tests:**
- Each answer status renders from fixtures.
- Citation chips open the source sheet.
- The approval round-trip works under MSW.
- Playwright: the TRN-014 preset shows "bounce interface" with a citation to the TRN-042 thread, and the QA-034 preset shows the not-documented alert.

**Off-nominal:**
- The stream stalls for more than 20 s with no event: show "Still working…" with cancel.
- `unknown` telemetry conditions render as grey "not checked", never green.

### WS5 — Gaps and conflicts boards

**Why:** these show that the system knows what it doesn't know, which supports the "agentic operations" criterion. They are cheap once WS1 exists.

**Changes:**
- `routes/gaps.tsx`: a `Table` of `KnowledgeGap`s (question, entity, routed to, first asked, status). A gap closes live when an extraction answers it, via query invalidation on `assertion.saved`.
- `routes/conflicts.tsx`: disputed pairs with both statements, authors and recency. It also lists stale-KB supersessions (ITSM and Confluence pages superseded by chat or email).

**Tests:** table rendering, empty states, and the live close-on-save behaviour.

**Off-nominal:** empty boards say why ("No open gaps — every question asked so far had a sourced answer").

### WS6 — Eval page (slides companion)

**Why:** the numbers support the judges' "relevance / correctness" scoring.

**Changes:** `routes/eval.tsx` renders `GET /eval/latest`:
- extraction precision and recall;
- QA and cases, correct vs. "correctly not documented";
- the held-out set shown separately;
- a **fixed caveat line**: "Gold set authored by our team; held-out set hand-written separately."

**Tests:** renders from a fixture. The caveat is always present.

### WS7 — Demo script and polish

**Why last:** it only polishes what already works.

**Changes:**
- **Home route `/`:** three large cards matching the brief's demo steps (Capture → Link → Recall), each deep-linking with presets.
- **Presenter mode:** `?present=1` enlarges type and hides devtools-like detail.
- **Keyboard shortcuts** (via the `Command` palette): next demo step, toggle theme, reset demo (calls `/admin/reset-demo`, if the backend adds it).
- **`e2e/demo.spec.ts`** runs the full script and is the go/no-go check on Sunday morning.

**Tests:** the Playwright demo spec passes three runs in a row against a fresh `docker compose up`.

## 7. Honesty rails

The UI shows claims that will drive operations, so these rules are fixed:

- **Recommendations:** never render a step without at least one citation. If the backend sends one, render it with an `Unsourced` destructive badge and log it.
- **Confidence:** show the backend's `low|medium|high` label, never a percentage or a decimal.
- **Time:** always show *both* the validity range and the recorded-at time on assertion detail. Display Europe/Helsinki local time with a UTC tooltip.
- **Exact copy for missing knowledge:** "No record found in captured knowledge. Ask <name>, <role>." Never "I think" or "probably" without a source.
- **`unverified` assertions:** always show a "Not yet confirmed by an expert" badge.
- **Superseded or expired assertions:** shown greyed out with "Valid until <date>", never in the default "valid now" list.

## 8. Sequencing

| When | Frontend | Needs |
|---|---|---|
| Fri 2026-11-06 evening | WS0 scaffold; WS1 parser, hook and fixtures against the frozen SSE schema | 001 WS5 JSON Schemas committed |
| Sat 2026-11-07 morning | WS3 explorer on seeded data (001 WS1 `seed --gold`) | Neo4j seeded |
| Sat 2026-11-07 afternoon | WS4 agent console, WS2 capture scene | 001 WS3/WS4 SSE live |
| Sat 2026-11-07 evening | WS5, WS6, WS7; **feature freeze 22:00** | — |
| Sun 2026-11-08 06:00–10:00 | Demo spec green ×3, record the backup video, submit | — |

## 9. Risks and open questions

| Risk / question | Impact | Owner / resolution |
|---|---|---|
| The Vite proxy or nginx buffers SSE, so tokens arrive all at once | Demo looks frozen | WS0 check with a fixture endpoint; `proxy_buffering off`; FastAPI sets `X-Accel-Buffering: no` |
| SSE contract drift between the backend and the frontend | Silent UI breakage | Shared JSON Schema contract test in both CIs (001 WS5) |
| ~~TRN-042 timeline inconsistency~~ **Resolved 2026-11-06** (see 001 §8): the telemetry sparkline now matches the chat (zero 05:15–06:15 UTC, recovery 06:30) | — | Done |
| React Flow layout quality for dense neighbourhoods | Readability | Half-day spike in WS3; fallback force graph |
| Router choice not externally validated | Low; both routers are mature | Revisit only if typed search params cost time |
| AI Elements moving into shadcn/ui in November 2026 | Could provide ready-made chat components later | Re-evaluate after the hackathon |

## 10. Out of scope

- Authentication, user accounts and RBAC.
- A real Teams app or Adaptive Card. The confirm card is a look-alike inside our UI, and the pitch shows the Teams integration point.
- Mobile layouts below tablet width. The demo is desktop; layouts stay responsive but untested on phones.
- i18n. The UI is English, and the data has Finnish names only.
- Offline/PWA support.
