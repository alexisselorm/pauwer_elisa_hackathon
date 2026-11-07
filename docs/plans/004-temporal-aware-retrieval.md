# 004 — Retrieval at time t: filter first, rank second, never hide a successor

Status: **v2, 2026-11-07.** v1 (same day) adopted the whole of an external article's four-layer
"temporal-aware retrieval" design. Rechecked against `Elisa_SinceAI2026/` (the only source of
truth), most of it did not serve Elisa's criteria, so v2 keeps only the parts that do (§4).
Plan 001's time model (R1–R10) is unchanged. This plan adds R11 and R12.
**v3, 2026-11-07:** a review of the shipped R12 found four more gaps (P5–P8), fixed in §2–§3.

Companion plans: [`001`](001-backend-setup.md) (time model, graph), [`002`](002-slack-and-mcp.md)
(MCP tools), [`003`](003-acceptance-and-eval.md) (tests ST-70…ST-73 added here).

## 1. What Elisa asks for

- Brief §04: *"Each context assertion is bound to a graph entity and carries valid_from / valid_to
  timestamps (valid_to defaults to 'still believed true'), so all layers can be cross-referenced
  for any point in time."*
- Diagram: *"each context / metadata assertion has superseded timestamp and expiry (defaults to
  infinity i.e. 'we still believe it's true')"* and *"For any given time, [we] can cross-reference
  all layers"* at one instant, t=x.
- Brief §06, secondary criteria: *"relevance of the captured context, correctness of linking
  (right entity, right time validity)"*.
- Brief §06, strong demo step 3: *"When a similar case occurs, an AI agent retrieves the
  knowledge and proposes the right next step"*.

Validity in Elisa's model is binary: an assertion is in force at t or it is not. Retrieval must
therefore (a) return exactly what is valid at t for the right entity, and (b) rank it by how much
to trust it. It must never rank a valid fact down for its age.

## 2. Problems found in the current code

| # | Where | Problem | Criterion it hurts |
|---|---|---|---|
| P1 | `context/service.py:156-157` (`search_context` with no `entity_id`) | It takes the top `limit×3` by approximate vector search, then filters by validity at t. If many similar versions exist (an access chain, a superseded rule), an assertion that *was* valid at a past `as_of` can fall outside the candidate set and never be returned. | Right time validity |
| P2 | `context/service.py:155,161` | Semantic mode **overwrites** the structural score (match × status × confidence, `repository.py:193`) with raw cosine. A `disputed` or `review_due` assertion then ranks level with a `believed_true` one. | Relevance |
| P4 | `repository.py` `STATUS_WEIGHT` (found while testing ST-70) | `superseded` and `expired` weighed 0.3, but a status is today's state. Everything ranked has already passed the validity filter at t, so at a past `as_of` the superseded padlock *is* the fact in force, and the penalty pushed it below unrelated assertions. This affected `get_context` too. | Right time validity |
| P5 | `search_context` with no `entity_id` | R12.1 says candidates come from the graph when entities are resolved from the query, but the query was never resolved: "TRN-003 zero traffic after an SFP swap" ranked the Gen1 rule (TNC-1X only) like any other, with no match weight and no qualifier filter. | Right entity |
| P6 | `rank_assertions` | Exact cosine kept only the 100 closest candidates, and the structural score was applied after that cut. Past 100 valid assertions, a direct, believed one just outside it was lost. | Relevance |
| P7 | semantic search | An assertion without an embedding (captured less than a minute ago, before the backfill) was left out of semantic search. | Relevance |
| P8 | keyword fallback | Scoped keyword search returned anything containing any term as a substring, unranked; the global one ranked by term count only, ignoring status and confidence. | Relevance |
| P3 | ops agent / tool output | A superseded assertion (returned by `get_history`, or by an `as_of` in the past) can reach an answer without its successor being named. Elisa's "superseded timestamp" is in the data but not visible in the answer. | Right time validity, suitability for agents |

## 3. Rules

**R11: a superseded assertion is never shown alone.** Every view of an assertion with
`status=superseded` carries `superseded_by` (it already does, `repository.py:100`). The ops agent
names the successor and the date it took over in the same answer. This is Elisa's "superseded
timestamp" made visible.

**R12: filter by time first, then rank.**
1. **Candidates come from the graph whenever an entity is known.** That means `entity_id`, or
   entities resolved from the query. They are the assertions valid at t (R9, with R10 `known_at`),
   with scope expanding node → model → generation → site → segment. This is Elisa's
   assertion → BINDING → entity.
2. **With no entity_id**, the query is resolved (`resolve`, confidence ≥ 0.8). Each resolved
   entity's scope comes in as `get_context` sees it, with its match weight and qualifier
   filter. All other assertions valid at t are ranked alongside with the unscoped weight 0.7,
   so a misread query hides nothing, except what `get_context` would itself leave out for a
   named device: knowledge bound to its scope but filtered out, and knowledge whose models,
   generation or firmware match none of the named devices (P5). `resolved_entities` in the
   result lists them. Validity is applied **before** any vector ranking, never after.
