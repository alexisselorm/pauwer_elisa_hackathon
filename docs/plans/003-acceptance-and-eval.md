# 003 — Success tests and eval

Status: **New 2026-11-06.** This replaces the v1 "success test" lines and v1 WS6 (eval). Rules R1–R10 and gates V1–V6 are in [`001`](001-backend-setup.md). The surfaces are in [`002`](002-slack-and-mcp.md).

## 1. Success tests

Every test is automated (pytest, against Neo4j in testcontainers, a fake Slack built from recorded event payloads, and the MCP server over HTTP) unless it is marked **live** or, as of 2026-11-07, **no automated test** / **in progress** below. "Criterion" refers to brief §06: **U** = usability (primary), **R** = relevance, **L** = linking (entity and time), **F** = feasibility, **A** = agentic suitability.

A CI gate now exists: `.github/workflows/ci.yml` runs ruff, mypy and `pytest -m "not live"` (the integration tests against a Neo4j testcontainer) for the backend, and typecheck, lint and vitest for the frontend, on every push and pull request. The pre-commit hook runs the unit tests only.

### 1.1 Capture with zero work (U, R)

| ID | Given | Expect |
|---|---|---|
| ST-01 | Elisa's 12 chat messages posted to `#noc-transport` (verbatim) | Exactly **1** assertion. **≤1** bot message in the thread. **0** required human actions. Card visible within 60 s of message 9. |
| ST-02 | Elisa's email, as `.eml`, after ST-01 | No new rule. `CORROBORATES` the ST-01 assertion. The card shows "+1 source (email)". Nothing is posted to work channels. |
| ST-03 | 200 labelled chatter messages from the corpus plus held-out H-04 | 0 assertions from chatter. Trigger precision ≥ 0.9. |
| ST-04 | Veikko's "So another SFP?" | Not stored as a rule. It appears as `avoid ⊇ {replace_sfp}` (V2). |
| ST-05 | An expert clicks ✏️ and narrows the scope to TRN-042 only | The old version gets `retracted_at` = click time. The new version **keeps** the original `valid_from` (R6ii). There is no `SUPERSEDES` edge. |
| ST-06 | A thread names "the Kemi box" while two devices exist at Kemi | One clarifying question with buttons. The answer binds the entity. No second question. |
| ST-07 | No response to the card for 24 h | Status stays `unverified`. The agent still uses it and labels it. |
| ST-08 | The bot is stopped, Elisa's thread is posted, then the bot starts | The catch-up sweep from the channel watermark ingests all 12 messages and captures the same single assertion as ST-01. No duplicates on a second restart. |
| ST-09 | A chatter thread is triaged `ignored`; later an expert replies in it with a fix | The thread is re-triaged once (new message after `last_triaged_ts`) and becomes `captured`. Threads with no new message are never re-triaged (0 extra LLM calls per sweep). |
| ST-09a | Corpus Kemi barrier thread (one site, no rule) | Triage keeps it as `kind=access`, not ignored as a one-off. Every triaged thread has a non-empty `triage_reason`. |

### 1.2 Linking (L)

