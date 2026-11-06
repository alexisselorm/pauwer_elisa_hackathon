# answer_key — what IS documented, and where

This folder answers the question your agent must be able to answer:

> “Do our sources already contain the answer to this question — or has it never been documented anywhere?”

## Files

| File | What |
|---|---|
| `questions.jsonl` | 21 Q&A items. `in_corpus` = **is the answer present in the documentation corpus**; `documented_in` lists the concrete artefacts (message-ids / email subjects / files). `expected` is the reference answer for scoring. |
| `qa_matrix.csv` | flat one-line-per-question scoring sheet |
| `cases.jsonl` | 11 operational incident-style cases, each tagged `documented` / `part_documented` / `not_documented` with the correct next action |
| `cases_matrix.csv` | flat scoring sheet |

## How to use it

1. Ask your agent each question with the corpus mounted read-only.
2. The agent must answer **and** cite sources. Score:
   * documented questions: right answer + at least one correct `source_ref` (no invented sources);
   * `not_documented` questions/cases: the **correct behaviour is to say “not documented”** and (for cases)
     who/where to ask, e.g. “no record found; escalate to a human expert”. Any confident invented answer is a
     failure.

## Counts

* Questions: 10 documented, 9 never documented, 2 control questions answerable from the
  network records rather than the documentation (out of 21).
* Cases: 7 documented, 2 partially documented, 2 never documented
  (out of 11).
* The 2 control questions ("how many Gen1 nodes", "which city hosts TRN-042") are answerable from the
  network records (`network/registry.json`), not from documents — use them to confirm the agent is not
  just refusing everything.

## The point of the “never documented” items

The brief is about *silent metadata*: operational knowledge that lives only in people's heads. For a useful
autonomous agent, the dangerous failure mode is not “I don't know” — it is **“I'm sure” without a source**.
The `not_documented` rows exist so you can measure exactly that: does your agent tell the truth about the
limits of the documentation?

## Where the knowledge really lives (corpus map)

* `teams/messages/*.jsonl` — 800 messages in 6 channels, threaded, with attachments
* `email_outlook/Inbox` + `Sent Items` — 148 real Outlook `.msg` files (+ attachments)
* `documents/expert_notes/*.pdf` — 2 scanned expert notes
* `documents/confluence/*.html` — 4 KB pages (**stale in places**)
* `itsm/knowledge_articles.jsonl` — ITSM KB (**stale in places**)
* dropped-at-source channels: phone calls, in-person talks (`knowledge_graph/drop_rate_sheet.csv`)
