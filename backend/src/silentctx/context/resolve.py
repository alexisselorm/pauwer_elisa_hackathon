"""Entity resolution: free text -> candidate graph entities with a confidence.

"the Pori node" -> TRN-017, "older transmission units" -> TNC-1X, "Kemi gate" -> SEG-042.
Deterministic and explainable: exact ids, then known aliases (entity_aliases.csv), then
places (a city in the text matches devices and private-road segments there).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz

from silentctx.context.models import EntityCandidate

# partial_ratio: aliases score 90+ against text that contains a variant of them
# ("old" vs "older") and ~40 against unrelated text.
FUZZY_THRESHOLD = 88
FUZZY_MIN_LEN = 8  # short aliases ("tnc1x") only match exactly

_ID = re.compile(r"\b(?:TRN|ACC|CR|SEG)(?:-[A-Z]+)?-\d{2,4}\b", re.IGNORECASE)
_WORD = re.compile(r"[a-zåäö0-9-]+", re.IGNORECASE)
# Words that mean the asker is talking about getting to a site, not a device.
_ACCESS_WORDS = {"gate", "road", "access", "key", "lock", "code", "barrier", "boom", "padlock"}
_NODE_WORDS = {"node", "transport", "trn", "roadm", "unit", "box", "transmission"}


def _alive(e: Mapping[str, Any], t: datetime) -> bool:
    vf, vt = e.get("valid_from"), e.get("valid_to")
    return (vf is None or vf <= t) and (vt is None or t < vt)


def resolve(
    text: str,
    entities: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, str],
    t: datetime,
    limit: int = 10,
) -> list[EntityCandidate]:
    ents = [e for e in entities if _alive(e, t)]
    by_id = {e["entity_id"].casefold(): e for e in ents}
    lowered = text.casefold()
    words = set(_WORD.findall(lowered))
    found: dict[str, EntityCandidate] = {}

    def add(e: Mapping[str, Any], confidence: float, matched: str) -> None:
        current = found.get(e["entity_id"])
        if current is None or confidence > current.confidence:
            found[e["entity_id"]] = EntityCandidate(
                entity_id=e["entity_id"],
                entity_type=e.get("entity_type") or "unknown",
                name=e.get("name") or e.get("label"),
                confidence=confidence,
                matched=matched,
            )

    # 1. Exact ids (TRN-017, SEG-042, TNC-1X, ...).
    for m in _ID.finditer(text):
        if (e := by_id.get(m.group(0).casefold())) is not None:
            add(e, 1.0, m.group(0))
    for e in ents:
        if e.get("entity_type") == "hw_model" and e["entity_id"].casefold() in lowered:
            add(e, 0.95, e["entity_id"])
        elif e.get("entity_type") == "generation" and e["entity_id"].casefold() in words:
            add(e, 0.9, e["entity_id"])

    # 2. Aliases ("older transmission units" -> TNC-1X): exact, then fuzzy, so that
    #    "the old transmission units" still matches (rapidfuzz partial_ratio).
    for alias in sorted(aliases, key=len, reverse=True):
        target = by_id.get(aliases[alias].casefold())
        if target is None:
            continue
        pattern = rf"(?<![\w-]){re.escape(alias)}(?![\w-])"
        if re.search(pattern, lowered):
            add(target, 0.9, alias)
        elif len(alias) >= FUZZY_MIN_LEN:
            ratio = fuzz.partial_ratio(alias, lowered)
            if ratio >= FUZZY_THRESHOLD:
                add(target, round(0.85 * ratio / 100, 3), alias)

    # 3. Places: a city named in the text.
    access = bool(words & _ACCESS_WORDS)
    node = bool(words & _NODE_WORDS)
    for e in ents:
        place = (e.get("city") or e.get("room") or e.get("name") or "").casefold()
        city = place.split(" ")[0] if place else ""
        if not city or city not in words:
            continue
        kind = e.get("entity_type")
        if kind == "device":
            transport = e["entity_id"].startswith("TRN-")
            confidence = 0.8 if transport and not access else 0.75 if transport else 0.4
            if node and transport:
                confidence = 0.85
            add(e, confidence, city)
        elif kind in ("fibre", "microwave") and e.get("private_road"):
            add(e, 0.8 if access else 0.5, city)

    return sorted(found.values(), key=lambda c: (-c.confidence, c.entity_id))[:limit]
