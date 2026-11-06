"""Answer key: questions about the network/operations, and whether the answer IS documented.

Two evaluation tracks:

  Q&A  — for each question: is the answer present in the documentation corpus? If yes,
         where (source_refs to actual artefacts in teams/, email_outlook/, documents/,
         itsm/)? If no, it is a *documentation gap* — the correct agent behaviour is to
         say "not documented" instead of inventing an answer.
  CASES — incident-style operational cases (symptom -> right next action). Each one is
          classified:
            documented        — the right action is written down; an agent should find it
            part_documented   — some pieces are there, but not the decision rule
            not_documented    — never written anywhere; a human expert knows it
          Also includes stale / contradictory sources as traps.

Output: mock_data/answer_key/README.md, questions.jsonl, qa_matrix.csv, cases.jsonl
"""
from __future__ import annotations

import json
import os

import common as c


def _first_message(channel: str, needle: str) -> dict:
    path = os.path.join(c.root_dir(), "teams", "messages", f"{channel}.jsonl")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = json.loads(line)
            if needle in m["text"]:
                return m
    raise KeyError(needle)


def main() -> None:
    reg = json.load(open(os.path.join(c.root_dir(), "network", "registry.json"), encoding="utf-8"))
    trn = reg["trn_meta"]
    gen1 = sum(1 for m in trn.values() if m["generation"] == "Gen1")
    qa = [
        # ---- answerable FROM THE DATA (registry/network records), tested here as a control set
        dict(id="QA-001", question="How many transport nodes use the Gen1 hardware generation?",
             answerable_from="network/registry.json", in_corpus=False, documented_in=[], from_network_data=True,
             expected="count == %d (derived from trn_meta)" % gen1, kind="metric"),
        dict(id="QA-002", question="Which city hosts transmission node TRN-042?",
             answerable_from="network/registry.json", in_corpus=False, documented_in=[], from_network_data=True,
             expected="Kemi", kind="fact"),
        # ---- answerable FROM DOCUMENTS
        dict(id="QA-010", question="What should you do when an interface stays UP with normal optics and zero "
                                  "traffic counters after an SFP replacement on an older unit?",
             answerable_from="teams + email + PDF", in_corpus=True, documented_in=[
                 _ref_teams("chan-noc-transport", "Disable the interface, wait a little, then enable it again"),
                 {"type": "email", "ref": "RE: TRN-042 traffic not recovering after SFP replacement",
                  "note": "email_outlook/Sent Items/2025-10-14 1145 - RE TRN-042 ..."},
                 {"type": "pdf", "ref": "documents/expert_notes/2019-11_transmission_upgrade_notes.pdf"}],
             expected="Disable/enable (bounce) the interface; do not replace the SFP again or reboot the node.",
             kind="procedure"),
        dict(id="QA-011", question="Which hardware generations are affected by the forwarding-state stall after an "
                                  "SFP swap?",
             answerable_from="teams", in_corpus=True,
             documented_in=[_ref_teams("chan-noc-transport", "So it's not only Gen1")],
             expected="Gen1 and Gen2 (the 'old units only' claim was corrected on 2026-03-10)", kind="scope",
             trap="Teams earlier says Gen1 only; the newer message supersedes it"),
        dict(id="QA-012", question="How do you get into the Kemi transport room, and who has access?",
             answerable_from="teams + email", in_corpus=True,
             documented_in=[_ref_teams("chan-field-ops", "The padlock key is on my keyring"),
                            {"type": "email", "ref": "Kemi site new code lock installed (action for field ops)",
                             "note": "says there is a code lock now, no key, contact security / Heikki"}],
             expected="Private road, gated. Since 2026-08 a code lock (no physical key), custodian Heikki / "
                      "site security. Before 2026-08 the padlock key was on Pekka's keyring.",
             kind="access", trap="Teams knowledge is outdated"),
        dict(id="QA-013", question="What is the maximum allowed BESS depth of discharge and SOC band now?",
             answerable_from="email (superseding Teams + KB)", in_corpus=True,
             documented_in=[{"type": "email", "ref": "BESS operating mode change from 1 April (aFRR participation)"}],
             expected="SOC band 35-70%, DoD <= 35% per event, <= 6 events/day (aFRR cycling since 2026-04)",
             kind="energy", trap="Teams 2025 and Confluence both say 'hold 80% SOC' - stale"),
        dict(id="QA-014", question="What MTU do the core links use?",
             answerable_from="teams (supersedes KB and older Teams)", in_corpus=True,
             documented_in=[_ref_teams("chan-noc-transport", "core links are 9216")],
             expected="9216 since the 2026 firmware (earlier 9114 is stale)", kind="config",
             trap="Confluence KB and an older Teams message say 9114"),
        dict(id="QA-015", question="Who is allowed to reset a KMAX-4K microwave radio, and what must be done first?",
             answerable_from="teams (conflicting)", in_corpus=True,
             documented_in=[_ref_teams("chan-access-north", "Please do NOT power-cycle"),
                            _ref_teams("chan-access-north", "power-cycle the radio")],
             expected="Do not power-cycle without exporting config; escalate / get approval, check dish alignment. "
                      "(One person says power-cycle works; contested.)",
             kind="procedure", trap="two experts conflict"),
        dict(id="QA-016", question="Why do the DC2 grid power readings spike to more than twice the IT power?",
             answerable_from="teams", in_corpus=True,
             documented_in=[_ref_teams("chan-sre-infra", "Confirmed it's the PDUs")],
             expected="Metering artefact; do not use suspect rows for customer PUE reporting", kind="data quality"),
        dict(id="QA-017", question="Was there a scheduled cause for the DC1 power spikes in September 2026?",
             answerable_from="email", in_corpus=True,
             documented_in=[{"type": "email", "ref": "DC1 power meter swap 2026-09-02 (short spikes expected)"}],
             expected="Yes, a grid meter swap on 2026-09-02", kind="data quality"),
        dict(id="QA-018", question="What happens to Kafka DLQ messages on the training event topic?",
             answerable_from="teams", in_corpus=True,
             documented_in=[_ref_teams("chan-sre-infra", "DLQ drains on its own")],
             expected="On platform 4.0+ they drain within 10 min; persistent DLQ means poison message, do not "
                      "raise memory", kind="procedure", trap="older advice (raise to 4Gi) is obsolete"),
        dict(id="QA-019", question="What is the SFP module model used on port 1/1/3 of TRN-042?",
             answerable_from="teams attachment", in_corpus=True,
             documented_in=[{"type": "teams_attachment", "ref": "TRN-042_1-1-3_port.txt"}],
             expected="SFP-10G-8421", kind="fact"),
        # ---- NEVER DOCUMENTED (the agent must say so)
        dict(id="QA-030", question="Which exact Nokia firmware version fixes the forwarding-state stall?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED (the vendor incident mentions a fix exists / v6.2.1 is mentioned only as a "
                      "vendor note in the incident feed, which is not documentation. Treat as unknown.)",
             kind="procedure"),
        dict(id="QA-031", question="What is the cabling repair procedure after a rodent-damaged duct?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED", kind="procedure"),
        dict(id="QA-032", question="How long does a TNC-1X chassis replacement typically take on site?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED (no MTTR figures, no estimate)", kind="estimate"),
        dict(id="QA-033", question="Which RF channel plan should be used for a new KMAX-4K hop in Sodankylä?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED (no channel plan or LNOB guidance)", kind="design"),
        dict(id="QA-034", question="Who approves an emergency change at night during the sales freeze?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED (a Teams message mentions change approval, but no night emergency rule "
                      "or definitive approver is written down)", kind="process"),
        dict(id="QA-035", question="What is the preferred swap-and-repair procedure for a fibre connector on a "
                                  "co-located chain (e.g. Järvinen chain)?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED", kind="procedure"),
        dict(id="QA-036", question="What BESS aFRR revenue expectation should be used in the 2027 plan?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED (only the mode change is documented, no revenue numbers/assumptions)",
             kind="planning"),
        dict(id="QA-037", question="Which vendor escalation path is used for a 7750 SR power-supply fault on a "
                                  "weekend?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED", kind="process"),
        dict(id="QA-038", question="What is the accepted error margin when validating DC energy billing data?",
             answerable_from="nowhere", in_corpus=False, documented_in=[], from_network_data=False,
             expected="NOT DOCUMENTED", kind="data quality"),
    ]

    cases = [
        dict(id="CASE-001", title="TRN-042 SFP swap: UP, optics OK, zero counters",
             symptom="After an SFP replacement on port 1/1/3 the link is UP, RX power normal, alarms cleared, but "
                     "in/out octets stay at ~0 for hours.",
             right_action="Disable and re-enable interface 1/1/3; do NOT replace the SFP again or reboot the node.",
             documentation="documented", where=["teams:chan-noc-transport (2025-10-14)",
                                                "email:RE: TRN-042 traffic not recovering after SFP replacement",
                                                "documents/expert_notes/2019-11_transmission_upgrade_notes.pdf"],
             hidden_in="none — this is the case the brief is built around (it IS documented)"),
        dict(id="CASE-002", title="Kemi transport room unreachable (locked barrier)",
             symptom="Field engineer arrives at the private road to the Kemi transport room; gate locked.",
             right_action="Use the code lock (since 2026-08), code held by site security / Heikki Lehtonen. "
                          "Afterwards update the access register.",
             documentation="documented", where=["email:Kemi site: new code lock installed",
                                                "teams:Field-Ops-FI (outdated: 'Pekka has the key')"],
             hidden_in="only in email + Teams; contradicted by Confluence KB003 & ITSM KB0018420"),
        dict(id="CASE-003", title="Ivalo site unreachable in winter",
             symptom="Dispatched to the Ivalo node in January; the gravel road is impassable.",
             right_action="Call the Rovaniemi field contractor to clear the snow before travelling.",
             documentation="documented", where=["email:Ivalo site access",
                                                "documents/expert_notes/2024-06_ivalo_site_access.pdf"],
             hidden_in="PDF drawing + one email only"),
        dict(id="CASE-004", title="KMAX-4K RF alarm",
             symptom="RF alarm on a microwave hop (KMAX-4K).",
             right_action="Do not power-cycle (config recovery); export config, check dish alignment, escalate/approve.",
             documentation="part_documented", where=["teams:Access-North (conflicting power-cycle vs no power-cycle)"],
             hidden_in="two contradicting experts; not resolved - an agent must surface the conflict"),
        dict(id="CASE-005", title="BESS SOC alert firing",
             symptom="BESS SOC alert at 48% unexpectedly goes off daily since April.",
             right_action="Expected under aFRR cycling (SOC band 35-70%); update the alert threshold.",
             documentation="documented", where=["email:BESS operating mode change from 1 April",
                                                "teams (stale 'hold 80% SOC')", "Confluence (stale)"],
             hidden_in="new rule only in a single email"),
        dict(id="CASE-006", title="DC2 grid power > 2x IT power for a few hours",
             symptom="Telemetry shows impossible PUE on FI-TRE-DC2 (~4) every other week.",
             right_action="Mark as metering artefact (quality suspect); exclude from customer reporting.",
             documentation="documented", where=["teams:SRE-Infra 2025-11-18"],
             hidden_in="only Teams"),
        dict(id="CASE-007", title="Core link MTU change request",
             symptom="Request to set core links to 9216 bytes.",
             right_action="1998 - allowed since the 2026 firmware.",
             documentation="documented", where=["teams:2026-04-20", "Confluence (stale 9114)"],
             hidden_in="contradictory KB (9114)"),
        dict(id="CASE-008", title="Second Gen2 unit shows the zero-counter symptom",
             symptom="A TNC-2X unit behaves exactly like the TRN-042 case.",
             right_action="The bounce recipe applies; the 'old units only' scope is wrong.",
             documentation="documented", where=["teams:chan-noc-transport 2026-03-10"],
             hidden_in="only a chat correction (contradicts the earlier scope)"),
        dict(id="CASE-009", title="Kemi barrier: field visit at night",
             symptom="Automated dispatch sends a contractor at 02:00 to Kemi; gate access needed.",
             right_action="Code lock code is with security... but the out-of-hours security contact procedure "
                          "is NOT documented.",
             documentation="not_documented",
             where=["email covers the code; out-of-hours procedure missing"],
             hidden_in="the code-lock knowledge exists, but the out-of-hours escalation path was never written down"),
        dict(id="CASE-010", title="Which team owns the transport collector threshold for 'traffic zero'",
             symptom="CPU bus fault: ticket routing for zero-counters alerts.",
             right_action="NOC-Transport owns the interface-counters spec; the NMS banks do NOT define the rule.",
             documentation="part_documented", where=["telemetry/spec/interface_counters.yml",
                                                     "nms/alarm_policy.yaml (says enabled: false)"],
             hidden_in="discoverable only by reading the spec, not by searching docs"),
        dict(id="CASE-011", title="Firmware fix version for the forwarding bug",
             symptom="Need the exact fixed firmware to plan the upgrade.",
             right_action="Unknown.",
             documentation="not_documented", where=[],
             hidden_in="nobody wrote it down"),
    ]

    for q in qa:
        q.setdefault("from_network_data", False)
        if q["from_network_data"]:
            q["status"] = "answerable from network data (control question, not a documentation search)"
        elif q["in_corpus"]:
            q["status"] = "documented"
        else:
            q["status"] = "not_documented"

    # ---- write
    out = os.path.join(c.root_dir(), "answer_key")
    os.makedirs(out, exist_ok=True)
    c.write_jsonl(["answer_key", "questions.jsonl"], qa)
    c.write_jsonl(["answer_key", "cases.jsonl"], cases)
    c.write_csv(["answer_key", "qa_matrix.csv"],
                ["id", "kind", "in_documentation", "n_sources", "question"],
                ([q["id"], q["kind"], q["in_corpus"], len(q["documented_in"]), q["question"]] for q in qa))
    c.write_csv(["answer_key", "cases_matrix.csv"],
                ["id", "documentation", "title", "right_action"],
                ([x["id"], x["documentation"], x["title"], x["right_action"]] for x in cases))

    n_doc = sum(1 for q in qa if q["in_corpus"])
    n_not = len(qa) - n_doc
    n_cases_doc = sum(1 for x in cases if x["documentation"] == "documented")
    n_cases_part = sum(1 for x in cases if x["documentation"] == "part_documented")
    n_cases_not = sum(1 for x in cases if x["documentation"] == "not_documented")
    readme = f"""# answer_key — what IS documented, and where

This folder answers the question your agent must be able to answer:

> “Do our sources already contain the answer to this question — or has it never been documented anywhere?”

## Files

| File | What |
|---|---|
| `questions.jsonl` | {len(qa)} Q&A items. `in_corpus` = **is the answer present in the documentation corpus**; `documented_in` lists the concrete artefacts (message-ids / email subjects / files). `expected` is the reference answer for scoring. |
| `qa_matrix.csv` | flat one-line-per-question scoring sheet |
| `cases.jsonl` | {len(cases)} operational incident-style cases, each tagged `documented` / `part_documented` / `not_documented` with the correct next action |
| `cases_matrix.csv` | flat scoring sheet |

## How to use it

1. Ask your agent each question with the corpus mounted read-only.
2. The agent must answer **and** cite sources. Score:
   * documented questions: right answer + at least one correct `source_ref` (no invented sources);
   * `not_documented` questions/cases: the **correct behaviour is to say “not documented”** and (for cases)
     who/where to ask, e.g. “no record found; escalate to a human expert”. Any confident invented answer is a
     failure.

## Counts

* Questions: {n_doc} documented, {n_not - 2} never documented, 2 control questions answerable from the
  network records rather than the documentation (out of {len(qa)}).
* Cases: {n_cases_doc} documented, {n_cases_part} partially documented, {n_cases_not} never documented
  (out of {len(cases)}).
* The 2 control questions ("how many Gen1 nodes", "which city hosts TRN-042") are answerable from the
  network records (`network/registry.json`), not from documents — use them to confirm the agent is not
  just refusing everything.

## The point of the “never documented” items

The brief is about *silent metadata*: operational knowledge that lives only in people's heads. For a useful
autonomous agent, the dangerous failure mode is not “I don't know” — it is **“I'm sure” without a source**.
The `not_documented` rows exist so you can measure exactly that: does your agent tell the truth about the
limits of the documentation?

## Where the knowledge really lives (corpus map)

* `teams/messages/*.jsonl` — {sum(1 for _ in _count_lines("teams"))} messages in 6 channels, threaded, with attachments
* `email_outlook/Inbox` + `Sent Items` — 148 real Outlook `.msg` files (+ attachments)
* `documents/expert_notes/*.pdf` — 2 scanned expert notes
* `documents/confluence/*.html` — 4 KB pages (**stale in places**)
* `itsm/knowledge_articles.jsonl` — ITSM KB (**stale in places**)
* dropped-at-source channels: phone calls, in-person talks (`knowledge_graph/drop_rate_sheet.csv`)
"""
    c.write_text(["answer_key", "README.md"], readme)
    print(f"answer_key: {len(qa)} questions ({n_doc} documented / {n_not - 2} not / 2 control), "
          f"{len(cases)} cases ({n_cases_doc}/{n_cases_part}/{n_cases_not})")


def _ref_teams(channel: str, needle: str) -> dict:
    m = _first_message(channel, needle)
    return {"type": "teams_message", "channel": channel, "message_id": m["message_id"],
            "created_at": m["created_at"], "excerpt": m["text"][:90]}


def _count_lines(folder: str) -> list[str]:
    d = os.path.join(c.root_dir(), folder, "messages")
    out = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith(".jsonl"):
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    out += fh.readlines()
    return out


if __name__ == "__main__":
    main()
