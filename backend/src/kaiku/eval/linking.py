"""ST-15: score entity linking against `entity_aliases.csv` (kind=entity). The alias file is
read HERE only, to score; the linker never sees it (BE-3.2).

Each alias is linked as a mention in its own text. A hit is the canonical entity among the
bindings. Precision counts every binding of the canonical entity's type (a generation
bound next to its model is not a wrong answer for a model alias).
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kaiku.extraction.link import link
from kaiku.extraction.models import Mention, TriageKind


class LinkScore(BaseModel):
    precision: float
    recall: float
    rows: int
    misses: list[str]
    wrong: list[str]


def score_aliases(
    aliases_csv: Path, inventory: Sequence[Mapping[str, Any]], at: datetime
) -> LinkScore:
    types = {e["entity_id"]: e.get("entity_type") for e in inventory}
    with aliases_csv.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["kind"].strip() == "entity"]
    hits, predicted = 0, 0
    misses, wrong = [], []
    for r in rows:
        surface, want = r["alias_surface"].strip(), r["canonical_entity"].strip()
        linked = link([Mention(surface_form=surface)], surface, TriageKind.RULE, inventory, at)
        same_type = [
            b.entity_id for b in linked.bindings if types.get(b.entity_id) == types.get(want)
        ]
        predicted += len(same_type)
        if want in same_type:
            hits += 1
        else:
            misses.append(f"{surface} -> {want}")
        wrong += [f"{surface} -> {e} (want {want})" for e in same_type if e != want]
    return LinkScore(
        precision=hits / predicted if predicted else 0.0,
        recall=hits / len(rows) if rows else 0.0,
        rows=len(rows),
        misses=misses,
        wrong=wrong,
    )
