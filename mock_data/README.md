# mock_data — the documentation corpus your agent searches

Synthetic operational documentation for the Elisa × Since AI 2026 "silent metadata" challenge
(`Elisa_SinceAI2026/…Challenge_Brief_FINAL.pdf`).

**The purpose of this dataset:** give your agent a realistic pile of the places operational knowledge
*actually* lives — Teams chats, emails, scanned notes, a stale knowledge base, tickets — and a ground-truth
`answer_key/` telling you, question by question, **whether the answer is already documented somewhere, and
where**, or **has never been written down anywhere** (so the right behaviour is "not documented — ask a
human", not an invented answer).

Everything is synthetic and deterministic (seed `20261107`, standard-library Python only). The only
non-synthetic files are the two optional feeds under `external/` (real FMI weather and Nord Pool FI prices).

---

## 1. Start here

| You want to… | Open |
|---|---|
| know what counts as "documented" | **`answer_key/README.md`**, `answer_key/questions.jsonl`, `answer_key/cases.jsonl` |
| see the source corpus | `teams/`, `email_outlook/`, `documents/`, `itsm/` |
| score a build | `answer_key/qa_matrix.csv`, `answer_key/cases_matrix.csv` |
| regenerate everything | `python3 mock_data/generate.py` |
| verify the `.msg` email files are real | `python3 mock_data/scripts/verify_msg.py mock_data` |

### The headline use case (from the Elisa brief)

**TRN-042**, an old (2019, Gen1) transmission node in Kemi. On 2025-10-14 its SFP was replaced; the
interface came back **UP** with **normal optics** and **no alarms**, but **no traffic**. A senior expert
knows the fix — *bounce the port, don't replace the SFP or reboot* — and that knowledge exists **only** in:

* a Teams thread — `teams/messages/chan-noc-transport.jsonl` (also as HTML in `teams/exports/`),
* an email — `email_outlook/Sent Items/2025-10-14 1145 - RE TRN-042 traffic not recovering after SFP replacement.msg`
  (a real Outlook `.msg` file),
* a scanned personal note — `documents/expert_notes/2019-11_transmission_upgrade_notes.pdf`,

and **nowhere** in the knowledge base (the ITSM KB page on the same issue actually says "reboot the node").

The second brief case — the **locked barrier** on the private road to the Kemi transport room — starts
in Teams ("Pekka has the key"), is **superseded** by a 2026-08 email ("code lock, no key, ask security"),
and is **contradicted** by a stale Confluence page and a stale ITSM article.

---

## 2. `answer_key/` — documented vs. never documented

* `questions.jsonl` — 21 questions. Each has `status`:
  * `documented` (10) — `documented_in` lists the exact artefacts (message-id, email subject, PDF path);
  * `not_documented` (9) — the answer **does not exist anywhere** in the corpus; the correct agent
    behaviour is to say so and hand off to a human;
  * `answerable from network data` (2) — control questions answered by the network records, not documents.
* `cases.jsonl` — 11 operational cases (symptom → right next action) tagged
  `documented` (7) / `part_documented` (2, e.g. two experts disagree) / `not_documented` (2).
* `qa_matrix.csv`, `cases_matrix.csv` — flat scoring sheets.

**Scoring principle:** for `documented` items, a good answer names the right action **and** cites a real
source. For `not_documented` items, a confident answer with no source is a *failure*; the win is
"I found no record of this — recommend asking <team>".

---

## 3. What is in the corpus

### Teams — `teams/`
6 channels, ~800 messages: `NOC-Transport`, `Field-Ops-FI`, `SRE-Infra`, `Access-North`,
`Energy-Procurement`, `Service-Ops`. Per channel: `.jsonl` (raw messages with thread ids, authors, roles,
reactions, edits, attachments) and an `.html` "conversation export"; `channels.json` is the directory.
Inline evidence attachments (`attachments/current/teams/`, e.g. `TRN-042_1-1-3_port.txt`, a "screenshot").

The channels deliberately contain, besides the useful knowledge: unrelated chatter, the same fact stated
in two places, advice that later becomes outdated, and two experts who disagree.

### Email — `email_outlook/`
**148 real Outlook `.msg` files** (OLE2/CFB compound documents, hand-written by the generator without any
libraries) split into `Inbox/` and `Sent Items/`. Each has Subject, body, HTML body, sender, recipients,
dates, Message-ID, transport headers. Attachments are copied into `email_outlook/Attachments/` and
`attachments/current/email_outlook/`. `mail_export_index.json` lists them like an Exchange export.

### Documents — `documents/`
* `expert_notes/*.pdf` — 2 valid PDF 1.4 "scanned" expert notes (the Gen1 upgrade notes; the Ivalo site
  access drawing).
* `confluence/*.html` — 4 knowledge-base pages, several **stale** (MTU 9114, BESS "hold 80% SOC",
  "Kemi not in register", "replace the SFP again").

### Structured knowledge — `itsm/`, `jira/`, `vendors/`, `admin/`
* `itsm/tickets.jsonl` 621 tickets (incl. `INC00420001`, the TRN-042 incident),
  `itsm/knowledge_articles.jsonl` 5 KB articles (**4 conflict** with newer chat/email truth — each carries a
  `conflict_note`), `itsm/change_calendar.jsonl`.
* `jira/issues.jsonl` 262 issues, incl. `NETOPS-4821` "Document TRN-042 workaround" — still **open**
  (the knowledge was never actually written into a runbook) — and `IMPL-1088` (Kemi access register missing).
* `vendors/incidents.jsonl` 91 supplier cases, incl. the TRN-042 silent call-home.
* `admin/confluence_pages_index.json` — 41 pages indexed, only 4 with content (a completeness trap).

### Supporting context (so answers are grounded in something real)
* `network/registry.json` — the node inventory facts (e.g. TRN-042 = Kemi, Gen1, `TNC-1X`).
* `knowledge_graph/` — a **gold** extracted view used only for scoring: `assertions.jsonl` (29
  `valid_from`/`valid_to` assertions with source refs), `contradictions.json`, `access_register.jsonl`,
  `entity_aliases.csv` ("the old units" → `TNC-1X`, "TRE" → `FI-TRE-DC2`, …),
  `drop_rate_sheet.csv` (calls and hallway talks are **dropped at source** — not searchable at all).
  Treat this as the answer the pipeline should have produced, not as an input.
* The full network/telemetry/CMDB/ITSM-simulation layers are still generated (the brief's five time-aware
  layers, 90 MB in total) so a demo can show *why* a question comes up — e.g. the TRN-042 traffic chart
  (`telemetry/case_trn042_counters_hourly.csv`) or the alarm trace (`network/alarms/trn042_case.json`).
  If you only need documentation, just point the agent at `teams/ email_outlook/ documents/ itsm/ jira/
  vendors/`.

### `external/` — real open data (optional)
* `external/fmi/observations_15min.jsonl` — **real** FMI weather near each DC site, 15-min, full year
  (<https://opendata.fmi.fi/wfs>, no key; the API caps a query at 7 days, so the fetcher chunks).
* `external/nordpool/fi_price_15min.jsonl` — **real** Nord Pool FI prices via Elering
  (<https://dashboard.elering.ee/api/nps/price>, no key).
* Fingrid needs a free API key (`FINGRID_API_KEY`); not included.

---

## 4. Documented, outdated, contradictory and missing — the four cases to test

| Kind | Example | Where |
|---|---|---|
| **documented once** | DC2 power spikes are a metering artefact | Teams `SRE-Infra`, 2025-11-18 |
| **documented in 2 places** | TRN-042 port bounce | Teams + email (+ PDF) |
| **outdated** | "holding 80% SOC", "MTU 9114", "Pekka has the key", "DLQ: raise memory to 4Gi" | Teams/Confluence/ITSM vs. newer email/Teams |
| **contradictory** | power-cycle a KMAX-4K: yes / never | `chan-access-north` 2026-06-02 |
| **never documented** | fixed firmware version for the forwarding bug; out-of-hours access procedure; cabling repair after rodent damage; RF channel plan; weekend vendor escalation | nothing anywhere → `answer_key` |

---

## 5. Regenerating & checking

```bash
python3 mock_data/generate.py                  # ~18 s, 90 MB, deterministic
python3 mock_data/generate.py --with-external  # also re-fetches FMI + Nord Pool (~2 min)
python3 mock_data/scripts/verify_msg.py mock_data   # must print "148 files, 148 valid, 0 broken"
```

Requirements: Python 3.10+, standard library only. The fetchers need outbound HTTPS and skip gracefully
offline (`external/` is optional; the documentation corpus never depends on it).

---

## 6. Provenance

Synthetic throughout, generating scripts in `generators/`. Motifs from the Elisa brief (TRN-042 SFP case,
locked barrier) are reproduced in spirit with invented names and node IDs, as the brief explicitly allows.
`external/fmi` is FMI open data; `external/nordpool` is from Elering's public API.
