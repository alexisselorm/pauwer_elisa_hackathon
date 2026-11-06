# Held-out eval set

**Provenance.** Written by hand (by the team, with AI help) on 2026-11-06. It is **not** produced by
`mock_data/generate.py` and shares no text with it. Do not use it while writing or tuning extraction or agent
prompts. If a prompt is changed because a held-out item failed, move that item into the regression set and
write a new one here.

**Why it exists.** The `mock_data/` corpus and its answer key were written by the same team that builds the
pipeline, so scoring on them is a regression test, not evidence. This set uses different wording, devices and
time patterns, so it tests whether capture, linking and validity generalise.

| File | Content |
|---|---|
| `threads.jsonl` | 9 short Slack threads and 1 email, timestamps in UTC to the second (the platform's event time) |
| `expected.jsonl` | Per thread: the assertions that must (or must not) be created, with exact `valid_from` / `valid_to` / `recorded_at`, entity bindings and relations |
| `questions.jsonl` | 12 agent questions with an `as_of` time: documented, disputed, expired, not-yet-valid and not-documented |

**What it covers.**

| Concern | Threads / questions |
|---|---|
| Valid time = source event time (record time = event time) | H-01, H-03, H-06 |
| Validity announced in advance (recorded before valid) | H-02, HQ-02/03 |
| Explicit expiry, then expired | H-03, HQ-04/05 |
| Relative date + Europe/Helsinki timezone (EET after 2026-10-25) | H-08, HQ-08/09 |
| Supersession by the same author | H-07, HQ-07 |
| Two experts disagree, device-only scope | H-06, HQ-06 |
| Scope widening from email (not Gen1 only) | H-09 |
| Corroboration and in-thread outcome, no duplicate | H-01, HQ-01 |
| Chatter and unanswered guesses produce nothing | H-04, H-05 |
| Not documented | HQ-10..12 |

**Loading.** Replay `threads.jsonl` through the same connector path as live Slack/email (the eval harness
posts them as Slack-shaped events with the given `ts`). Score with `silentctx eval --set heldout`
(see `docs/plans/003-acceptance-and-eval.md`).
