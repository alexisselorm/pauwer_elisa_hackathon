"""V6 Reconcile (plan 001 §6.1): a new draft against what is already known.

The LLM judge names the relation to each candidate. Code decides what is written:
- duplicate from the same thread -> nothing new (a re-triage after more replies);
  duplicate or corroboration from another source -> CORROBORATES (ST-02), never a copy;
- corrects, same thread -> R6(ii) correction of the earlier extraction (card updated);
  corrects from elsewhere -> a disagreement, so CONTRADICTS;
- supersedes -> R4b, only if the draft starts later (else it is a disagreement);
- contradicts -> CONTRADICTS. Only the side with lower or equal channel authority becomes
  disputed, so a stale ITSM KB does not dispute an expert rule (ST-28), while two experts
  disagreeing are both disputed (KMAX-4K).

Authority ranking, used to order candidates and pick the one superseded:
recency > author role > channel.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from silentctx.extraction.models import Relation, RelationKind

# Expert chat and email outrank the ticketing KB and wiki pages.
_LOW_AUTHORITY = frozenset({"itsm", "confluence", "kb", "wiki"})
_ROLE_RANK = (
    (re.compile(r"expert|principal|architect|lead", re.I), 3),
    (re.compile(r"senior", re.I), 2),
    (re.compile(r"engineer|technician", re.I), 1),
)


def channel_authority(platform: str | None) -> int:
    return 0 if (platform or "").casefold() in _LOW_AUTHORITY else 1


def role_rank(role: str | None) -> int:
    for pattern, rank in _ROLE_RANK:
        if role and pattern.search(role):
            return rank
    return 0


def rank_key(recorded_at: datetime, role: str | None, platform: str | None) -> tuple[Any, ...]:
    return (recorded_at, role_rank(role), channel_authority(platform))


# Nothing changes in the world mid-conversation: a reply that "supersedes" advice given
# minutes earlier in the same thread is a disagreement (KMAX-4K), while H-07's correction
# seven weeks later is a supersession.
SAME_CONVERSATION = timedelta(days=1)


def equipment(entity_ids: Iterable[str], by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """What hardware an assertion is about, at generation level plus the devices themselves:
    a device brings its model's generation, a model its generation. Sites, segments and
    topics bring nothing (access knowledge has no hardware scope)."""
    out: set[str] = set()
    for eid in entity_ids:
        e = by_id.get(eid.casefold())
        if e is None:
            continue
        kind = e.get("entity_type")
        if kind == "device":
            out.add(e["entity_id"])
            e = by_id.get(str(e.get("model") or "").casefold()) or {}
            kind = e.get("entity_type")
        if kind == "hw_model" and e.get("generation"):
            out.add(str(e["generation"]))
        elif kind == "generation":
            out.add(e["entity_id"])
    return out


def disjoint(new: set[str], other: set[str]) -> bool:
    """Both name hardware and none of it is shared (TNC-3X vs Gen1/TNC-1X). Unknown on
    either side is not disjoint, so access knowledge and unscoped rules keep the old path."""
    return bool(new) and bool(other) and not (new & other)


class Decision(BaseModel):
    skip_as: str | None = None  # already known from this thread: nothing to write
    correct: str | None = None
    supersede: str | None = None
    corroborates: list[str] = []
    contradicts: list[str] = []


def decide(
    *,
    thread_id: str,
    valid_from: datetime,
    candidates: Sequence[Mapping[str, Any]],
    relations: Sequence[Relation],
    action: str | None = None,
    avoid: Sequence[str] = (),
    elsewhere: Collection[str] = (),
) -> Decision:
    """`elsewhere`: candidates about other hardware than the new one (`disjoint`). A rule for
    TNC-3X that says "replace the SFP" doesn't contradict a Gen1 rule that says "don't", and
    a change on one model doesn't supersede knowledge about another (found live, G-01).
    The judge, which sees both scopes, may still call them contradictory (ST-14: "it is not
    only the old units" contests the Gen1 scope)."""
    by_id = {c["assertion_id"]: c for c in candidates}
    d = Decision()
    superseded: list[Mapping[str, Any]] = []
    # One says do X, the other says avoid X, on the same hardware: a disagreement, whatever
    # the judge said.
    judged = {r.assertion_id: r for r in relations}
    for cand in candidates:
        q = json.loads(cand.get("qualifiers_json") or "{}")
        if cand["assertion_id"] in elsewhere:
            rel = judged.get(cand["assertion_id"])
            if rel is not None and rel.relation is RelationKind.SUPERSEDES:
                judged[cand["assertion_id"]] = Relation(
                    assertion_id=cand["assertion_id"], relation=RelationKind.UNRELATED
                )
            continue
        if (action and action in (q.get("avoid") or [])) or (q.get("action") in avoid):
            judged[cand["assertion_id"]] = Relation(
                assertion_id=cand["assertion_id"], relation=RelationKind.CONTRADICTS
            )
        # The thread re-read after more replies: the same action from the same thread is
        # the knowledge already captured, unless the judge says the new one corrects it.
        elif (
            action
            and q.get("action") == action
            and thread_id in (cand.get("threads") or [])
            and getattr(judged.get(cand["assertion_id"]), "relation", None)
            is not RelationKind.CORRECTS
        ):
            judged[cand["assertion_id"]] = Relation(
                assertion_id=cand["assertion_id"], relation=RelationKind.DUPLICATE
            )
    relations = list(judged.values())
    for r in relations:
        c = by_id.get(r.assertion_id)
        if c is None or r.relation is RelationKind.UNRELATED:
            continue
        same_thread = thread_id in (c.get("threads") or [])
        if r.relation is RelationKind.DUPLICATE and same_thread:
            d.skip_as = d.skip_as or c["assertion_id"]
        elif r.relation in (RelationKind.DUPLICATE, RelationKind.CORROBORATES):
            d.corroborates.append(c["assertion_id"])
        elif r.relation is RelationKind.CORRECTS and same_thread:
            d.correct = d.correct or c["assertion_id"]
        elif (
            r.relation is RelationKind.SUPERSEDES
            and valid_from > c["valid_from"]
            and not (same_thread and valid_from - c["valid_from"] < SAME_CONVERSATION)
        ):
            superseded.append(c)
        else:  # contradicts, a correction from elsewhere, or a "supersedes" that starts earlier
            d.contradicts.append(c["assertion_id"])
    if superseded:
        latest = max(
            superseded, key=lambda c: rank_key(c["recorded_at"], c.get("role"), c.get("platform"))
        )
        d.supersede = latest["assertion_id"]
        d.contradicts += [c["assertion_id"] for c in superseded if c is not latest]
    if d.skip_as or d.correct:  # the thread's own assertion is replaced or kept, not linked
        d.supersede = None
    d.corroborates = sorted(set(d.corroborates))
    d.contradicts = sorted(set(d.contradicts) - set(d.corroborates))
    return d
