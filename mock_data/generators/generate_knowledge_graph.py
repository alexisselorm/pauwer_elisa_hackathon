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


def entity_aliases() -> list[tuple[str, str]]:
    """How specialists actually refer to things -> the canonical id.

    The canonical side is either an `entity_id` from entities.jsonl, or `concept:<name>` for generic
    terms that name a *kind* of thing rather than one entity ("the gate", "SFP"). Scoring-only: the
    extraction pipeline must resolve names from inventory attributes, not from this file.
    """
    groups: list[tuple[str, list[str]]] = [
        # Gen1 transmission hardware model (the brief's "older units")
        ("TNC-1X", ["the old units", "older units", "older transmission units", "older units (2019)",
                    "Gen1 units", "the old TNC-1X", "TNC-1X", "tnc1x", "tnc1_roadm", "tnc-1x_roadm"]),
        ("TNC-2X", ["TNC-2X", "Gen2 units", "tnc2x", "tnc2_roadm", "tnc-2x_roadm"]),
        ("TNC-3X", ["TNC-3X", "Gen3 units", "tnc3x", "tnc3_roadm", "tnc-3x_roadm"]),
        # the specific node in the case
        ("TRN-042", ["TRN-042", "the Kemi box", "Kemi TRN-042 (ROADM-1)"]),
        # the specific gated segment (Kemi transport room private road)
        ("SEG-042", ["gate at Kemi", "Kemi gate", "the Kemi barrier", "private road to the Kemi transport room"]),
        ("FI-TRE-DC2", ["Tampere DC", "TRE", "DC2", "FI-TRE-DC2", "the secondary site"]),
        ("FI-HEL-DC1", ["Helsinki DC1", "HEL", "DC1", "FI-HEL-DC1", "the primary"]),
        ("TOPIC:forwarding_state_after_sfp_swap",
         ["forwarding state", "forwarding plane state", "FIB state", "silent blackhole", "green-but-dark"]),
        # generic kinds: no single entity
        ("concept:barrier", ["bit-barrier", "barrier", "portti", "the gate", "the boom at the road",
                             "private road gate"]),
        ("concept:optical_transport", ["transmission node", "TRN node", "ROADM", "optical transport unit", "TN",
                                       "line side", "the node"]),
        ("concept:sfp", ["SFP", "SFP+", "optical module", "transceiver", "the module", "10G optic"]),
        ("concept:power_meter", ["the power meter", "meter", "PDU meter", "electricity meter", "the kWh meter"]),
        ("concept:hw_model", ["hardware model", "hardware_class", "hw", "HWC", "device model"]),
    ]
    return [(alias, canon) for canon, aliases in groups for alias in aliases]


def main() -> None:
    entities = _load_jsonl("knowledge_graph/entities.jsonl")
    assertions = _load_jsonl("knowledge_graph/assertions.jsonl")
    aliases = entity_aliases()
    known = {e["entity_id"] for e in entities}
    bad = [(a, canon) for a, canon in aliases if not canon.startswith("concept:") and canon not in known]
    if entities and bad:
        raise ValueError(f"entity_aliases: canonical ids not in entities.jsonl: {bad}")
    dupes = {a for a, _ in aliases if sum(1 for b, _ in aliases if b == a) > 1}
    if dupes:
        raise ValueError(f"entity_aliases: alias mapped twice: {sorted(dupes)}")
    c.write_csv(["knowledge_graph", "entity_aliases.csv"], ["alias_surface", "canonical_entity", "kind"],
                ([a, canon, "concept" if canon.startswith("concept:") else "entity"] for a, canon in aliases))
    if assertions:
        c.write_json(["knowledge_graph", "counts.json"], {
            "entities": len(entities), "assertions": len(assertions),
            "by_status": _count(assertions, "status"),
            "by_topic": _count(assertions, "topic"),
        })


def _count(rows, field):
    out: dict[str, int] = {}
    for r in rows:
        out[r.get(field, "?")] = out.get(r.get(field, "?"), 0) + 1
    return dict(sorted(out.items()))


def _exists(rel: str) -> bool:
    return os.path.exists(os.path.join(c.root_dir(), *rel.split("/")))


if __name__ == "__main__":
    main()
