"""Knowledge layer: entities, context assertions and the "ground truth" labels.

This is the layer your pipeline is supposed to *reconstruct* from raw channels.
It is written as an explicit, time-aware graph so the demo has a gold reference:

  entities.jsonl            every node/entity an assertion can bind to
  assertions.jsonl          ContextAssertion records (valid_from, valid_to, supersedes,
                            source_ref, extracted_from, confidence, contradicts)
  entity_aliases.csv        messy name -> canonical entity (from the 'specialist grammar')
  access_register.jsonl     keys/codes/callers for gated sites (from emails + PDF)
  contradictions.json       the human-readable list of the designed conflicts
  derived_insights.json     conclusions that require cross-referencing >=2 layers

Records are produced by generate_context.py from the *same* event definitions that
drive the Teams/email sources, so source_ref IDs line up exactly with the raw files.
"""
from __future__ import annotations

import json
import os

import common as c


def _load_jsonl(rel: str) -> list[dict]:
    path = os.path.join(c.root_dir(), *rel.split("/"))
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_entity_graph(entities: list[dict], assertions: list[dict]) -> None:
    c.write_jsonl(["knowledge_graph", "entities.jsonl"], entities)
    c.write_jsonl(["knowledge_graph", "assertions.jsonl"], assertions)


def entity_aliases() -> list[list[str]]:
    """How specialists actually refer to things vs. the canonical id."""
    return [
        ["the old units", "older units", "older transmission units", "older units (2019)", "Gen1 units", "the old TNC-1X", "TNC-1X", "the Kemi box", "TRN-042"],
        ["bit-barrier", "barrier", "portti", "the gate", "the boom at the road", "private road gate", "gate at Kemi"],
        ["transmission node", "TRN node", "ROADM", "optical transport unit", "TN", "line side", "the node"],
        ["SFP", "SFP+", "optical module", "transceiver", "the module", "10G optic"],
        ["forwarding state", "forwarding plane state", "FIB state", "silent blackhole", "green-but-dark"],
        ["the power meter", "meter", "PDU meter", "electricity meter", "the kWh meter"],
        ["Tampere DC", "TRE", "DC2", "FI-TRE-DC2", "the secondary site"],
        ["Helsinki DC1", "HEL", "DC1", "FI-HEL-DC1", "the primary"],
        ["hardware model", "hardware_class", "hw", "HWC", "device model", "tnc2_roadm", "TNC-2X"],
    ]


def main() -> None:
    entities = _load_jsonl("knowledge_graph/entities.jsonl")
    assertions = _load_jsonl("knowledge_graph/assertions.jsonl")
    c.write_csv(["knowledge_graph", "entity_aliases.csv"], ["alias_surface", "canonical_entity"],
                ([a, canon] for canon, aliases in _rotate(entity_aliases()) for a in aliases))
    if assertions:
        c.write_json(["knowledge_graph", "counts.json"], {
            "entities": len(entities), "assertions": len(assertions),
            "by_status": _count(assertions, "status"),
            "by_topic": _count(assertions, "topic"),
        })


def _rotate(pairs):
    for canon, *aliases in pairs:
        yield canon, aliases


def _count(rows, field):
    out: dict[str, int] = {}
    for r in rows:
        out[r.get(field, "?")] = out.get(r.get(field, "?"), 0) + 1
    return dict(sorted(out.items()))


def _exists(rel: str) -> bool:
    return os.path.exists(os.path.join(c.root_dir(), *rel.split("/")))


if __name__ == "__main__":
    main()
