# Retrieval eval set

Measures `search_context` alone, before any agent reads the results, on the gold graph
(`mock_data/knowledge_graph/assertions.jsonl`). The QA sets (T0, T2) score the agent's final
answer, so a retrieval miss there only shows up as a wrong answer; this set shows which
step caused it.

**Provenance.** Written by the team against the gold assertions, so it is a regression test of
retrieval, not evidence that it generalises. The held-out set in `../heldout/` is still the
place for that.

Each line of `questions.jsonl`:

| Field | Meaning |
|---|---|
| `query`, `as_of`, `entity_id` (optional) | The `search_context` call |
| `expected` | Assertion ids relevant at `as_of`; all must be in the top k (default 5) |
| `forbidden` | Assertion ids that must not be returned at all: not valid at `as_of`, or, with `forbidden_why: "scope"`, valid but not applicable to the entity asked about |
| `tests` | What the item checks |

Run: `kaiku eval retrieval` (a throwaway Neo4j unless `--neo4j-uri` is given). With
`FEATHERLESS_API_KEY` set it ranks semantically; without it, by keyword overlap, and the
report says which. It reports recall@k, MRR and the forbidden check per item.
