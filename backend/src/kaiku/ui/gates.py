"""What each capture gate decided for one rule, read back from the stored CaptureResult.

V1/V2 run together in `extraction.gates.ground`, so they share a row: a rule dropped there
names its gate in the reason ("V1: no action", "V2: the action comes from a question").
"""

from __future__ import annotations

from typing import Any

from kaiku.context.models import AssertionView, Scope
from kaiku.extraction.graph import CaptureResult, Item, Step
from kaiku.ui.models import Gate, ItemView, StepView

NODES = ("triage", "extract", "link", "validate", "reconcile", "persist")


def steps(raw: list[Step]) -> list[StepView]:
    """One row per node in pipeline order; reconcile and persist run once per rule."""
    total: dict[str, list[int]] = {}
    for s in raw:
        t = total.setdefault(s.node, [0, 0])
        t[0] += s.ms
        t[1] += 1
    return [StepView(node=n, ms=total[n][0], runs=total[n][1]) for n in NODES if n in total]


def items(result: CaptureResult) -> list[ItemView]:
    return [_item(i) for i in result.items]


def _item(item: Item) -> ItemView:
    d = item.draft
    quotes: dict[str, list[str]] = {
        "condition": [q.quote for q in d.conditions],
        "observed": [q.quote for q in d.observed],
        "action": [d.action.quote] if d.action else [],
        "avoid": [q.quote for q in d.avoid],
        "entity": [m.surface_form for m in d.entities],
    }
    return ItemView(
        statement=d.statement,
        topic=d.topic,
        assertion_id=item.assertion_id,
        dropped=item.dropped,
        gates=[_grounding(item), _entities(item), _telemetry(item), _temporal(item), _v6(item)],
        quotes={k: [q for q in v if q] for k, v in quotes.items() if any(v)},
    )


def _grounding(item: Item) -> Gate:
    name = "Grounding · speaker"
    g = item.grounded
    if g is None:
        return Gate(code="V1-V2", name=name, result="fail", detail=item.dropped or "not grounded")
    n = len(g.sources)
    if g.dropped:
        return Gate(
            code="V1-V2", name=name, result="flag", detail=f"removed: {'; '.join(g.dropped)}"
        )
    return Gate(
        code="V1-V2", name=name, result="pass", detail=f"every field quoted · {n} source(s)"
    )


def _entities(item: Item) -> Gate:
    name = "Entity at time"
    if item.grounded is None:
        return Gate(code="V3", name=name, result="skip")
    linked = item.linked
    if linked is None or not linked.bindings:
        return Gate(code="V3", name=name, result="fail", detail=item.dropped or "no entity")
    sure = [b for b in linked.bindings if not b.candidate and b.scope is not Scope.ACTIVITY]
    unsure = [b for b in linked.bindings if b.candidate]
    names = ", ".join(f"{b.entity_id} ({b.scope})" for b in sure) or "activity only"
    if unsure:
        ask = linked.question or ", ".join(b.entity_id for b in unsure)
        return Gate(code="V3", name=name, result="flag", detail=f"{names}; asked: {ask}")
    return Gate(code="V3", name=name, result="pass", detail=names)


def _telemetry(item: Item) -> Gate:
    name = "Network cross-check"
    if item.linked is None or (item.dropped and item.assertion_id is None):
        return Gate(code="V4", name=name, result="skip")
    where = " ".join(x for x in (item.telemetry_entity, item.telemetry_port) if x)
    detail = " · ".join(x for x in (where, item.telemetry_detail) if x)
    match item.telemetry:
        case "agrees":
            return Gate(code="V4", name=name, result="pass", detail=detail or "telemetry agrees")
        case "disagrees":
            return Gate(code="V4", name=name, result="flag", detail=detail or "disagrees")
    return Gate(code="V4", name=name, result="skip", detail="no symptom to check, or no data")


def _temporal(item: Item) -> Gate:
    name = "Temporal rules"
    if item.dropped and item.dropped.startswith("write rejected"):
        return Gate(code="V5", name=name, result="fail", detail=item.dropped)
    if item.assertion_id:
        return Gate(code="V5", name=name, result="pass", detail="R2-R7 checked in the write")
    return Gate(code="V5", name=name, result="skip")


def _v6(item: Item) -> Gate:
    name = "Reconcile"
    d = item.decision
    if d is None:
        return Gate(code="V6", name=name, result="skip")
    if d.skip_as:
        return Gate(code="V6", name=name, result="info", detail=f"already known as {d.skip_as}")
    parts = []
    if d.supersede:
        parts.append(f"supersedes {d.supersede}")
    if d.correct:
        parts.append(f"corrects {d.correct}")
    if d.corroborates:
        parts.append(f"corroborates {', '.join(d.corroborates)}")
    if d.contradicts:
        parts.append(f"contradicts {', '.join(d.contradicts)}")
    if not parts:
        return Gate(code="V6", name=name, result="pass", detail="new knowledge")
    result = "flag" if d.contradicts else "info"
    return Gate(code="V6", name=name, result=result, detail="; ".join(parts))


def stored(view: AssertionView) -> ItemView:
    """A capture from before step tracing: what the stored assertion still records (its
    quotes, its bindings and the V4 result), without the per-gate decisions."""
    q: dict[str, Any] = view.qualifiers or {}
    quotes = q.get("quotes") or {}
    action = quotes.get("action")
    sure = [b for b in view.bindings if not b.candidate and b.scope is not Scope.ACTIVITY]
    gates = [
        Gate(code="V1-V2", name="Grounding · speaker", result="pass", detail="stored with quotes"),
        Gate(
            code="V3",
            name="Entity at time",
            result="flag" if any(b.candidate for b in view.bindings) else "pass",
            detail=", ".join(f"{b.entity_id} ({b.scope})" for b in sure),
        ),
    ]
    telemetry = q.get("telemetry")
    if telemetry in ("agrees", "disagrees"):
        gates.append(
            Gate(
                code="V4",
                name="Network cross-check",
                result="pass" if telemetry == "agrees" else "flag",
                detail=str(q.get("telemetry_detail") or ""),
            )
        )
    links = [*(f"contradicts {x}" for x in view.contradicts)]
    links += [f"corroborates {x}" for x in view.corroborates]
    if view.supersedes:
        links.insert(0, f"supersedes {view.supersedes}")
    if links:
        gates.append(Gate(code="V6", name="Reconcile", result="info", detail="; ".join(links)))
    return ItemView(
        statement=view.statement,
        topic=view.topic or "",
        assertion_id=view.assertion_id,
        gates=gates,
        quotes={
            k: v
            for k, v in {
                "condition": list(quotes.get("conditions") or []),
                "action": [action] if action else [],
                "avoid": list(quotes.get("avoid") or []),
            }.items()
            if v
        },
    )
