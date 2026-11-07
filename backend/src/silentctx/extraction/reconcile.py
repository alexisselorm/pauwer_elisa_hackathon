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

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
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
) -> Decision:
    by_id = {c["assertion_id"]: c for c in candidates}
    d = Decision()
    superseded: list[Mapping[str, Any]] = []
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
        elif r.relation is RelationKind.SUPERSEDES and valid_from > c["valid_from"]:
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
