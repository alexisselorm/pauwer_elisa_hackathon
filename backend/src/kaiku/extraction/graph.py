"""The extraction graph (plan 001 WS3, BE-3.1):

    triage -> extract -> link -> validate -> reconcile -> persist

- triage: keep or ignore, with a kind and a one-line reason (FAST_MODEL).
- extract: rules with a verbatim quote per field (CHAT_MODEL).
- link: V1/V2 grounding and time resolution (gates.py), then entities and V3 (link.py);
  linking needs the quoted mentions and the start time, so grounding runs first.
- validate: V4, the symptoms claimed in the thread against the last complete PM interval.
- reconcile: V6 against earlier knowledge (reconcile.py).
- persist: one ContextRepository transaction per assertion, with the V5 checks in it.

No HITL `interrupt()`: confirmation is asynchronous on the Slack card, so extraction never
waits for a person. A thread whose LLM calls fail twice is parked as `extraction_failed`;
an assertion is written whole or not at all.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from opentelemetry import trace
from pydantic import BaseModel

from kaiku.context.models import AssertionIn, Scope
from kaiku.errors import WriteRejectedError
from kaiku.extraction import gates, reconcile
from kaiku.extraction.link import Linked, link, ports
from kaiku.extraction.llm import ExtractionError
from kaiku.extraction.models import Judge, RuleDraft, Triage, TriageKind
from kaiku.graph.repository import ContextRepository
from kaiku.ingest.store import ThreadView
from kaiku.logging import get_logger
from kaiku.telemetry.store import ConditionStatus, TelemetryStore
from kaiku.tracing import current_trace_id

log = get_logger(__name__)
tracer = trace.get_tracer(__name__)

CHECKABLE = ("oper_up", "rx_power_normal", "counters_zero")


class Item(BaseModel):
    """One rule on its way through the graph."""

    draft: RuleDraft
    grounded: gates.Grounded | None = None
    linked: Linked | None = None
    telemetry: str = "unknown"  # agrees | disagrees | unknown (V4)
    telemetry_detail: str | None = None
    telemetry_entity: str | None = None
    telemetry_port: str | None = None
    decision: reconcile.Decision | None = None
    assertion_id: str | None = None
    corrected: str | None = None  # the earlier version a correction replaced
    new: bool = False
    dropped: str | None = None


class Step(BaseModel):
    """One node run, in order; reconcile and persist repeat once per rule."""

    node: str
    ms: int


class CaptureResult(BaseModel):
    thread_id: str
    status: str  # captured | ignored | extraction_failed
    kind: str
    reason: str
    items: list[Item] = []
    # What the console shows (UI-1): when it ran, each node's time, and the Phoenix trace.
    started_at: datetime | None = None
    steps: list[Step] = []
    trace_id: str | None = None

    @property
    def assertion_ids(self) -> list[str]:
        return [i.assertion_id for i in self.items if i.assertion_id]


class State(TypedDict, total=False):
    thread: ThreadView
    triage: Triage
    items: list[Item]
    cursor: int  # the rule reconcile and persist are on
    failed: str


Embed = Callable[[str], Awaitable[list[float] | None]]


class Extractor:
    def __init__(
        self,
        repo: ContextRepository,
        judge: Judge,
        telemetry: TelemetryStore | None = None,
        embed: Embed | None = None,
    ) -> None:
        self.repo = repo
        self.judge = judge
        self.telemetry = telemetry
        self.embed = embed
        g = StateGraph(State)
        for name in ("triage", "extract", "link", "validate", "reconcile", "persist"):
            g.add_node(name, getattr(self, f"_{name}"))
        g.set_entry_point("triage")
        g.add_conditional_edges("triage", self._kept, {True: "extract", False: END})
        g.add_conditional_edges("extract", self._has_items, {True: "link", False: END})
        g.add_edge("link", "validate")
        g.add_edge("validate", "reconcile")
        g.add_edge("reconcile", "persist")
        # One rule at a time, so a rule sees the ones persisted before it from the same
        # thread (the KMAX-4K pair disagrees within one conversation).
        g.add_conditional_edges("persist", self._more, {True: "reconcile", False: END})
        self.graph = g.compile()

    async def run(self, thread: ThreadView) -> CaptureResult:
        with tracer.start_as_current_span(
            "capture",
            attributes={"openinference.span.kind": "CHAIN", "thread_id": thread.thread_id},
        ):
            started_at, trace_id = datetime.now(UTC), current_trace_id()
            steps: list[Step] = []
            try:
                state = await self._stream(thread, steps)
            except ExtractionError as exc:
                log.warning("extraction_failed", thread_id=thread.thread_id, error=str(exc))
                return CaptureResult(
                    thread_id=thread.thread_id,
                    status="extraction_failed",
                    kind="none",
                    reason=f"the model failed twice: {exc}"[:300],
                    started_at=started_at,
                    steps=steps,
                    trace_id=trace_id,
                )
        result = self._result(thread, state)
        return result.model_copy(
            update={"started_at": started_at, "steps": steps, "trace_id": trace_id}
        )

    async def _stream(self, thread: ThreadView, steps: list[Step]) -> State:
        """Run the graph node by node, timing each one. The state has no reducers, so
        merging each node's update gives what `ainvoke` would return."""
        state: dict[str, Any] = {"thread": thread}
        last = time.perf_counter()
        async for update in self.graph.astream({"thread": thread}, stream_mode="updates"):
            for node, delta in update.items():
                now = time.perf_counter()
                steps.append(Step(node=node, ms=round((now - last) * 1000)))
                last = now
                state.update(delta or {})
        return state  # type: ignore[return-value]

    @staticmethod
    def _result(thread: ThreadView, state: State) -> CaptureResult:
        triage = state["triage"]
        items = state.get("items", [])
        kept = [i for i in items if i.assertion_id]
        if not triage.relevant:
            status, reason = "ignored", triage.reason
        elif kept:
            status, reason = "captured", triage.reason
        elif any(i.dropped and i.dropped.startswith("write rejected") for i in items):
            status = "extraction_failed"
            reason = "; ".join(i.dropped or "" for i in items)
        else:
            status = "ignored"
            why = "; ".join(i.dropped or "" for i in items) or "no rule in the thread"
            reason = f"kept by triage ({triage.reason}), but nothing passed the gates: {why}"
        return CaptureResult(
            thread_id=thread.thread_id,
            status=status,
            kind=triage.kind.value,
            reason=reason[:500],
            items=items,
        )

    # --- Routing ---------------------------------------------------------------------------

    @staticmethod
    def _kept(state: State) -> bool:
        return state["triage"].relevant

    @staticmethod
    def _has_items(state: State) -> bool:
        return bool(state.get("items"))

    @staticmethod
    def _more(state: State) -> bool:
        return state["cursor"] < len(state["items"])

    # --- Nodes -------------------------------------------------------------------------------

    async def _triage(self, state: State) -> dict[str, Any]:
        thread = state["thread"]
        triage = await self.judge.triage(thread)
        if thread.forced and not triage.relevant:  # the capture shortcut: a person asked
            kind = triage.kind if triage.kind is not TriageKind.NONE else TriageKind.RULE
            triage = Triage(
                relevant=True, kind=kind, reason=f"captured on request ({triage.reason})"
            )
        return {"triage": triage}

    async def _extract(self, state: State) -> dict[str, Any]:
        inventory = await self.repo.all_entities()
        topics = [
            str(e.get("label") or e["entity_id"].removeprefix("TOPIC:"))
            for e in inventory
            if e.get("entity_type") == "topic"
        ]
        out = await self.judge.extract(state["thread"], state["triage"].kind, topics)
        return {"items": [Item(draft=d) for d in out.rules]}

    async def _link(self, state: State) -> dict[str, Any]:
        thread = state["thread"]
        inventory = await self.repo.all_entities()
        items = []
        for item in state["items"]:
            grounded = gates.ground(item.draft, thread.messages)
            if isinstance(grounded, str):
                items.append(item.model_copy(update={"dropped": grounded}))
                continue
            d = grounded.draft
            # The rule's scope is read from its statement and action, not from its avoid
            # quotes, which often name other hardware ("the bounce trick is for Gen1/Gen2").
            linked = link(
                d.entities,
                thread.text(),
                state["triage"].kind,
                inventory,
                grounded.valid_from,
                draft_text=f"{d.statement}\n{d.action.quote if d.action else ''}",
            )
            update: dict[str, Any] = {"grounded": grounded, "linked": linked}
            if not linked.bindings:
                update["dropped"] = "no entity in the inventory matches"
            items.append(item.model_copy(update=update))
        return {"items": items}

    async def _validate(self, state: State) -> dict[str, Any]:
        """V4: the symptoms someone reported, against the last complete PM interval that
        ended at or before they reported them, on each device the rule was observed on."""
        thread = state["thread"]
        items = []
        for item in state["items"]:
            g, linked = item.grounded, item.linked
            if self.telemetry is None or item.dropped or g is None or linked is None:
                items.append(item)
                continue
            claimed = sorted(
                {q.value for q in [*g.draft.observed, *g.draft.conditions]} & set(CHECKABLE)
            )
            if not claimed or g.observed_at is None:
                items.append(item)
                continue
            update: dict[str, Any] = {}
            for b in linked.bindings:
                if b.scope is not Scope.OBSERVED_ON or b.candidate:
                    continue
                try:
                    result = await self._check(b.entity_id, ports(thread.text()), claimed, g)
                except Exception:  # V4 is evidence, not a gate: no check means "unknown"
                    log.warning("telemetry_check_failed", entity_id=b.entity_id, exc_info=True)
                    continue
                if result is not None:
                    update = result
                    if result["telemetry"] == "agrees":
                        break
            items.append(item.model_copy(update=update))
        return {"items": items, "cursor": 0}

    async def _check(
        self, entity_id: str, named: list[str], claimed: list[str], g: gates.Grounded
    ) -> dict[str, Any] | None:
        assert self.telemetry is not None
        assert g.observed_at is not None
        await self.telemetry.refresh(entity_id)
        available = await asyncio.to_thread(self.telemetry.ports, entity_id)
        candidates = [p for p in named if p in available] or available
        verdict: dict[str, Any] | None = None
        for port in candidates:
            results = await self.telemetry.acheck(entity_id, port, claimed, g.observed_at)
            detail = "; ".join(f"{r.name}: {r.detail}" for r in results)
            base = {"telemetry_entity": entity_id, "telemetry_port": port}
            if all(r.status is ConditionStatus.MET for r in results):
                return {**base, "telemetry": "agrees", "telemetry_detail": detail}
            if any(r.status is ConditionStatus.NOT_MET for r in results):
                verdict = {**base, "telemetry": "disagrees", "telemetry_detail": detail}
        return verdict

    async def _reconcile(self, state: State) -> dict[str, Any]:
        thread = state["thread"]
        items = []
        for i, item in enumerate(state["items"]):
            g, linked = item.grounded, item.linked
            if i != state["cursor"] or item.dropped or g is None or linked is None:
                items.append(item)
                continue
            extra: list[str] = []
            if self.embed is not None and (vec := await self.embed(g.draft.statement)):
                try:
                    extra = [h.id for h in await self.repo.search_assertions(vec, 5)]
                except Exception:  # vector search is a bonus: topic and entity still match
                    log.warning("vector_candidates_failed", exc_info=True)
            candidates = await self.repo.reconcile_candidates(
                g.draft.topic, [b.entity_id for b in linked.bindings], g.valid_from, extra
            )
            candidates.sort(
                key=lambda c: reconcile.rank_key(
                    c["recorded_at"], c.get("role"), c.get("platform")
                ),
                reverse=True,
            )
            mine = [
                {"entity_id": b.entity_id, "scope": str(b.scope)}
                for b in linked.bindings
                if not b.candidate and b.scope is not Scope.ACTIVITY
            ]
            relations = await self.judge.relate(
                g.draft, candidates, g.valid_from.isoformat(), scope=mine
            )
            by_id = {e["entity_id"].casefold(): e for e in await self.repo.all_entities()}
            new_hw = reconcile.equipment([m["entity_id"] for m in mine], by_id)
            elsewhere = [
                c["assertion_id"]
                for c in candidates
                if reconcile.disjoint(
                    new_hw,
                    reconcile.equipment([x["entity_id"] for x in c.get("about") or []], by_id),
                )
            ]
            decision = reconcile.decide(
                thread_id=thread.thread_id,
                valid_from=g.valid_from,
                candidates=candidates,
                relations=relations,
                action=g.draft.action.value if g.draft.action else None,
                avoid=[a.value for a in g.draft.avoid],
                elsewhere=elsewhere,
                authority=reconcile.channel_authority(thread.platform),
            )
            items.append(item.model_copy(update={"decision": decision}))
        return {"items": items}

    async def _persist(self, state: State) -> dict[str, Any]:
        thread, triage = state["thread"], state["triage"]
        items = []
        for i, item in enumerate(state["items"]):
            g, linked, d = item.grounded, item.linked, item.decision
            if i != state["cursor"] or item.dropped or g is None or linked is None or d is None:
                items.append(item)
                continue
            if d.skip_as:
                items.append(item.model_copy(update={"assertion_id": d.skip_as}))
                continue
            draft = g.draft
            models = [b.entity_id for b in linked.bindings if b.scope is Scope.APPLIES_TO]
            qualifiers = {
                "symptom": sorted({q.value for q in [*draft.conditions, *draft.observed]}),
                "action": draft.action.value if draft.action else None,
                "avoid": sorted({q.value for q in draft.avoid}),
                "applies_to": models,
                "quotes": {
                    "action": draft.action.quote if draft.action else None,
                    "conditions": [q.quote for q in draft.conditions],
                    "avoid": [q.quote for q in draft.avoid],
                },
                "thread_id": thread.thread_id,
                "telemetry": item.telemetry,
                **({"telemetry_detail": item.telemetry_detail} if item.telemetry_detail else {}),
                **({"time_note": g.time_note} if g.time_note else {}),
                **({"question": linked.question} if linked.question else {}),
            }
            new = AssertionIn(
                statement=draft.statement,
                topic=draft.topic,
                kind=triage.kind.value,
                qualifiers=qualifiers,
                valid_from=g.valid_from,
                valid_to=g.valid_to,
                bindings=linked.bindings,
                sources=g.sources,
                corroborates=d.corroborates,
                contradicts=d.contradicts,
                authority=reconcile.channel_authority(thread.platform),
            )
            try:
                if d.correct:
                    last = thread.messages[-1].event_id
                    out = await self.repo.correct(d.correct, new, last)
                elif d.supersede:
                    out = await self.repo.supersede(d.supersede, new)
                else:
                    out = await self.repo.assert_(new)  # noqa: PT009  # not unittest
            except WriteRejectedError as exc:
                log.warning("write_rejected", thread_id=thread.thread_id, error=str(exc))
                items.append(item.model_copy(update={"dropped": f"write rejected: {exc}"}))
                continue
            if item.telemetry in ("agrees", "disagrees") and item.telemetry_entity:
                assert g.observed_at is not None
                await self.repo.network_check(
                    assertion_id=out.assertion_id,
                    entity_id=item.telemetry_entity,
                    port=item.telemetry_port,
                    observed_at=g.observed_at,
                    agrees=item.telemetry == "agrees",
                    detail=item.telemetry_detail or "",
                )
            items.append(
                item.model_copy(
                    update={
                        "assertion_id": out.assertion_id,
                        "corrected": d.correct,
                        "new": not out.replayed,
                    }
                )
            )
        return {"items": items, "cursor": state["cursor"] + 1}