3. **Ranking:** `score = max(0, cosine(q, a)) × structural(a)` when there is query text, otherwise
   `structural(a)`. `structural` is the existing `repository.score()`.
   - Cosine is computed exactly over **all** candidates (`similarities`), not by approximate
     top-k search, and nothing is cut before the structural score is applied (P6).
   - A candidate with no embedding yet is embedded on the spot. If the provider fails, it is
     ranked by keyword overlap and the result's `note` says so (P7).
   - Without embeddings at all, similarity is keyword overlap (share of query terms found
     as words of the statement or topic), and the same formula applies (P8).
   - Neo4j's cosine is already scaled to [0, 1]; the clamp guards any other source.
   - `superseded` and `expired` weigh 1.0 in `structural`. Candidates are valid at t, so
     those statuses only describe what happened after t (P4).
4. **Ranking never hides anything** (002 §4.1). No age decay: a valid assertion is "still believed
   true" (R5, Elisa's default).

Cost: at our size (24 gold assertions, about 100 extracted) exact cosine over the valid set is
cheap. At Elisa's scale the same rule maps to a filtered vector index query, with the validity
predicate pushed into the index filter. Our pitch says so.

## 4. What we considered and did not adopt

We reviewed Talukdar, *"Temporal-Aware Retrieval: Beyond RAG and GraphRAG"* (2026-08-31). It is a
design proposal whose results are labelled "Projected", so we don't quote its numbers. We kept the
ideas behind R11 ("retrieving the superseded fact alone is actively misleading") and the
filter-before-rank rule ("Hard filter when you have an explicit temporal constraint"). We did not
adopt the rest, because each part conflicts with Elisa's model or doesn't move a criterion:

| Not adopted | Why |
|---|---|
| Age decay `exp(−λ·age)` | Elisa's assertions are "still believed true" until replaced or expired. If usefulness ends, the Elisa-native answer is a `valid_to`, not a lower score. The per-channel λ values are guesses we can't validate on synthetic data. |
| A `temporal_type` field (point / interval / evolving / decaying) | Redundant with Elisa's model: an interval is a stated `valid_to`, an evolving fact has none and is superseded, and a point fact is the event stream ("record time = event time"). |
| Reading the time from the question ("in March 2025", session carry-over) | Elisa's agents act on alarms, so t comes from the event. A misread date would silently answer at the wrong time. Callers pass `as_of` explicitly. |
| Window queries | Elisa's model cross-references at one instant, t=x. |
| The article's vocabulary (TAR, its mode names) | We use Elisa's: as-experienced context layer, binding, valid_from / valid_to, cross-reference at t. |

These stay in the Trello **Stretch** list in case the judges ask for them.

## 5. Work

| Card | Scope |
|---|---|
| **TAR-5** Retrieval at t (R12) | Fix P1, P2 and P4 in `search_context` and `get_context` ranking; ST-70, ST-71, ST-72 |
| **TAR-3** Successor always shown (R11) | Fix P3 in the tool summaries and the ops-agent prompt; ST-73 |
| Retrieval gaps (v3) | Fix P5–P8 in `search_context`; ST-74 to ST-77 |

## 6. Tests (added to plan 003 §1.8)

| ID | Input | Expected |
|---|---|---|
| ST-70 | `search_context("Kemi gate access", as_of=2026-07-01)`, with many access-related assertions in the graph | The padlock is returned. It is not missed because of approximate top-k (P1). |
| ST-71 | `search_context("Kemi gate access")`, no `as_of` | The code lock. The padlock is not presented as current. |
| ST-72 | Disputed ASR-0020 (TRN-003 counter-report) given closer wording than Erkki's rule ASR-0001, in semantic search | ASR-0001 ranks above ASR-0020 (P2). |
| ST-73 | Any result containing a superseded assertion | It carries `superseded_by`, and the ops agent's answer names the successor and when it took over (R11). |
| ST-74 | `search_context("TRN-003 zero traffic after the SFP swap")`, all vectors equal | `resolved_entities = [TRN-003]`; the Gen1 rule ASR-0001 is absent; ASR-0020 is `direct`. For TRN-017 (Gen1), ASR-0001 ranks first as `model` (P5). |
| ST-75 | `similarities` over 120 embedded candidates | 120 scores, no top-100 cut (P6). |
| ST-76 | ASR-0021 with no embedding | Embedded during the search and returned; if the provider fails, returned with a keyword score and a `note` (P7). |
| ST-77 | Keyword mode | A believed rule ranks above a disputed report with the same terms; assertions sharing no term are left out (P8). |
