#!/usr/bin/env python3
"""Check that every graph-layer record carries consistent valid_from / valid_to bounds.

    python3 mock_data/scripts/verify_temporal.py [mock_data_dir]

Convention (see generators/common.py): valid_from is inclusive, valid_to is exclusive, and
valid_to = null means "still believed true". Raw source systems (Teams, email, ITSM, CMDB, Jira)
carry no validity of their own, but they ARE the event stream, so record time = event time:

  (a) every assertion's recorded_at == the timestamp of the artefact its source_ref names
      (Teams message, email .msg, ITSM record, Jira issue, PDF, Confluence page); unresolvable
      refs are violations;
  (b) messages inside each Teams conversation are strictly increasing in time; email replies are
      later than the message they reply to; the mail index agrees with the .msg files;
  (c) event rows carrying both record_time and event_time have them equal; alarm rows are either
      alarms (raised <= cleared) or point events (state "event", cleared null);
  (d) the TRN-042 case is causally ordered across layers: SFP inserted < expert advice (msg 8) <
      port bounce < "Traffic is back" (msg 9) <= end of the first PM bin with traffic again, and the
      canonical thread matches Elisa's teams_chat.json (text verbatim, local times);
  (e) every answer-key reference to a message carries that message's real timestamp.

Exit code 0 = all good, 1 = violations (each printed on its own line).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "generators"))
import common as c  # noqa: E402
import verify_msg  # noqa: E402  (same folder)

OPEN_STATUSES = {"believed_true", "disputed", "unverified"}


def _load(root: str, rel: str):
    path = os.path.join(root, *rel.split("/"))
    with open(path, encoding="utf-8") as fh:
        if rel.endswith(".jsonl"):
            return [json.loads(line) for line in fh if line.strip()]
        return json.load(fh)


def _check_bounds(errors: list[str], where: str, row: dict, *, allow_null_from: bool = False) -> None:
    if "valid_from" not in row or "valid_to" not in row:
        errors.append(f"{where}: missing valid_from/valid_to")
        return
    vf, vt = row["valid_from"], row["valid_to"]
    if vf is None and not allow_null_from:
        errors.append(f"{where}: valid_from is null")
    if vf is not None and vt is not None and vt <= vf:
        errors.append(f"{where}: valid_to {vt} <= valid_from {vf}")


def _check_chain(errors: list[str], where: str, rows: list[dict]) -> None:
    """Records describing the same thing over time must not overlap."""
    rows = sorted(rows, key=lambda r: r["valid_from"])
    for a, b in zip(rows, rows[1:]):
        if a["valid_to"] is None or a["valid_to"] > b["valid_from"]:
            errors.append(f"{where}: [{a['valid_from']}, {a['valid_to']}) overlaps next record "
                          f"starting {b['valid_from']}")


def check(root: str) -> list[str]:
    errors: list[str] = []
    snap = c.SNAPSHOT_AT

    # ---------------------------------------------------------------- network layers
    for rel, key in [("network/as_designed/nodes_designed.json", "node_id"),
                     ("network/as_built/devices.json", "device_id"),
                     ("network/as_built/segments.json", "segment_id"),
                     ("network/as_built/sites.json", "code"),
                     ("network/as_running/nodes_current.json", "node_id"),
                     ("network/as_running/links_current.json", "link_id"),
                     ("network/as_running/links_retired.json", "link_id"),
                     ("network/as_running/physical_node_snapshots.json", "physical_node_id")]:
        for row in _load(root, rel):
            _check_bounds(errors, f"{rel}:{row[key]}", row)

    snaps = _load(root, "network/as_running/physical_node_snapshots.json")
    by_node = defaultdict(list)
    for s in snaps:
        by_node[s["physical_name"]].append(s)
    for node, rows in by_node.items():
        _check_chain(errors, f"physical_node_snapshots:{node}", rows)
    snap_by_id = {s["physical_node_id"]: s for s in snaps}
    for s in snaps:
        if s["supersedes"]:
            old = snap_by_id[s["supersedes"]]
            if old["valid_to"] != s["valid_from"]:
                errors.append(f"physical_node_snapshots:{s['physical_node_id']}: predecessor "
                              f"{old['physical_node_id']} ends {old['valid_to']}, successor starts {s['valid_from']}")

    # ---------------------------------------------------------------- entities
    entities = {e["entity_id"]: e for e in _load(root, "knowledge_graph/entities.jsonl")}
    for eid, e in entities.items():
        _check_bounds(errors, f"entities:{eid}", e)

    # ---------------------------------------------------------------- context assertions
    assertions = _load(root, "knowledge_graph/assertions.jsonl")
    by_id = {a["assertion_id"]: a for a in assertions}
    for a in assertions:
        aid, st = a["assertion_id"], a["status"]
        where = f"assertions:{aid}"
        _check_bounds(errors, where, a)
        if not a.get("recorded_at"):
            errors.append(f"{where}: missing recorded_at")
        vt = a["valid_to"]
        if st in OPEN_STATUSES and vt is not None and vt <= snap:
            errors.append(f"{where}: status {st} but valid_to {vt} is before the snapshot ({snap}); "
                          f"should be superseded or expired")
        if st == "superseded":
            succ = by_id.get(a.get("superseded_by") or "")
            if succ is None:
                errors.append(f"{where}: superseded but superseded_by is missing/unknown")
            elif succ["valid_from"] != vt:
                errors.append(f"{where}: valid_to {vt} != successor {succ['assertion_id']} valid_from "
                              f"{succ['valid_from']}")
        elif a.get("superseded_by"):
            errors.append(f"{where}: has superseded_by but status is {st}")
        if st == "expired" and (vt is None or vt > snap):
            errors.append(f"{where}: expired but valid_to {vt} is not in the past")
        if st == "disputed" and not a.get("contradicts"):
            errors.append(f"{where}: disputed but contradicts nothing")
        if a.get("supersedes") and by_id.get(a["supersedes"], {}).get("superseded_by") != aid:
            errors.append(f"{where}: supersedes {a['supersedes']} but the link is not mirrored")
        for rel in ("contradicts", "corroborates"):
            for other in a.get(rel, []):
                if other not in by_id:
                    errors.append(f"{where}: {rel} unknown {other}")
                elif aid not in by_id[other].get(rel, []):
                    errors.append(f"{where}: {rel} {other} is not symmetric")
        ent = entities.get(a["subject_entity_id"])
        if ent is None:
            errors.append(f"{where}: subject {a['subject_entity_id']} not in entities.jsonl")
        elif ent.get("valid_from") is None:
            pass  # already reported above as missing bounds
        elif ent["valid_from"] > a["valid_from"] or (ent["valid_to"] and ent["valid_to"] <= a["valid_from"]):
            errors.append(f"{where}: subject {ent['entity_id']} [{ent['valid_from']}, {ent['valid_to']}) "
                          f"does not exist at assertion valid_from {a['valid_from']}")

    # ---------------------------------------------------------------- access register
    reg = _load(root, "knowledge_graph/access_register.jsonl")
    by_seg = defaultdict(list)
    for r in reg:
        _check_bounds(errors, f"access_register:{r['segment_id']}", r)
        by_seg[r["segment_id"]].append(r)
    for seg, rows in by_seg.items():
        _check_chain(errors, f"access_register:{seg}", rows)

    # ---------------------------------------------------------------- contradictions reference ids
    for conflict in _load(root, "knowledge_graph/contradictions.json"):
        for ref in conflict["assertions"]:
            if not isinstance(ref, str) or ref not in by_id:
                errors.append(f"contradictions:{conflict['id']}: bad assertion reference {str(ref)[:40]}")
    for ins in _load(root, "knowledge_graph/derived_insights.json"):
        if ins.get("assertion") and ins["assertion"] not in by_id:
            errors.append(f"derived_insights:{ins['id']}: unknown assertion {ins['assertion']}")

    # ---------------------------------------------------------------- record time = event time
    src = _sources(root, errors)
    _check_recorded_at(errors, assertions, src)
    _check_conversations(errors, src)
    _check_event_rows(root, errors)
    _check_trn042(root, errors, src)
    _check_answer_key(root, errors, src)
    return errors


# ==================================================================== source resolution
def _filetime_to_iso(ft: int) -> str:
    secs = (ft - 116444736000000000) / 10_000_000
    return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sources(root: str, errors: list[str]) -> dict:
    """Index every raw artefact an assertion or answer-key row can point at -> its own timestamp."""
    teams: dict[str, dict] = {}
    mdir = os.path.join(root, "teams", "messages")
    for f in sorted(os.listdir(mdir)):
        if f.endswith(".jsonl"):
            for m in _load(root, f"teams/messages/{f}"):
                if m["message_id"] in teams:
                    errors.append(f"teams: duplicate message_id {m['message_id']}")
                teams[m["message_id"]] = m
    # emails: read the .msg files themselves (Message-ID + PR_CLIENT_SUBMIT_TIME), not the index
    emails: dict[str, dict] = {}
    for folder in ("Inbox", "Sent Items"):
        d = os.path.join(root, "email_outlook", folder)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not f.endswith(".msg"):
                continue
            props = verify_msg.read_cfb(os.path.join(d, f))["props"]
            mid = props.get(verify_msg.P_MSGID)
            headers = props.get(0x007D, "")
            in_reply = re.search(r"^In-Reply-To: (.+)$", headers, re.M)
            emails[mid] = {"sent_at": _filetime_to_iso(props[0x0039]), "file": f"{folder}/{f}",
                           "subject": props.get(verify_msg.P_SUBJECT), "headers": headers,
                           "in_reply_to": in_reply.group(1).strip() if in_reply else None}
    itsm: dict[str, str] = {}
    for kb in _load(root, "itsm/knowledge_articles.jsonl"):
        itsm[f"itsm/knowledge_articles.jsonl#{kb['article_id']}"] = c.date_to_instant(kb["valid_from"])
    for t in _load(root, "itsm/tickets.jsonl"):
        itsm[f"itsm/tickets.jsonl#{t['number']}"] = t["opened_at"]
    jira = {f"jira/issues.jsonl#{j['key']}": j["created"] for j in _load(root, "jira/issues.jsonl")}
    return {"teams": teams, "emails": emails, "itsm": itsm, "jira": jira, "root": root}


def _doc_time(root: str, rel: str) -> str | None:
    path = os.path.join(root, *rel.split("/"))
    if not os.path.exists(path):
        return None
    if rel.endswith(".pdf"):
        m = re.search(rb"/CreationDate \(D:(\d{14})Z\)", open(path, "rb").read())
        return dt.datetime.strptime(m.group(1).decode(), "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ") if m else None
    if rel.endswith(".html"):
        m = re.search(r"Last edited by .*? - (\d{4}-\d{2}-\d{2}) -", open(path, encoding="utf-8").read())
        return c.date_to_instant(m.group(1)) if m else None
    return None


def resolve(src: dict, source_type: str, ref: str) -> str | None:
    """Timestamp of the artefact named by (source_type, source_ref), or None if it cannot be found."""
    if source_type == "teams_message":
        m = src["teams"].get(ref)
        return m["created_at"] if m else None
    if source_type == "email":
        e = src["emails"].get(ref)
        return e["sent_at"] if e else None
    if source_type == "itsm":
        return src["itsm"].get(ref)
    if source_type == "jira":
        return src["jira"].get(ref if ref.startswith("jira/") else f"jira/issues.jsonl#{ref}")
    if source_type in ("pdf", "confluence"):
        return _doc_time(src["root"], ref)
    return None


def _check_recorded_at(errors: list[str], assertions: list[dict], src: dict) -> None:
    for a in assertions:
        where = f"assertions:{a['assertion_id']}"
        t = resolve(src, a["source_type"], a["source_ref"])
        if t is None:
            errors.append(f"{where}: source_ref {a['source_type']}:{a['source_ref'][:50]} does not resolve")
        elif a["recorded_at"] != t:
            errors.append(f"{where}: recorded_at {a['recorded_at']} != source event time {t}")


def _check_conversations(errors: list[str], src: dict) -> None:
    by_conv = defaultdict(list)
    for m in src["teams"].values():
        by_conv[m["conversation_id"]].append(m)
    for conv, msgs in by_conv.items():
        msgs.sort(key=lambda m: m["created_at"])
        for a, b in zip(msgs, msgs[1:]):
            if a["created_at"] >= b["created_at"]:
                errors.append(f"teams conversation {conv}: {a['message_id']} and {b['message_id']} "
                              f"not strictly increasing ({a['created_at']}, {b['created_at']})")
        for m in msgs:
            if m["created_iid"] != c.instant(c.parse_iso(m["created_at"])):
                errors.append(f"teams {m['message_id']}: created_iid is not the slot containing created_at")
    # files are written in time order too
    root = src["root"]
    for f in sorted(os.listdir(os.path.join(root, "teams", "messages"))):
        if f.endswith(".jsonl"):
            ts_ = [m["created_at"] for m in _load(root, f"teams/messages/{f}")]
            if ts_ != sorted(ts_):
                errors.append(f"teams/messages/{f}: rows not in time order")
    emails = src["emails"]
    for mid, e in emails.items():
        hdr = re.search(r"^Date: (.+)$", e["headers"], re.M)
        if not hdr:
            errors.append(f"email {e['file']}: no Date header")
        else:
            sent = dt.datetime.strptime(hdr.group(1).strip(), "%a, %d %b %Y %H:%M:%S %z")
            if c.iso(sent) != e["sent_at"]:
                errors.append(f"email {e['file']}: Date header {hdr.group(1)} != submit time {e['sent_at']}")
            if os.path.basename(e["file"])[:15] != c.to_local(sent).strftime("%Y-%m-%d %H%M"):
                errors.append(f"email {e['file']}: file name time != sent time {e['sent_at']}")
        if e["in_reply_to"]:
            parent = emails.get(e["in_reply_to"])
            if parent is None:
                errors.append(f"email {e['file']}: In-Reply-To {e['in_reply_to']} not in the corpus")
            elif parent["sent_at"] >= e["sent_at"]:
                errors.append(f"email {e['file']}: reply sent before its parent")
    index = _load(root, "email_outlook/mail_export_index.json")
    if len(index) != len(emails):
        errors.append(f"mail_export_index: {len(index)} rows but {len(emails)} .msg files")
    for row in index:
        e = emails.get(row["message_id"])
        if e is None or e["file"] != f"{row['folder']}/{row['file']}" or e["sent_at"] != row["sent_at"]:
            errors.append(f"mail_export_index: {row['file']} disagrees with its .msg file")


EVENT_FILES = ["network/events/device_events.jsonl", "network/alarms/alarms.jsonl", "itsm/tickets.jsonl",
               "itsm/change_calendar.jsonl", "jira/issues.jsonl", "vendors/incidents.jsonl"]


def _check_event_rows(root: str, errors: list[str]) -> None:
    for rel in EVENT_FILES:
        for i, row in enumerate(_load(root, rel)):
            if "record_time" in row and "event_time" in row and row["record_time"] != row["event_time"]:
                errors.append(f"{rel}:{i + 1}: record_time {row['record_time']} != event_time {row['event_time']}")
    for a in _load(root, "network/alarms/alarms.jsonl"):
        where = f"alarms:{a['alarm_id']}"
        if a["kind"] == "event":
            if a["state"] != "event" or a["cleared"] is not None:
                errors.append(f"{where}: point event must have state 'event' and cleared null")
        elif a["kind"] == "alarm":
            if (a["cleared"] is None) != (a["state"] == "active"):
                errors.append(f"{where}: state {a['state']} inconsistent with cleared {a['cleared']}")
            if a["cleared"] is not None and a["cleared"] < a["raised"]:
                errors.append(f"{where}: cleared before raised")
        else:
            errors.append(f"{where}: unknown kind {a['kind']}")
        if a["raised_iid"] != c.instant(c.parse_iso(a["raised"])):
            errors.append(f"{where}: raised_iid is not the slot containing raised")
    for e in _load(root, "network/events/device_events.jsonl"):
        if e["end_time"] <= e["event_time"]:
            errors.append(f"device_events:{e['event_id']}: end_time not after event_time")


def _check_trn042(root: str, errors: list[str], src: dict) -> None:
    where = "TRN-042 case"
    case = _load(root, "network/alarms/trn042_case.json")["timeline"]
    first = lambda typ, after="": next((a["raised"] for a in case if a["type"] == typ and a["raised"] > after), None)
    inserted = first("MODULE_INSERTED")
    bounce = first("IF_DISABLED", inserted or "")
    thread = [m for m in src["teams"].values() if "Changed the SFP on transmission node TRN-042" in m["text"]]
    if len(thread) != 1 or not inserted or not bounce:
        errors.append(f"{where}: cannot locate the thread / MODULE_INSERTED / IF_DISABLED")
        return
    conv = sorted((m for m in src["teams"].values() if m["conversation_id"] == thread[0]["conversation_id"]),
                  key=lambda m: m["created_at"])[:12]
    advice, back = conv[7], conv[8]
    if not advice["text"].startswith("That's it.\nDisable the interface") or "Traffic is back" not in back["text"]:
        errors.append(f"{where}: message 8/9 of the thread are not the advice / 'traffic is back'")
        return
    # first PM bin with traffic again after the bounce; samples are interval-start stamped
    rows = [json.loads(l) for l in gzip.open(os.path.join(root, "telemetry", "interface_counters_trn_15min.ndjson.gz"),
                                             "rt", encoding="utf-8")]
    trn = sorted((r for r in rows if r["ne_id"] == "TRN-042"), key=lambda r: r["ts"])
    stalled = [r for r in trn if inserted <= r["ts"] < bounce and r["in_octets_mbps"] < 0.01]
    after = [r for r in trn if c.iso(c.parse_iso(r["ts"]) + dt.timedelta(minutes=15)) > bounce]
    if not stalled or len(after) < 2:
        errors.append(f"{where}: telemetry shows no stall / no samples after the bounce")
        return
    partial, normal = after[0], after[1]
    recovery_end = c.iso(c.parse_iso(partial["ts"]) + dt.timedelta(minutes=15))
    chain = [("SFP inserted", inserted), ("advice (msg 8)", advice["created_at"]), ("bounce", bounce),
             ("'traffic is back' (msg 9)", back["created_at"])]
    for (na, ta), (nb, tb) in zip(chain, chain[1:]):
        if not ta < tb:
            errors.append(f"{where}: {na} {ta} is not before {nb} {tb}")
    if not back["created_at"] <= recovery_end:
        errors.append(f"{where}: 'traffic is back' {back['created_at']} after the recovery bin ends {recovery_end}")
    if not 0.01 <= partial["in_octets_mbps"] < 0.8 * normal["in_octets_mbps"]:
        errors.append(f"{where}: bin {partial['ts']} (contains the bounce) should show partial recovery "
                      f"({partial['in_octets_mbps']} vs next {normal['in_octets_mbps']})")
    zero_from = min(r["ts"] for r in stalled)
    tickets = [t for t in _load(root, "itsm/tickets.jsonl") if t["number"] == "INC00420001"]
    if tickets:
        opened = tickets[0]["opened_at"]
        if opened < c.iso(c.parse_iso(zero_from) + dt.timedelta(minutes=60)):
            errors.append(f"{where}: ticket opened {opened} before 60 min of zero traffic (from {zero_from})")
        if not (bounce < tickets[0]["resolved_at"] <= back["created_at"]):
            errors.append(f"{where}: ticket resolved_at {tickets[0]['resolved_at']} not between bounce and msg 9")
    # Elisa's original thread: verbatim text, same local times (Europe/Helsinki)
    elisa = os.path.join(root, "..", "Elisa_SinceAI2026", "ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026", "teams_chat.json")
    if os.path.exists(elisa):
        orig = json.load(open(elisa, encoding="utf-8"))["messages"]
        for o, m in zip(orig, conv):
            local = c.to_local(c.parse_iso(m["created_at"])).strftime("%H:%M")
            if o["text"] != m["text"] or o["time"] != local or o["author"] != m["author_name"]:
                errors.append(f"{where}: message {o['seq']} differs from Elisa's teams_chat.json")


def _check_answer_key(root: str, errors: list[str], src: dict) -> None:
    for rel in ("answer_key/questions.jsonl",):
        for q in _load(root, rel):
            for ref in q.get("documented_in", []):
                if ref["type"] == "teams_message":
                    m = src["teams"].get(ref["message_id"])
                    if m is None:
                        errors.append(f"{rel}:{q['id']}: unknown message {ref['message_id']}")
                    elif m["created_at"] != ref.get("created_at"):
                        errors.append(f"{rel}:{q['id']}: created_at {ref.get('created_at')} != message "
                                      f"{m['created_at']}")
                elif ref["type"] == "email":
                    e = src["emails"].get(ref.get("message_id"))
                    if e is None or e["sent_at"] != ref.get("created_at") or e["subject"] != ref["ref"]:
                        errors.append(f"{rel}:{q['id']}: email ref {ref['ref'][:40]} does not match its .msg")


def main(root: str | None = None) -> int:
    root = root or os.path.join(HERE, "..")
    errors = check(root)
    for e in errors:
        print("TEMPORAL:", e)
    print(f"temporal check: {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
