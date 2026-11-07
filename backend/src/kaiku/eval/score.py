"""Deterministic scoring for the eval (plan 003 §2.3). No LLM judge.

Capture (T0, T2): each expected assertion is matched to one captured in the same thread by
its observed-on entity and statement keywords; then validity is compared **exactly** (to
the second for event times; the listed alternative readings only where the held-out notes
name one), linking as sets, and status as it stood at the snapshot.

QA: the agent's structured answer against the question's expectations: status, action,
avoid list, words that must (not) appear, and citations by source thread.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kaiku.agents.ops_agent import AnswerStatus, OpsAnswer
from kaiku.context.models import AssertionView

# Held-out notes that accept a second reading (README of eval/heldout).
ALTERNATIVE_VALID_TO = {"H-03a": ["2026-11-20T22:00:00Z"]}
# How an action enum shows up in an answer's own words.
ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "bounce_interface": ("bounce", "disable", "re-enable", "shut", "toggle", "flap"),
}
AVOID_WORDS: dict[str, tuple[str, ...]] = {
    "replace_sfp": ("sfp", "optic", "module"),
    "reboot_node": ("reboot", "reload"),
}


def iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- Sets --------------------------------------------------------------------------------


class HeldoutMessage(BaseModel):
    ts: str
    user: str
    role: str | None = None
    text: str
    subject: str | None = None


class HeldoutThread(BaseModel):
    thread_id: str
    channel: str
    channel_name: str
    messages: list[HeldoutMessage]


class ExpectedAssertion(BaseModel):
    key: str
    topic: str | None = None
    observed_on: list[str] = []
    applies_to: list[str] = []
    statement_contains: list[str] = []
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_at: str | None = None
    status_at_snapshot: str | None = None
    supersedes: str | None = None
    contradicts: list[str] = []


class Expected(BaseModel):
    thread_id: str
    tests: list[str] = []
    expected_assertions: list[ExpectedAssertion] = []
    expected_relations: list[dict[str, Any]] = []
    notes: str = ""


class Question(BaseModel):
    id: str
    ask: str
    as_of: str
    status: str
    expected_action: str | None = None
    expected_avoid: list[str] = []
    expected_contains: list[str] = []
    expected_not_contains: list[str] = []
    expected_not_contains_as_recommendation: list[str] = []
    must_cite_any_of: list[str] = []


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def load_heldout(folder: Path) -> tuple[list[HeldoutThread], list[Expected], list[Question]]:
    return (
        [HeldoutThread.model_validate(r) for r in _jsonl(folder / "threads.jsonl")],
        [Expected.model_validate(r) for r in _jsonl(folder / "expected.jsonl")],
        [Question.model_validate(r) for r in _jsonl(folder / "questions.jsonl")],
    )


# --- Results -----------------------------------------------------------------------------


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ItemResult(BaseModel):
    id: str
    passed: bool
    checks: list[Check] = []
    skipped: bool = False
    note: str = ""
    # What was scored, saved so `kaiku eval rescore` can score again without a run.
    assertions: list[AssertionView] = []
    answer: OpsAnswer | None = None

    @classmethod
    def of(cls, id: str, checks: list[Check], note: str = "") -> ItemResult:
        return cls(id=id, passed=all(c.passed for c in checks), checks=checks, note=note)


class SetResult(BaseModel):
    name: str
    items: list[ItemResult] = []
    field_accuracy: dict[str, str] = Field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(i.passed for i in self.items if not i.skipped)

    @property
    def scored(self) -> int:
        return sum(not i.skipped for i in self.items)


def field_accuracy(items: Iterable[ItemResult]) -> dict[str, str]:
    """Per check name: passed / total, e.g. {'valid_from': '6/7'}."""
    totals: dict[str, list[int]] = {}
    for item in items:
        for c in item.checks:
            t = totals.setdefault(c.name, [0, 0])
            t[0] += c.passed
            t[1] += 1
    return {k: f"{p}/{n}" for k, (p, n) in sorted(totals.items())}


# --- Capture scoring -----------------------------------------------------------------------


def scoped(view: AssertionView, scope: str) -> set[str]:
    """Bound entity ids; topics are stored as `TOPIC:<name>` and scored by name."""
    return {
        b.entity_id.removeprefix("TOPIC:")
        for b in view.bindings
        if b.scope == scope and not b.candidate
    }


def status_at(view: AssertionView, t: datetime) -> str:
    """What the assertion's status means at t: superseded/expired only once t has passed
    its valid_to; before that it is the status it had before (stored: superseded)."""
    if view.valid_to is not None and view.valid_to <= t:
        return "superseded" if view.superseded_by else "expired"
    if view.status in ("superseded", "expired"):
        return "believed_true"
    return str(view.status)


# Trust is not validity: a fresh capture nobody has clicked stays `unverified` (ST-07),
# and the held-out key writes `believed_true` for "current". The status check compares
# validity, so both read as current. Decided 2026-11-07, after the first run.
CURRENT = frozenset({"believed_true", "unverified"})


def same_status(got: str, want: str) -> bool:
    return got == want or (got in CURRENT and want in CURRENT)


def _rank(expected: ExpectedAssertion, v: AssertionView) -> tuple[int, int]:
    # Either field: a capture bound to the device instead of its segment (H-02) is still
    # the same knowledge, and fails the linking check rather than counting as missing.
    entities = scoped(v, "observed_on") | scoped(v, "applies_to")
    overlap = len((set(expected.observed_on) | set(expected.applies_to)) & entities)
    words = sum(w.casefold() in v.statement.casefold() for w in expected.statement_contains)
    return overlap, words


def match_all(
    expected: Sequence[ExpectedAssertion], views: Sequence[AssertionView]
) -> dict[str, AssertionView | None]:
    """One captured assertion per expected one. Two sides of a dispute often share their
    words ("bounce" and "reseat" in both), so matching each alone could score one assertion
    twice. Pairs are taken best first; an assertion used once is not reused."""
    pairs = sorted(
        (
            (_rank(e, v), -j, -k, e.key, v)
            for k, e in enumerate(expected)
            for j, v in enumerate(views)
            if _rank(e, v)[0] > 0
        ),
        key=lambda p: p[:3],
        reverse=True,
    )
    out: dict[str, AssertionView | None] = {e.key: None for e in expected}
    used: set[str] = set()
    for _, _, _, key, v in pairs:
        if out[key] is None and v.assertion_id not in used:
            out[key] = v
            used.add(v.assertion_id)
    return out


def _same(name: str, actual: datetime | None, wanted: str | None, alts: Sequence[str]) -> Check:
    ok = actual == iso(wanted) or any(actual == iso(a) for a in alts)
    return Check(name=name, passed=ok, detail=f"got {actual}, want {wanted}")


def score_assertion(
    exp: ExpectedAssertion, view: AssertionView | None, snapshot: datetime
) -> list[Check]:
    if view is None:
        return [Check(name="captured", passed=False, detail="no matching assertion")]
    checks = [Check(name="captured", passed=True, detail=view.assertion_id)]
    observed = scoped(view, "observed_on")
    reach = observed | scoped(view, "applies_to")
    checks.append(
        Check(
            name="link_observed_on",
            passed=set(exp.observed_on) <= observed,
            detail=f"got {sorted(observed)}, want {exp.observed_on}",
        )
    )
    if exp.applies_to:
        allowed = set(exp.applies_to) | set(exp.observed_on)
        widened = scoped(view, "applies_to") - allowed
        checks.append(
            Check(
                name="link_applies_to",
                passed=set(exp.applies_to) <= reach and not widened,
                detail=f"got {sorted(reach)}, want {exp.applies_to}"
                + (f"; widened to {sorted(widened)}" if widened else ""),
            )
        )
    if exp.valid_from is not None:
        checks.append(_same("valid_from", view.valid_from, exp.valid_from, ()))
    if "valid_to" in exp.model_fields_set:
        alts = ALTERNATIVE_VALID_TO.get(exp.key, [])
        checks.append(_same("valid_to", view.valid_to, exp.valid_to, alts))
    if exp.recorded_at is not None:
        checks.append(_same("recorded_at", view.recorded_at, exp.recorded_at, ()))
    if exp.status_at_snapshot is not None:
        got = status_at(view, snapshot)
        checks.append(
            Check(
                name="status",
                passed=same_status(got, exp.status_at_snapshot),
                detail=f"got {got}, want {exp.status_at_snapshot}",
            )
        )
    return checks


def score_relations(
    expected: Sequence[ExpectedAssertion], matched: Mapping[str, AssertionView | None]
) -> list[Check]:
    checks = []
    for exp in expected:
        view = matched.get(exp.key)
        if exp.supersedes:
            other = matched.get(exp.supersedes)
            ok = view is not None and other is not None and view.supersedes == other.assertion_id
            checks.append(Check(name="supersedes", passed=ok, detail=f"{exp.key}>{exp.supersedes}"))
        for key in exp.contradicts:
            other = matched.get(key)
            ok = view is not None and other is not None and other.assertion_id in view.contradicts
            checks.append(Check(name="contradicts", passed=ok, detail=f"{exp.key}<>{key}"))
    return checks


# --- QA scoring ----------------------------------------------------------------------------


def _text(answer: OpsAnswer, *, recommendations_only: bool = False) -> str:
    parts = [answer.summary, *(s.action for s in answer.steps)]
    if not recommendations_only:
        parts += [*answer.avoid, *answer.review_notes]
    return "\n".join(parts).casefold()


def score_answer(q: Question, answer: OpsAnswer, cite_ids: Mapping[str, set[str]]) -> list[Check]:
    checks = []
    status = answer.status
    if q.status == "not_documented":
        ok = status is AnswerStatus.NOT_DOCUMENTED and not answer.steps
        checks.append(Check(name="not_documented", passed=ok, detail=f"got {status}"))
        checks.append(
            Check(name="false_confident", passed=bool(not answer.steps), detail="steps given")
        )
        return checks
    if q.status == "documented":
        ok = status in (AnswerStatus.ANSWERED, AnswerStatus.PARTIAL)
        checks.append(Check(name="answered", passed=ok, detail=f"got {status}"))
    if q.status == "disputed":
        ok = status is AnswerStatus.DISPUTED or "disput" in _text(answer)
        checks.append(Check(name="dispute_shown", passed=ok, detail=f"got {status}"))
    if q.expected_action:
        words = ACTION_WORDS.get(q.expected_action, (q.expected_action,))
        steps = " ".join(s.action for s in answer.steps).casefold()
        checks.append(
            Check(
                name="action",
                passed=any(w in steps for w in words),
                detail=steps[:120],
            )
        )
    for avoid in q.expected_avoid:
        words = AVOID_WORDS.get(avoid, (avoid,))
        text = " ".join(answer.avoid).casefold()
        checks.append(Check(name="avoid", passed=any(w in text for w in words), detail=avoid))
    text = _text(answer)
    for w in q.expected_contains:
        checks.append(Check(name="contains", passed=w.casefold() in text, detail=w))
    rec = _text(answer, recommendations_only=True)
    for w in q.expected_not_contains:
        checks.append(Check(name="not_contains", passed=w.casefold() not in rec, detail=w))
    for w in q.expected_not_contains_as_recommendation:
        steps = " ".join(s.action for s in answer.steps).casefold()
        checks.append(Check(name="not_recommended", passed=w.casefold() not in steps, detail=w))
    if q.must_cite_any_of:
        wanted = set().union(*(cite_ids.get(src, set()) for src in q.must_cite_any_of))
        cited = {s.assertion_id for s in answer.sources}
        cited |= {i for s in answer.steps for i in s.assertion_ids}
        checks.append(
            Check(
                name="citation",
                passed=bool(wanted & cited),
                detail=f"cited {sorted(cited)}, want any of {sorted(wanted)}",
            )
        )
    return checks


# --- One thread, one question: pure, so a saved run can be scored again --------------------


def score_thread(
    exp: Expected,
    views: Sequence[AssertionView],
    *,
    note: str,
    elisa_rule: str | None,
    snapshot: datetime,
) -> ItemResult:
    checks: list[Check] = []
    if not exp.expected_assertions and not exp.expected_relations:
        checks.append(
            Check(
                name="nothing_captured",
                passed=not views,
                detail=f"{len(views)} captured: {note}",
            )
        )
    elif not exp.expected_assertions:  # H-01: corroborate, never duplicate
        dup = [v.assertion_id for v in views if elisa_rule not in v.corroborates]
        checks.append(Check(name="no_duplicate", passed=not dup, detail=str(dup)))
    matched = match_all(exp.expected_assertions, views)
    for ea in exp.expected_assertions:
        checks += [
            c.model_copy(update={"detail": f"{ea.key}: {c.detail}"})
            for c in score_assertion(ea, matched[ea.key], snapshot)
        ]
    checks += score_relations(exp.expected_assertions, matched)
    item = ItemResult.of(exp.thread_id, checks, note=note[:200])
    item.assertions = list(views)
    return item


def cite_map(
    elisa_rule: str | None, captured: Mapping[str, Sequence[AssertionView]]
) -> dict[str, set[str]]:
    """Source name in questions.jsonl -> the assertion ids that count as citing it."""
    cite: dict[str, set[str]] = {}
    if elisa_rule:
        cite["Elisa TRN-042 Teams message 8"] = {elisa_rule}
        cite["Elisa TRN-042 email"] = {elisa_rule}
    for thread_id, views in captured.items():
        cite[thread_id] = {v.assertion_id for v in views}
        for v in views:
            cite[thread_id] |= set(v.corroborates)
    return cite


def score_question(q: Question, answer: OpsAnswer, cite: Mapping[str, set[str]]) -> ItemResult:
    item = ItemResult.of(
        q.id, score_answer(q, answer, cite), note=f"{answer.status}: {answer.summary}"[:200]
    )
    item.answer = answer
    return item
