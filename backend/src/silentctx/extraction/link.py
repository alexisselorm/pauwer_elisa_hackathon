"""Entity linking from the inventory (BE-3.2, plan 001 §5) and gate V3.

Surface forms resolve against what the graph knows, never against `entity_aliases.csv`
(that file only scores us, ST-15):
- exact ids (TRN-042, TNC-1X, SEG-042) -> 1.0;
- "older units", "installed 2019" -> the model and generation of the devices in the thread,
  surer when the stated year is the device's commissioning year;
- site and city names -> devices, private-road segments and sites there (context.resolve);
- the work being done ("changed the SFP") -> the closest Topic, as `activity`.

V3: a binding whose entity does not exist at `valid_from`, or that is a tie between two
entities of the same kind, is stored as a `candidate`, and the card asks one question.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from rapidfuzz import fuzz

from silentctx.context.models import BindingIn, EntityCandidate, Scope
from silentctx.context.resolve import resolve
from silentctx.extraction.models import Mention, TriageKind

_ID = re.compile(r"\b(?:TRN|ACC|CR|SEG|PON)(?:-[A-Z]+)*-\d{2,4}\b", re.IGNORECASE)
_OLD = re.compile(
    r"\b(old|older|oldest)\b(?:\s+\w+){0,2}?\s+(units?|box(?:es)?|nodes?|devices?|ones|kit)\b",
    re.IGNORECASE,
)
_INSTALLED = re.compile(
    r"\b(?:installed|commissioned|deployed|from)\s+(?:around\s+|in\s+|about\s+)?"
    r"((?:19|20)\d\d)\b|\(((?:19|20)\d\d)\)",
    re.IGNORECASE,
)
_PORT = re.compile(r"\b\d{1,2}/\d{1,2}/\d{1,2}\b")
# Words that name the same work differently.
_SYNONYMS = {
    "swap": "replacement",
    "swapped": "replacement",
    "changed": "replacement",
    "change": "replacement",
    "changing": "replacement",
    "replaced": "replacement",
    "replace": "replacement",
    "replacing": "replacement",
    "optics": "sfp",
    "module": "sfp",
    "transceiver": "sfp",
    # Forwarding Information Base; "forwarding plane state" is the forwarding state.
    "fib": "forwarding",
    "plane": "",
    "optical": "",
    "the": "",
    "a": "",
    "an": "",
    "on": "",
    "of": "",
}
TOPIC_MATCH = 85  # token_set_ratio: the words said are (nearly) all in the label
TIE = 0.05
SCOPE_BY_TYPE = {
    "device": Scope.OBSERVED_ON,
    "fibre": Scope.OBSERVED_ON,
    "microwave": Scope.OBSERVED_ON,
    "site": Scope.OBSERVED_ON,
    "hw_model": Scope.APPLIES_TO,
    "generation": Scope.APPLIES_TO,
    "topic": Scope.ACTIVITY,
}


class Linked(BaseModel):
    bindings: list[BindingIn]
    question: str | None = None  # one clarifying question (V3), with options
    options: list[EntityCandidate] = []


def ports(text: str) -> list[str]:
    return sorted(set(_PORT.findall(text)))


def _alive(e: Mapping[str, Any], t: datetime) -> bool:
    vf, vt = e.get("valid_from"), e.get("valid_to")
    return (vf is None or vf <= t) and (vt is None or t < vt)


def _words(text: str) -> str:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.casefold()):
        w = _SYNONYMS.get(w, w)
        if w:
            out.append(w)
    return " ".join(out)


def _topic(
    surface: str, topics: Iterable[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], float] | None:
    """The topic whose label holds the words said ("forwarding state", "changed the SFP");
    among equals, the closest label overall. One word is too little to go on."""
    said = _words(surface)
    if len(said.split()) < 2:
        return None
    best: tuple[tuple[float, float], Mapping[str, Any]] | None = None
    for e in topics:
        label = _words((e.get("label") or e["entity_id"].removeprefix("TOPIC:")).replace("_", " "))
        key = (fuzz.token_set_ratio(said, label), fuzz.token_sort_ratio(said, label))
        if key[0] >= TOPIC_MATCH and (best is None or key > best[0]):
            best = (key, e)
    return (best[1], best[0][0]) if best else None


def _year(device: Mapping[str, Any]) -> str:
    return str(device.get("commissioned") or device.get("valid_from") or "")[:4]


def _generic_model(
    inventory: Sequence[Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
    year: str | None,
) -> Mapping[str, Any] | None:
    """ "Older units" with no device named: the generational model most installed in the
    stated year, else the one whose devices were installed first."""
    years: dict[str, list[str]] = {}
    for d in inventory:
        model = by_id.get(str(d.get("model") or "").casefold())
        if d.get("entity_type") == "device" and model and model.get("generation"):
            years.setdefault(model["entity_id"], []).append(_year(d))
    if not years:
        return None
    if year:
        counts = {m: ys.count(year) for m, ys in years.items()}
        best = max(counts, key=lambda m: (counts[m], m))
        return by_id[best.casefold()] if counts[best] else None
    return by_id[min(years, key=lambda m: (min(years[m]), m)).casefold()]


def _model_pattern(model_id: str, others: Sequence[str]) -> re.Pattern[str]:
    """ "TNC-1X" -> tnc[-_ ]?1(?:[-_ ]?x)?, so "tnc1x", "tnc1_roadm" and "TNC-1X" match. A
    trailing letter is optional only when no other model shares the rest of the id."""
    chunks = re.findall(r"[a-z]+|\d+", model_id.casefold())
    head, tail = chunks[:-1], chunks[-1]
    prefix = "".join(head)
    shared = any(
        o != model_id and "".join(re.findall(r"[a-z]+|\d+", o.casefold())[:-1]) == prefix
        for o in others
    )
    sep = "[-_ ]?"
    body = sep.join(re.escape(c) for c in head)
    end = f"(?:{sep}{re.escape(tail)})" + ("" if shared or not tail.isalpha() else "?")
    return re.compile(rf"(?<![a-z0-9]){body}{end}(?![0-9])", re.IGNORECASE)


def link(
    mentions: Sequence[Mention],
    thread_text: str,
    kind: TriageKind,
    inventory: Sequence[Mapping[str, Any]],
    valid_from: datetime,
) -> Linked:
    by_id = {e["entity_id"].casefold(): e for e in inventory}
    found: dict[str, BindingIn] = {}

    def bind(e: Mapping[str, Any], scope: Scope | None, confidence: float, surface: str) -> None:
        eid = e["entity_id"]
        scope = scope or SCOPE_BY_TYPE.get(e.get("entity_type") or "", Scope.OBSERVED_ON)
        current = found.get(eid)
        if current is None or confidence > current.resolution_confidence:
            found[eid] = BindingIn(
                entity_id=eid,
                scope=scope,
                surface_form=surface,
                resolution_confidence=round(confidence, 3),
            )

    # 1. Exact ids anywhere in the thread.
    for m in _ID.finditer(thread_text):
        if (e := by_id.get(m.group(0).casefold())) is not None:
            bind(e, None, 1.0, m.group(0))
    models = [e for e in inventory if e.get("entity_type") == "hw_model"]
    model_ids = [m["entity_id"] for m in models]
    for e in models:
        if re.search(
            rf"(?<![\w-]){re.escape(e['entity_id'])}(?![\w-])", thread_text, re.IGNORECASE
        ):
            bind(e, Scope.APPLIES_TO, 1.0, e["entity_id"])
        elif (
            found_id := _model_pattern(e["entity_id"], model_ids).search(thread_text)
        ) is not None:
            bind(e, Scope.APPLIES_TO, 0.9, found_id.group(0))

    # 2. "older units, installed 2019": the model and generation of the devices named.
    old = _OLD.search(thread_text)
    year = _INSTALLED.search(thread_text)
    if old or year:
        devices = [
            by_id[i.casefold()] for i in found if by_id[i.casefold()].get("entity_type") == "device"
        ]
        surface = " / ".join(x.group(0) for x in (old, year) if x)
        stated = (year.group(1) or year.group(2)) if year else None
        for d in devices:
            model = by_id.get(str(d.get("model") or "").casefold())
            if model is None:
                continue
            sure = 0.9 if stated and _year(d) == stated else 0.75
            bind(model, Scope.APPLIES_TO, sure, surface)
        named = any(b.scope is Scope.APPLIES_TO for b in found.values())
        generic = None if devices or named else _generic_model(inventory, by_id, stated)
        if generic is not None:
            bind(generic, Scope.APPLIES_TO, 0.7, surface)

    # 3. The LLM's mentions: places, models, the activity.
    topics = [e for e in inventory if e.get("entity_type") == "topic"]
    options: list[EntityCandidate] = []
    question = None
    for mention in mentions:
        cands = (
            []
            if mention.role is Scope.ACTIVITY
            else resolve(mention.surface_form, inventory, {}, valid_from)
        )
        if not cands:  # "changed the SFP", "forwarding state": the work, not a thing
            if (hit := _topic(mention.surface_form, topics)) is not None:
                bind(hit[0], Scope.ACTIVITY, hit[1] / 100, mention.surface_form)
            continue
        if kind is TriageKind.ACCESS and any(
            c.entity_type in ("fibre", "microwave", "site") for c in cands
        ):
            cands = [c for c in cands if c.entity_type != "device"]  # a gate is not a node
        if not cands or cands[0].entity_id in found:
            continue
        top = cands[0]
        ties = [
            c
            for c in cands
            if c.entity_type == top.entity_type and top.confidence - c.confidence < TIE
        ]
        if len(ties) > 1 and top.confidence < 1.0:
            for c in ties:
                found[c.entity_id] = BindingIn(
                    entity_id=c.entity_id,
                    scope=mention.role or SCOPE_BY_TYPE.get(c.entity_type, Scope.OBSERVED_ON),
                    surface_form=mention.surface_form,
                    resolution_confidence=c.confidence,
                    candidate=True,
                )
            if question is None:
                question = f'Which one is "{mention.surface_form}"?'
                options = ties
        else:
            bind(
                by_id[top.entity_id.casefold()], mention.role, top.confidence, mention.surface_form
            )

    # A model brings its generation, and a generation with a single model brings it.
    for b in list(found.values()):
        e = by_id[b.entity_id.casefold()]
        if b.candidate:
            continue
        if e.get("entity_type") == "hw_model" and e.get("generation"):
            if (gen := by_id.get(str(e["generation"]).casefold())) is not None:
                bind(gen, Scope.APPLIES_TO, b.resolution_confidence, b.surface_form or "")
        elif e.get("entity_type") == "generation":
            of_gen = [m for m in models if m.get("generation") == e["entity_id"]]
            if len(of_gen) == 1:
                bind(of_gen[0], Scope.APPLIES_TO, b.resolution_confidence, b.surface_form or "")

    # V3: every binding exists when the knowledge starts.
    for eid, b in list(found.items()):
        if not b.candidate and not _alive(by_id[eid.casefold()], valid_from):
            found[eid] = b.model_copy(update={"candidate": True})
            if question is None:
                question = f"{eid} did not exist when this starts. Is it the right one?"
                options = [
                    EntityCandidate(
                        entity_id=eid,
                        entity_type=by_id[eid.casefold()].get("entity_type") or "unknown",
                        confidence=b.resolution_confidence,
                        matched=b.surface_form or eid,
                    )
                ]
    return Linked(
        bindings=sorted(found.values(), key=lambda b: (b.scope, b.entity_id)),
        question=question,
        options=options,
    )
