#!/usr/bin/env python3
"""Check that every graph-layer record carries consistent valid_from / valid_to bounds.

    python3 mock_data/scripts/verify_temporal.py [mock_data_dir]

Convention (see generators/common.py): valid_from is inclusive, valid_to is exclusive, and
valid_to = null means "still believed true". Raw source systems (Teams, email, ITSM, CMDB, Jira)
and point-in-time event rows are intentionally not checked: they carry no validity of their own.

Exit code 0 = all good, 1 = violations (each printed on its own line).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "generators"))
import common as c  # noqa: E402

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
    return errors


def main(root: str | None = None) -> int:
    root = root or os.path.join(HERE, "..")
    errors = check(root)
    for e in errors:
        print("TEMPORAL:", e)
    print(f"temporal check: {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
