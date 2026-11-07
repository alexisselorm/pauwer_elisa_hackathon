"""Capture-time gates V1 and V2 (plan 001 §6.1) and the time resolution (R2, R3, R4a).

V1 Grounding: every field keeps a verbatim quote from a message, or it is dropped. A rule
without an action quote is dropped (ST-45).
V2 Speaker: a rule comes from an answer. An action quoted from a question ("So another
SFP?") is a guess, so the rule is dropped; rejected suggestions belong in `avoid` (ST-04).
Times are resolved by code against the quoting message's event_time, never by the LLM.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

from kaiku.context.models import SourceIn
from kaiku.domain import time as rules
from kaiku.extraction.models import Quoted, RuleDraft, StatedTimeOut
from kaiku.ingest.store import MessageView

_SPACE = re.compile(r"\s+")
# A validity start or end names a day: a month or weekday name, a relative day, or a
# numeric date. A bare year ("installed around 2019") dates equipment, not knowledge.
_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(mon|tues?|wed(nes)?|thu(rs)?|fri|sat(ur)?|sun)(day)?\b"
    r"|\b(today|tonight|tomorrow|yesterday)\b"
    r"|\b(tammi|helmi|maalis|huhti|touko|kesä|heinä|elo|syys|loka|marras|joulu)kuu"
    r"|\b(maanantai|tiistai|keskiviikko|torstai|perjantai|lauantai|sunnuntai|huomenna|tänään)"
    r"|\b\d{1,2}[./]\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
_EDGE = "\"'\u201c\u201d\u2018\u2019`.,;:!\u2026- "


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text.replace("\u2019", "'")).strip(_EDGE).casefold()


def locate(quote: str, messages: Sequence[MessageView]) -> MessageView | None:
    """The message that contains the quote verbatim (case and spacing aside); the latest one
    if several do, since that is when it was said last."""
    q = _norm(quote)
    if len(q) < 3:
        return None
    hits = [m for m in messages if q in _norm(m.text)]
    return hits[-1] if hits else None


def is_question(quote: str, message: MessageView) -> bool:
    """The quoted sentence ends in a question mark in the message."""
    text = _SPACE.sub(" ", message.text.replace("\u2019", "'"))
    q = _norm(quote)
    idx = text.casefold().find(q)
    if idx < 0 or q.endswith("?"):
        return quote.rstrip().endswith("?")
    tail = text[idx + len(q) :]
    sentence_end = re.search(r"[.!?\n]", tail)
    return bool(sentence_end and sentence_end.group(0) == "?")


# V2 backstop: a suggestion asked as a question and answered "No" by someone else is an
# avoid, even if the model missed it ("So another SFP?" -> "No. Usually the SFP is fine.").
_NO = re.compile(r"^\s*(no|nope|ei|don'?t)\b", re.IGNORECASE)
_SUGGESTED = (
    ("replace_sfp", re.compile(r"\b(sfp|optic|module|transceiver)", re.IGNORECASE)),
    ("reboot_node", re.compile(r"\b(reboot|reload|restart)\b", re.IGNORECASE)),
    ("power_cycle_radio", re.compile(r"\bpower[- ]?cycl", re.IGNORECASE)),
)


def rejected_suggestions(messages: Sequence[MessageView]) -> list[Quoted]:
    out = []
    for asked, answer in itertools.pairwise(messages):
        if asked.author == answer.author or not _NO.match(answer.text):
            continue
        for line in asked.text.splitlines():
            if line.rstrip().endswith("?"):
                out += [
                    Quoted(value=v, quote=line.strip()) for v, p in _SUGGESTED if p.search(line)
                ]
    return out


class Grounded(BaseModel):
    draft: RuleDraft
    sources: list[SourceIn]
    recorded_at: datetime  # R2: the last source message
    valid_from: datetime
    valid_to: datetime | None = None
    observed_at: datetime | None = None  # when the symptoms were reported (V4)
    time_note: str | None = None  # an ambiguous or unresolved date: asked on the card
    dropped: list[str] = []  # what V1/V2 removed, for the log


def ground(draft: RuleDraft, messages: Sequence[MessageView]) -> Grounded | str:
    """Apply V1 and V2 and resolve times. Returns the grounded draft, or why it was dropped."""
    dropped: list[str] = []

    def keep(items: Sequence[Quoted], field: str) -> list[tuple[Quoted, MessageView]]:
        out = []
        for q in items:
            m = locate(q.quote, messages)
            if m is None:
                dropped.append(f"{field}:{q.value} (no verbatim quote)")
            else:
                out.append((q, m))
        return out

    if draft.action is None:
        return "V1: no action"
    action_msg = locate(draft.action.quote, messages)
    if action_msg is None:
        return "V1: the action has no verbatim quote"
    if is_question(draft.action.quote, action_msg):
        return "V2: the action comes from a question, not an answer"
    conditions = keep(draft.conditions, "condition")
    said = {a.value for a in draft.avoid}
    missed = [r for r in rejected_suggestions(messages) if r.value not in said]
    avoid = keep([*draft.avoid, *missed], "avoid")
    observed = keep(draft.observed, "observed")
    entities = [e for e in draft.entities if locate(e.surface_form, messages) is not None]
    dropped += [f"entity:{e.surface_form}" for e in draft.entities if e not in entities]

    used = [(draft.action, action_msg), *conditions, *avoid]
    sources: dict[str, SourceIn] = {}
    for q, m in used:
        if m.event_id in sources:
            current = sources[m.event_id].quote or ""
            sources[m.event_id] = SourceIn(event_id=m.event_id, quote=f"{current} … {q.quote}")
        else:
            sources[m.event_id] = SourceIn(event_id=m.event_id, quote=q.quote)
    recorded = max(m.event_time for _, m in used)

    time_note = None
    valid_from = recorded
    for field in ("valid_from", "valid_to"):
        stated = getattr(draft, field)
        if stated is not None and not _DAY.search(stated.phrase):
            dropped.append(f"{field}:{stated.phrase!r} (names no day)")
            draft = draft.model_copy(update={field: None})
    if draft.valid_from is not None:
        resolved = _resolve(draft.valid_from, messages, recorded)
        if isinstance(resolved, str):
            time_note = resolved
        else:
            valid_from = resolved
    valid_to = None
    if draft.valid_to is not None:
        at = locate(draft.valid_to.phrase, messages)
        try:
            valid_to = rules.resolve_stated_expiry(
                _stated(draft.valid_to), at.event_time if at else recorded, valid_from
            )
        except (rules.TemporalError, ValueError) as exc:
            time_note = time_note or f"end date {draft.valid_to.phrase!r}: {exc}"

    clean = draft.model_copy(
        update={
            "conditions": [q for q, _ in conditions],
            "avoid": [q for q, _ in avoid],
            "observed": [q for q, _ in observed],
            "entities": entities,
        }
    )
    reported = [m.event_time for _, m in observed] or [m.event_time for _, m in conditions]
    return Grounded(
        draft=clean,
        sources=sorted(sources.values(), key=lambda s: s.event_id),
        recorded_at=recorded,
        valid_from=valid_from,
        valid_to=valid_to,
        observed_at=max(reported) if reported else None,
        time_note=time_note,
        dropped=dropped,
    )


def _stated(t: StatedTimeOut) -> rules.StatedTime:
    tense = rules.Tense.FUTURE if t.tense.casefold().startswith("f") else rules.Tense.PAST
    return rules.StatedTime(phrase=t.phrase, tense=tense)


def _resolve(
    t: StatedTimeOut, messages: Sequence[MessageView], recorded: datetime
) -> datetime | str:
    at = locate(t.phrase, messages)
    if at is None:
        return f"start date {t.phrase!r} is not in the thread; using the message time"
    result = rules.resolve_valid_from(recorded, _stated(t), event_time=at.event_time)
    if result.unresolved_phrase:
        return f"couldn't tell which date {t.phrase!r} means; using the message time"
    return result.valid_from
