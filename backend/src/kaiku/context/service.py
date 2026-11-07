"""ContextService: the semantics behind every MCP tool (plan 002 §4).

Defaults: `as_of` and `known_at` are "now"; ports default to the device's only monitored
port. Governance (plan 001 §7): agents can add evidence, gaps, proposals and flags; they
can't confirm, retract or change validity. Bad input raises `ContextError`, which the MCP
layer returns as a tool error.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from kaiku.context.models import (
    AssertionView,
    Binding,
    ConditionsResult,
    ContextResult,
    EntityView,
    EventsResult,
    FlagRecorded,
    GapOpened,
    HistoryResult,
    Match,
    OutcomeRecorded,
    OutcomeResult,
    ProposalCreated,
    ResolveResult,
    SearchResult,
    SiteAccessResult,
)
from kaiku.context.resolve import resolve
from kaiku.domain.models import AssertionStatus
from kaiku.domain.time import ensure_utc, qualifiers_match
from kaiku.graph.cypher.vector import MAX_K
from kaiku.graph.repository import (
    SCOPE_BY_ENTITY_TYPE,
    ContextRepository,
    NotFoundError,
    score,
)
from kaiku.llm.embeddings import Embedder, embed_query_or_none
from kaiku.logging import get_logger
from kaiku.telemetry.store import ConditionStatus, TelemetryStore

log = get_logger(__name__)
MAX_EVENTS = 200
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from", "has",
        "have", "how", "i", "in", "is", "it", "its", "of", "on", "or", "our", "should", "that",
        "the", "this", "to", "was", "we", "what", "when", "where", "which", "who", "why",
        "will", "with", "you",
    }
)  # fmt: skip


def rank_semantic(
    candidates: Sequence[AssertionView], cosine: Mapping[str, float]
) -> list[AssertionView]:
    """R12: score = max(0, cosine) * the structural score (match * status * confidence), so
    a disputed or review_due assertion never ranks level with a believed one just because
    its wording is closer. Neo4j's cosine is already in [0, 1]; the clamp guards any other
    source. Candidates without an embedding have no cosine and are left out, as before;
    they are still reachable through get_context."""
    ranked = [
        a.model_copy(update={"score": round(max(0.0, cosine[a.assertion_id]) * score(a), 3)})
        for a in candidates
        if a.assertion_id in cosine
    ]
    return sorted(ranked, key=lambda a: (-(a.score or 0.0), a.assertion_id))


class ContextError(ValueError):
    """A request the context layer refuses (unknown id, governance rule, bad window)."""


# Assertion qualifier -> as-running snapshot key it constrains (R9).
_QUALIFIER_KEYS = {
    "applies_to_models": "hw_model",
    "applies_to_generation": "generation",
    "applies_to_firmware": "firmware",
}


def _required(qualifiers: Mapping[str, Any]) -> dict[str, Any]:
    return {key: qualifiers[q] for q, key in _QUALIFIER_KEYS.items() if qualifiers.get(q)}


def _running_snapshot(entity: EntityView) -> dict[str, str]:
    """What the device is at as_of: the as-running model wins over the as-built one."""
    values = {
        "hw_model": entity.running_hw_model or entity.model,
        "generation": entity.generation,
        "firmware": entity.firmware,
    }
    return {k: v for k, v in values.items() if v}


def _now() -> datetime:
    return datetime.now(UTC)


class ContextService:
    def __init__(
        self,
        repo: ContextRepository,
        telemetry: TelemetryStore,
        *,
        embedder: Embedder | None = None,
        aliases: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.repo = repo
        self.telemetry = telemetry
        self.embedder = embedder
        self.aliases = dict(aliases or {})
        self.clock = clock

    def _t(self, value: datetime | None) -> datetime:
        return ensure_utc(value) if value is not None else self.clock()

    # --- Reads ----------------------------------------------------------------------------

    async def resolve_entities(self, text: str, as_of: datetime | None = None) -> ResolveResult:
        t = self._t(as_of)
        entities = await self.repo.all_entities()
        return ResolveResult(
            text=text, as_of=t, candidates=resolve(text, entities, self.aliases, t)
        )

    async def get_entity(self, entity_id: str, as_of: datetime | None = None) -> EntityView:
        try:
            return await self.repo.get_entity(entity_id, self._t(as_of))
        except NotFoundError as exc:
            raise ContextError(str(exc)) from exc

    async def get_context(
        self,
        entity_id: str,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
        topic: str | None = None,
        symptoms: Sequence[str] | None = None,
        statuses: Sequence[AssertionStatus] | None = None,
    ) -> ContextResult:
        t, k = self._t(as_of), self._t(known_at)
        entity = await self.get_entity(entity_id, t)
        scope = await self.repo.scope(entity_id, t)
        same_model = entity.model if entity.entity_type == "device" else None
        items = await self.repo.context(scope, t, k, same_model_of=same_model)
        if entity.entity_type == "device":
            snapshot = _running_snapshot(entity)
            items = [a for a in items if qualifiers_match(_required(a.qualifiers), snapshot)]
        if topic:
            items = [a for a in items if a.topic and topic.casefold() in a.topic.casefold()]
        if symptoms:
            wanted = {s.casefold() for s in symptoms}
            items = [
                a
                for a in items
                if wanted & {str(x).casefold() for x in a.qualifiers.get("symptom", [])}
            ]
        if statuses:
            items = [a for a in items if a.status in set(statuses)]
        note = None
        if not entity.exists_at:
            note = f"{entity_id} was not in service at {t.isoformat()}"
        elif not items:
            note = "nothing documented for this entity at this time"
        return ContextResult(
            entity_id=entity_id,
            as_of=t,
            known_at=k,
            entity_exists=entity.exists_at,
            scope_entities=list(scope),
            assertions=items,
            note=note,
        )

    async def search_context(
        self,
        query: str,
        as_of: datetime | None = None,
        entity_id: str | None = None,
        limit: int = 10,
    ) -> SearchResult:
        t = self._t(as_of)
        k = self.clock()
        limit = max(1, min(limit, 50))
        scoped: list[AssertionView] | None = None
        if entity_id:
            scoped = (await self.get_context(entity_id, as_of=t)).assertions
        vec = await embed_query_or_none(self.embedder, query)
        if vec is not None:
            # R12 (plan 004): validity at t first, then rank. Ranking an index top-k and
            # filtering afterwards can miss what was valid at a past `as_of`.
            if scoped is not None:
                candidates = scoped
            else:
                candidates = await self.repo.assertions(
                    await self.repo.valid_assertion_ids(t, k), t, k
                )
            hits = await self.repo.rank_assertions(
                [a.assertion_id for a in candidates], vec, max(1, min(len(candidates), MAX_K))
            )
            found = rank_semantic(candidates, {h.id: h.score for h in hits})[:limit]
            return SearchResult(query=query, as_of=t, mode="semantic", assertions=found)
        terms = [w for w in re.findall(r"[\w-]+", query.casefold()) if w not in _STOPWORDS]
        if scoped is not None:
            found = [
                a
                for a in scoped
                if any(w in a.statement.casefold() or w in (a.topic or "") for w in terms)
            ][:limit]
        else:
            found = await self.repo.keyword_search(terms, t, k, limit)
        return SearchResult(query=query, as_of=t, mode="keyword", assertions=found)

    async def check_conditions(
        self,
        assertion_id: str,
        entity_id: str,
        port: str | None = None,
        at: datetime | None = None,
    ) -> ConditionsResult:
        t = self._t(at)
        found = await self.repo.assertions([assertion_id])
        if not found:
            raise ContextError(f"unknown assertion {assertion_id!r}")
        await self.get_entity(entity_id, t)
        symptoms = [str(s) for s in found[0].qualifiers.get("symptom", [])]
        if not symptoms:
            return ConditionsResult(
                assertion_id=assertion_id,
                entity_id=entity_id,
                port=port,
                at=t,
                conditions=[],
                all_met=False,
                note="this assertion states no checkable conditions",
            )
        await self.telemetry.refresh(entity_id)
        ports = await self._ports(entity_id)
        chosen = port or (ports[0] if len(ports) == 1 else None)
        note = None
        if chosen is None:
            note = (
                f"no telemetry for {entity_id}"
                if not ports
                else f"several ports have telemetry ({', '.join(ports)}): pass `port`"
            )
            conditions = await self.telemetry.acheck(entity_id, "", symptoms, t)
        else:
            conditions = await self.telemetry.acheck(entity_id, chosen, symptoms, t)
        return ConditionsResult(
            assertion_id=assertion_id,
            entity_id=entity_id,
            port=chosen,
            at=t,
            conditions=conditions,
            all_met=all(c.status is ConditionStatus.MET for c in conditions),
            note=note,
        )

    async def _ports(self, entity_id: str) -> list[str]:
        return await asyncio.to_thread(self.telemetry.ports, entity_id)

    async def get_site_access(
        self,
        entity_or_site: str,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> SiteAccessResult:
        t, k = self._t(as_of), self._t(known_at)
        await self.get_entity(entity_or_site, t)
        scope = await self.repo.scope(entity_or_site, t)
        segments, assertions, access_register = await self.repo.site_access(scope, t, k)
        note = None
        if not assertions and not access_register:
            private = [s.entity_id for s in segments if s.private_road]
            note = (
                f"no access knowledge recorded; private road on {', '.join(private)}"
                if private
                else "no access knowledge recorded"
            )
        return SiteAccessResult(
            entity_id=entity_or_site,
            as_of=t,
            known_at=k,
            segments=segments,
            assertions=assertions,
            access_register=access_register,
            note=note,
        )

    async def get_events(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        types: Sequence[str] | None = None,
        limit: int = 100,
    ) -> EventsResult:
        start, end = ensure_utc(start), ensure_utc(end)
        if end <= start:
            raise ContextError("`end` must be after `start`")
        await self.get_entity(entity_id, start)
        events, truncated = await self.repo.events(
            entity_id, start, end, types, max(1, min(limit, MAX_EVENTS))
        )
        return EventsResult(
            entity_id=entity_id, start=start, end=end, events=events, truncated=truncated
        )

    async def get_history(self, assertion_id: str) -> HistoryResult:
        try:
            versions, contradicts, corroborates, evidence, flags = await self.repo.history(
                assertion_id
            )
        except NotFoundError as exc:
            raise ContextError(str(exc)) from exc
        return HistoryResult(
            assertion_id=assertion_id,
            versions=versions,
            contradicts=contradicts,
            corroborates=corroborates,
            evidence=evidence,
            flags=flags,
        )

    # --- Writes ---------------------------------------------------------------------------

    async def _require_entities(self, ids: Sequence[str]) -> dict[str, str]:
        if not ids:
            raise ContextError("at least one entity id is required")
        types = await self.repo.entity_types(ids)
        unknown = sorted(set(ids) - set(types))
        if unknown:
            raise ContextError(f"unknown entities: {', '.join(unknown)}")
        return types

    async def _require_events(self, ids: Sequence[str], *, at_least_one: bool) -> None:
        if at_least_one and not ids:
            raise ContextError("a proposal must cite at least one existing event")
        missing = sorted(set(ids) - await self.repo.existing_events(ids))
        if missing:
            raise ContextError(f"unknown events: {', '.join(missing)}")

    async def report_outcome(
        self,
        *,
        agent_id: str,
        entity_id: str,
        action: str,
        result: OutcomeResult,
        observed_at: datetime | None,
        idempotency_key: str,
        assertion_id: str | None = None,
        port: str | None = None,
        evidence_ref: str | None = None,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> OutcomeRecorded:
        await self._require_entities([entity_id])
        observed = self._t(observed_at)
        conditions_met: bool | None = None
        if assertion_id:
            if not await self.repo.assertions([assertion_id]):
                raise ContextError(f"unknown assertion {assertion_id!r}")
            # Were the rule's conditions true just before the action? (plan 001 §6.2)
            check = await self.check_conditions(assertion_id, entity_id, port, observed)
            statuses = {c.status for c in check.conditions}
            if check.conditions and statuses == {ConditionStatus.MET}:
                conditions_met = True
            elif ConditionStatus.NOT_MET in statuses:
                conditions_met = False
        out, replayed = await self.repo.record_outcome(
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            now=self.clock(),
            entity_id=entity_id,
            assertion_id=assertion_id,
            port=port,
            action=action,
            result=result.value,
            observed_at=observed,
            evidence_ref=evidence_ref,
            conditions_met=conditions_met,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )
        return OutcomeRecorded.model_validate({**out, "replayed": replayed})

    async def open_knowledge_gap(
        self,
        *,
        agent_id: str,
        question: str,
        entity_ids: Sequence[str],
        idempotency_key: str,
        context: str | None = None,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> GapOpened:
        if not question.strip():
            raise ContextError("the question is empty")
        await self._require_entities(entity_ids)
        routed_to = await self.repo.top_author(entity_ids)
        out, replayed = await self.repo.open_gap(
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            now=self.clock(),
            question=question.strip(),
            entity_ids=entity_ids,
            context=context,
            routed_to=routed_to,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )
        return GapOpened.model_validate({**out, "replayed": replayed})

    async def propose_assertion(
        self,
        *,
        agent_id: str,
        statement: str,
        entity_ids: Sequence[str],
        evidence_event_ids: Sequence[str],
        rationale: str,
        idempotency_key: str,
        topic: str | None = None,
        qualifiers: Mapping[str, Any] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> ProposalCreated:
        if not statement.strip():
            raise ContextError("the statement is empty")
        if not rationale.strip():
            raise ContextError("a proposal needs a rationale")
        types = await self._require_entities(entity_ids)
        await self._require_events(evidence_event_ids, at_least_one=True)
        now = self.clock()
        vf = ensure_utc(valid_from) if valid_from else now
        vt = ensure_utc(valid_to) if valid_to else None
        if vt is not None and vt <= vf:
            raise ContextError("valid_to must be after valid_from")
        bindings = [
            {"entity_id": i, "scope": SCOPE_BY_ENTITY_TYPE.get(types[i], "observed_on")}
            for i in entity_ids
        ]
        out, replayed = await self.repo.propose(
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            now=now,
            statement=statement.strip(),
            topic=topic,
            bindings=bindings,
            qualifiers=qualifiers or {},
            valid_from=vf,
            valid_to=vt,
            evidence_event_ids=evidence_event_ids,
            rationale=rationale.strip(),
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )
        out["bindings"] = [Binding.model_validate(b) for b in out["bindings"]]
        return ProposalCreated.model_validate({**out, "replayed": replayed})

    async def flag_assertion(
        self,
        *,
        agent_id: str,
        assertion_id: str,
        reason: str,
        evidence_event_ids: Sequence[str],
        idempotency_key: str,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> FlagRecorded:
        if not reason.strip():
            raise ContextError("a flag needs a reason")
        if not await self.repo.assertions([assertion_id]):
            raise ContextError(f"unknown assertion {assertion_id!r}")
        await self._require_events(evidence_event_ids, at_least_one=False)
        out, replayed = await self.repo.flag(
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            now=self.clock(),
            assertion_id=assertion_id,
            reason=reason.strip(),
            evidence_event_ids=evidence_event_ids,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )
        return FlagRecorded.model_validate({**out, "replayed": replayed})

    # --- Maintenance ----------------------------------------------------------------------

    async def embed_missing(self) -> int:
        """Embed assertions that have no vector yet (seeded gold, proposals)."""
        if self.embedder is None:
            return 0
        rows = await self.repo.assertions_missing_embeddings()
        if not rows:
            return 0
        vectors = await self.embedder.embed_documents([r["statement"] for r in rows])
        await self.repo.set_embeddings(
            [{"id": r["id"], "vector": v} for r, v in zip(rows, vectors, strict=True)]
        )
        log.info("assertions_embedded", count=len(rows))
        return len(rows)


def match_label(match: Match | None) -> str:
    return (
        {
            Match.DIRECT: "about this entity",
            Match.MODEL: "about its hardware model",
            Match.SITE: "about its site",
            Match.SEGMENT: "about its site/road",
            Match.SAME_MODEL_DEVICE: "seen on another device of the same model",
        }.get(match, "related")
        if match
        else "related"
    )
