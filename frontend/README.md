# kaiku console

A read-only web view of the context layer. People capture and ask in Slack, and agents use
MCP. The console shows what happened: the live knowledge graph over time, how each thread
was captured, and every MCP call an agent made. Plan: [`docs/plans/005-console.md`](../docs/plans/005-console.md).

| Stack | Why |
|---|---|
| Vite + React 19 + TypeScript | standard SPA toolchain |
| TanStack Query | server state, cache, refetch on live events |
| React Router | `/`, `/captures/:threadId`, `/agents/:runId`, `/scorecard` |
| Cytoscape.js + fcose | the graph: incremental adds, seeded layout, styling by data |
| openapi-fetch + openapi-typescript | typed API client generated from FastAPI's OpenAPI |
| lucide-react | icons |

The styling uses Elisa Design System tokens, copied from the public Storybook
(`storybook.elisa.fi`): Jost for titles, Open Sans for body, brand blue `#2323ff`, the EDS
neutral, success, danger and warning scales, 4/8/16 px radii, and light and dark themes
(`src/styles/tokens.css`).

## Run

```bash
npm install
API_URL=http://localhost:8000 npm run dev     # http://localhost:5173, /api proxied to the API
npm run build && npm run lint
API_URL=http://localhost:8000 npm run gen:api # regenerate src/api/schema.d.ts after an API change
```

In compose it runs as `web`, an nginx image serving the build and proxying `/api` to the API:
http://localhost:8080.

## How it works

- **Graph** (`src/graph`): `GET /api/ui/graph` returns every assertion with `valid_from`,
  `valid_to`, `recorded_at` and status, plus the entities each one is bound to and its
  sources. The browser filters by time (`lib/time.ts` `stateAt`), so the as-of slider is
  instant. "Only what was known then" also sets `known_at` to the same instant.
- **Live:** `/api/ui/stream` (server-sent events) sends `change` when assertions, threads
  or tool calls change. `live.tsx` invalidates the affected queries. New nodes are placed
  next to their neighbours while the existing nodes stay locked, and they pulse.
- **Captures:** the stored `CaptureResult` of each thread holds the step timings, the
  V1–V6 gates per rule and the verbatim quotes. The quotes are highlighted in the messages.
- **Agent runs:** MCP tool calls recorded by the server, grouped per agent. A new run
  starts after 90 s without a call.
- **Reset** (header): `POST /api/ui/reset` deletes everything captured or recorded since
  seeding and seeds the network and gold knowledge again. It's the console's only write.
- **Traces:** "Trace ↗" opens the capture's trace in Arize Phoenix (`PHOENIX_PUBLIC_URL`).