| ID | Given | Expect |
|---|---|---|
| ST-10 | ST-01 | `observed_on` = TRN-042. `applies_to` ⊇ {TNC-1X, Gen1}. `activity` = `sfp_replacement`. Symptoms = {oper_up, rx_power_normal, counters_zero}. Action = `bounce_interface`. Avoid ⊇ {replace_sfp, reboot_node}. |
| ST-11 | Kemi barrier threads (corpus) | Bound to SEG-042 / site Kemi, not to a device |
| ST-12 | A message mentions TRN-999 (doesn't exist) | No orphan entity. The binding is flagged. HTTP 422 on a direct write. |
| ST-13 | Held-out H-06 (TRN-019 dispute) | Scope = TRN-019 only. The model-level rule is unchanged. **No automated test** (covered only by the T2 eval run, H-06). |
| ST-14 | Held-out H-09 (TNC-2X, by email), on the demo-profile graph (001 §3.5: no corpus Gen2 widening) | A new assertion `applies_to` TNC-2X, `unverified`. `CONTRADICTS` the Gen1-only scope. The Gen1 rule isn't rewritten. On the full backfill it `CORROBORATES` ASR-0006 instead. |
| ST-15 | The entity-link scorer over the regression and held-out sets | P and R ≥ 0.9 on `kind=entity` aliases |

### 1.3 Time validity (L): the Elisa model (001 §3)

| ID | Given | Expect |
|---|---|---|
| ST-20 | ST-01 | `recorded_at` = `valid_from` = Slack `ts` of message 8, **to the second** (R2, R3). `valid_to` = null. |
| ST-21 | Backfilled Elisa chat (no date in the source) | Message 8 → `2025-10-14T06:22:08Z` (EEST, R8; minute from Elisa, seconds synthetic). All 12 messages strictly increasing (R7). |
| ST-22 | Kemi code-lock email (sent 2026-08-18, "installed 3 August") | Code lock `valid_from` 2026-08-03, `recorded_at` 2026-08-18. Padlock `valid_to` = 2026-08-03, status `superseded` (R4b). |
| ST-23 | `get_site_access(Kemi)` at `as_of` 2026-07-01 / 2026-08-10, and at 2026-08-10 with `known_at` 2026-08-10 | Padlock (Pekka) / code lock / padlock + "updated 2026-08-18" (R9, R10) |
| ST-24 | H-02 (change announced in advance) | `recorded_at` < `valid_from` = 2026-11-30T22:00Z. On 2026-11-20 the answer is the padlock; on 2026-12-03 it is the code lock. |
| ST-25 | H-03 ("until 20 November") | `valid_to` set. `expired` after it. The HQ-05 answer doesn't mention the closure. |
| ST-26 | H-08 ("from Monday", sent Wed 2026-11-04) | `valid_from` 2026-11-08T22:00Z (Europe/Helsinki, EET). HQ-09 doesn't present it as current. |
| ST-27 | H-07 (same author corrects themselves) | `SUPERSEDES`. Predecessor `valid_to` = successor `valid_from`. |
| ST-28 | Stale ITSM KB "reboot the node" vs the expert rule | `CONTRADICTS`. The agent ranks the newer expert knowledge first and mentions the KB as outdated. |
| ST-29 | After every ingest and every eval run | 0 violations from `checks.cypher` (port of `verify_temporal.py` plus R2/R7) |
| ST-29b | Device decommissioned / firmware upgraded in the as-running layer | Device-only assertions expire (R4c). Model-level ones get `review_due` for that device only. Validity is unchanged (R5). **In progress** (lifecycle watcher, `validation/lifecycle.py`). |

### 1.4 Agent recall and continuing the workflow (A, R)

| ID | Given | Expect |
|---|---|---|
| ST-30 | Sim fault on TRN-017 1/1/2 (TNC-1X), knowledge from ST-01 only | A proactive post in `#noc-alarms`: conditions met ×3, step = check counters → bounce, avoid = SFP swap and reboot, citation = permalink to message 8. It arrives **before** any hardware action. |
| ST-31 | The same symptom, but the counters are normal | The rule is not applied (`not_met`). No bounce is recommended. |
| ST-32 | No telemetry for the node | Conditions `unknown`, stated explicitly. Not treated as met. |
| ST-33 | Dispatch to Kemi planned for a given date | Access block valid **at the visit time**. **No automated test** (`get_site_access` at `as_of` is tested; the agent's dispatch answer is not). |
| ST-34 | CASE-009 (Kemi at 02:00) | `partial`: the code is known, but the out-of-hours procedure is not documented. A gap is opened. **No automated test.** |
| ST-35 | All `not_documented` questions (regression + HQ-10…12) | `not_documented`, no steps, gap routed. **0** confident answers. |
| ST-36 | Control questions QA-001/002 | Answered from network data, not refused |
| ST-37 | A gap is routed in Slack and the expert replies in the thread | An assertion is captured from the reply. Gap `ANSWERED_BY` that assertion. No extra form. |
| ST-38 | H-06 disputed (HQ-06) | Both views, authors and dates. Status `disputed`. Gap opened. **No automated test** (the T2 eval scores HQ-06 `dispute_shown`). |

### 1.5 Validation (R, A): 001 §6

| ID | Given | Expect |
|---|---|---|
| ST-40 | ST-01: "counters basically zero" at 06:21:30Z; the last complete bin (06:00–06:15) of TRN-042 1/1/3 is zero (backfill) / sim (live) | V4 adds `SUPPORTS` from the network. The card shows "telemetry agrees ✓". Using the open 06:15 bin (partial, 1.01 Mbps) would wrongly report a disagreement, so this is tested. |
| ST-41 | Bounce on TRN-017 succeeds (sim) | An `Outcome` `resolved`, then `SUPPORTS`. `observed_on` += TRN-017. `unverified` → `believed_true` without any human click. |
| ST-42 | Bounce on TRN-019 fails (sim `outcome=fail`), conditions met | `REFUTES`, `review_due`, one DM to Erkki (tested). A second failure → `disputed` + gap: **no automated test**. |
| ST-43 | Bounce fails but conditions were `not_met` | Not counted against the rule |
| ST-44 | Bounce succeeds on TRN-010 (TNC-2X, outside scope), demo-profile graph | An `agent_proposal` widening the scope (`unverified`). ~~Slack card for a person.~~ (shows in the console; no card.) **In progress** (scope widening in the outcome verifier). |
| ST-45 | Extraction output without a quote for its action | Dropped (V1) |

### 1.6 MCP and agents writing (A, F)

| ID | Given | Expect |
|---|---|---|
| ST-50 | MCP Inspector and Claude Desktop connect to `/mcp` | All tools are listed. `get_context(TRN-017)` returns the same assertions the Slack agent used. **(live)** |
| ST-51 | `propose_assertion` with evidence | `unverified`, `origin=agent_proposal`, card in Slack. It can't become `believed_true` through MCP. |
| ST-52 | `propose_assertion` without evidence, or with an unknown entity | Tool error. Nothing is written. |
| ST-53 | The same `idempotency_key` twice | One event |
| ST-54 | Missing or invalid token | Rejected. Every successful write has `agent_id`. |
| ST-55 | A foreign agent runs ST-30 using only MCP + the `ops_triage` prompt | Same step, avoid list and citations as our agent (scored like §2.3). **No automated test** (done by hand with MCP Inspector, run-book step 9). |

### 1.7 Robustness (F)

| ID | Given | Expect |
|---|---|---|
| ST-60 | LLM unavailable | Capture is queued and retried (tested: `test_llm_retry.py`). The agent answers in "degraded" mode from graph-only context, says so, and never fabricates: **in progress** (degraded-mode wording; today the mention answers "Sorry, I couldn't answer that right now" and the fault thread posts a warning). |
| ST-61 | Slack redelivers an event (retry header) | No duplicate event or assertion |
| ST-62 | Re-running the backfill | No-op (idempotent) |
| ST-63 | `docker compose up` on a clean machine and the run-book | Demo §6 of plan 002 runs end to end **(live)** |

### 1.8 Temporal-aware retrieval (L, A): plan 004

ST-70…ST-78 are specified in [plan 004 §6](004-temporal-aware-retrieval.md#6-tests-added-to-plan-003-18): a past `as_of` isn't missed, current isn't confused with superseded, disputed ranks below believed, the successor is always named, entities named in a query scope the search, no candidate is cut before ranking or left out for lack of an embedding, the keyword fallback is ranked, and the retrieval eval set passes.

## 2. Eval (fixed)

### 2.1 What was wrong with v1

1. **Circular.** The pipeline, the corpus and the answer key are all ours, and v1 scored only on them.
2. **A ±15 min validity tolerance hid real time bugs**, the very bugs found in 001 §3.4 (whole-slot drift).
3. **Gold could leak.** Seed mode loads gold assertions into Neo4j, and v1 didn't say the QA eval must run without them.
4. **The primary criterion (usability) wasn't measured at all.**
5. **One run per item.** LLM variance wasn't reported.

### 2.2 Sets (tiers)

| Tier | Content | Role | Bar |
|---|---|---|---|
| **T0 Elisa** | Elisa's original chat + email (the only external data) | Must pass. Shown first on the slide. | 100% on ST-01/02/10/20/21/30 |
| **T1 Regression** | `mock_data/answer_key` + gold assertions (self-authored) | Catches regressions; not evidence of quality | Tracked |
| **T2 Held-out** | `eval/heldout/` (9 threads, 12 questions, hand-written, never used for prompt tuning) | The generalisation number for the slide | Reported with n |
| **T3 Live** | A recorded Slack run of the demo script | Proves it works end to end | Pass/fail |

### 2.3 Metrics (deterministic: no LLM judge)

| Criterion | Metric | How it's computed |
|---|---|---|
| **U** | Required human actions per captured assertion (target 0) · bot messages per thread (≤1) · clarifying-question rate · capture latency p50/p95 from the resolution message to the card | Event log of the fake Slack |
| R | Assertion precision/recall vs gold, matched on (subject or applies_to, topic, action) · chatter trigger precision | `eval extract` |
| L — entity | P/R of `observed_on` and `applies_to` (aliases table for scoring only) | `eval extract` |
| L — time | **Exact** `valid_from`/`recorded_at` for event-derived times (to the second); day-exact for times stated in text; `valid_to` exact; status at snapshot; supersession edges correct; as-of probe accuracy (ST-23…26, HQ-02…09) | `eval time` |
| A | Action accuracy (enum match) · avoid-list honoured · condition-check honesty (no `unknown` treated as met) · citation validity (the cited event exists **and** contains the quoted span) · **false-confident rate** on `not_documented` (target 0) · gap routing | `eval qa` |
| A — validation | Correct status transitions for ST-40…44 | `eval validation` |

### 2.4 Protocol (as shipped, 2026-11-07)

- **One graph condition for QA: extracted-only.** The graph is built by our pipeline from raw sources on a throwaway Neo4j, with gold never loaded. The planned gold-seeded upper bound was not built; the retrieval eval (§2.5) is the only run on the gold graph.
- **One run** per item. The report says so in its caveat and pins the model ids; no mean or min–max is reported. Three runs were planned and not done, for time.
- **Through the real interfaces.** The QA eval calls the MCP server, not internal functions. Capture goes through the same ingest path as live Slack and email.
- **No key, no run.** `kaiku eval run` needs the Featherless key; without it the retrieval eval ranks by keyword and says so, and `kaiku eval time` runs the temporal checks without an LLM.
- **CI gate:** the automated ST tests (unit and integration) must pass in CI (`.github/workflows/ci.yml`). The eval runs are not gated; they are recorded.
- **Output:** `eval/results/<start>.json` plus a Markdown table, both committed, shown by the console's Scorecard. The run also saves the captures and the agents' answers, so `kaiku eval rescore` can score a saved run again with a newer scorer in seconds. The slide shows T0 and T2 with n and the caveat: "T2 is hand-written and held out, n = 9 threads / 12 questions; one run, so no variance is reported."
- **T1 regression** (the answer key on the full backfill) was not wired into `kaiku eval`; it is covered by the retrieval set and the integration tests.

### 2.5 CLI (as shipped)

```
kaiku eval run --set elisa|all        # T0, or T0 + T2 held-out; extracted graph only, one run
kaiku eval rescore eval/results/<start>.json   # score a saved run again, no Neo4j, no LLM
kaiku eval time                       # no LLM needed for the R-rule checks on stored data
kaiku eval retrieval                  # search_context alone on the gold graph: recall@k, MRR, nothing invalid at as_of
```

Planned and not built: `--set regression|heldout` on their own, `--graph extracted|gold|both`, `--runs 3`.
