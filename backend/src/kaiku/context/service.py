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
from dataclasses import dataclass, field
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
from kaiku.graph.repository import (
    SCOPE_BY_ENTITY_TYPE,
    ContextRepository,
    NotFoundError,
    score,
)
from kaiku.llm.embeddings import (
    Embedder,
    EmbeddingDimensionError,
    EmbeddingUnavailableError,
    embed_query_or_none,
)
from kaiku.logging import get_logger
from kaiku.telemetry.store import ConditionStatus, TelemetryStore
from kaiku.validation.outcomes import OutcomeVerifier
from kaiku.validation.outcomes import conditions_met as outcome_conditions_met

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


# search_context reads entities from the query when no entity_id is given. Below this
# confidence a match is a guess (a city alone, a loose fuzzy alias), so it doesn't scope.
RESOLVE_MIN_CONFIDENCE = 0.8
_WORD = re.compile(r"[\w-]+")
_PREFIX_MIN = 4  # "replac" matches "replacement"; "up" doesn't match "upgrade"


def query_terms(query: str) -> list[str]:
    return list(dict.fromkeys(w for w in _WORD.findall(query.casefold()) if w not in _STOPWORDS))


def lexical_similarity(terms: Sequence[str], assertion: AssertionView) -> float:
    """Share of the query terms found as words of the statement or topic, in [0, 1]. A term
    of `_PREFIX_MIN`+ letters also matches a longer word it starts ("swap" -> "swapped")."""
    if not terms:
        return 0.0
    text = f"{assertion.statement} {(assertion.topic or '').replace('_', ' ')}".casefold()
    words = set(_WORD.findall(text))
    hits = sum(
        1
        for w in terms
        if w in words or (len(w) >= _PREFIX_MIN and any(x.startswith(w) for x in words))
    )
    return hits / len(terms)


def rank_by_similarity(
    candidates: Sequence[AssertionView],
    similarity: Mapping[str, float],
    *,
    keep_unmatched: bool,
) -> list[AssertionView]:
    """R12: score = max(0, similarity) * the structural score (match * status * confidence),
    so a disputed or review_due assertion never ranks level with a believed one just because
    its wording is closer. Neo4j's cosine is already in [0, 1]; the clamp guards any other
    source. With `keep_unmatched` false (keyword mode), candidates sharing no term with the
    query are left out; in semantic mode every candidate is ranked."""
    ranked = [
        a.model_copy(update={"score": round(max(0.0, sim) * score(a), 3)})
        for a in candidates
        if (sim := similarity.get(a.assertion_id, 0.0)) > 0 or keep_unmatched
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


@dataclass
class _Scope:
    assertions: list[AssertionView] = field(default_factory=list)
    entity_ids: set[str] = field(default_factory=set)
    devices: list[dict[str, str]] = field(default_factory=list)  # running snapshots at t


def _fits_any(assertion: AssertionView, devices: Sequence[Mapping[str, str]]) -> bool:
    """R9 against the devices a query names: true when none were named."""
    required = _required(assertion.qualifiers)
    return not devices or any(qualifiers_match(required, d) for d in devices)


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
        self.verifier = OutcomeVerifier(repo, telemetry, clock=clock)

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
        """R12 (plan 004): validity at t first, then rank. Ranking an index top-k and
        filtering afterwards can miss what was valid at a past `as_of`.

        With `entity_id`, only that entity's scope is searched. Without it, entities read
        from the query (`resolve`, confidence >= RESOLVE_MIN_CONFIDENCE) bring their scope
        in as get_context sees it: with its match weight (direct 1.0, model 0.9, ...) and
        its qualifier filter. The rest of the valid knowledge is ranked alongside with the
        unscoped weight (0.7), so a wrong reading of the query lowers nothing below what an
        unscoped search would give, and hides nothing. What get_context leaves out for a
        resolved device is not brought back: knowledge bound to its scope but filtered out,
        and knowledge whose models, generation or firmware match none of the devices named
        (R9), such as a Gen1 rule when the question is about a Gen3 node."""
        t = self._t(as_of)
        k = self.clock()
        limit = max(1, min(limit, 50))
        resolved: list[str] = []
        if entity_id:
            first = (await self._in_scope([entity_id], t)).assertions
            rest: list[AssertionView] = []
        else:
            resolved = [
                c.entity_id
                for c in resolve(query, await self.repo.all_entities(), self.aliases, t)
                if c.confidence >= RESOLVE_MIN_CONFIDENCE
            ]
            scope = await self._in_scope(resolved, t)
            first = scope.assertions
            taken = {a.assertion_id for a in first}
            rest = [
                a
                for a in await self.repo.assertions(await self.repo.valid_assertion_ids(t, k), t, k)
                if a.assertion_id not in taken
                and not any(b.entity_id in scope.entity_ids for b in a.bindings)
                and _fits_any(a, scope.devices)
            ]
        terms = query_terms(query)
        candidates = first + rest
        lexical = {a.assertion_id: lexical_similarity(terms, a) for a in candidates}
        vec = await embed_query_or_none(self.embedder, query)
        note = None
        if vec is None:
            similarity, mode = lexical, "keyword"
        else:
            ids = list(lexical)
            unembedded = await self._embed_now(ids)
            cosine = {h.id: h.score for h in await self.repo.similarities(ids, vec)}
            similarity, mode = {i: cosine.get(i, lexical[i]) for i in ids}, "semantic"
            if missing := [i for i in ids if i not in cosine]:
                note = (
                    f"{len(missing)} assertion(s) have no embedding yet"
                    + (f" ({unembedded})" if unembedded else "")
                    + "; they were ranked by keyword overlap"
                )
        found = rank_by_similarity(candidates, similarity, keep_unmatched=mode == "semantic")[
            :limit
        ]
        return SearchResult(
            query=query,
            as_of=t,
            mode=mode,
            resolved_entities=resolved,
            assertions=found,
            note=note,
        )

    async def _in_scope(self, entity_ids: Sequence[str], t: datetime) -> _Scope:
        """What get_context returns for each entity at t, merged (an assertion reached from
        two entities keeps its best match), every entity id those scopes cover, and what
        each device among them runs at t."""
        scope = _Scope()
        best: dict[str, AssertionView] = {}
        for entity_id in entity_ids:
            entity = await self.get_entity(entity_id, t)
            if entity.entity_type == "device":
                scope.devices.append(_running_snapshot(entity))
            ctx = await self.get_context(entity_id, as_of=t)
            scope.entity_ids.update(ctx.scope_entities)
            for a in ctx.assertions:
                current = best.get(a.assertion_id)
                if current is None or score(a) > score(current):
                    best[a.assertion_id] = a
        scope.assertions = list(best.values())
        return scope

    async def _embed_now(self, ids: Sequence[str]) -> str | None:
        """Embed the candidates the minute-by-minute backfill hasn't reached yet, so a fresh
        capture is searchable at once. Returns why that failed, or None."""
        if self.embedder is None:
            return "no embedder"
        rows = await self.repo.assertions_missing_embeddings(len(ids), ids=ids)
        if not rows:
            return None
        try:
            vectors = await self.embedder.embed_documents([r["statement"] for r in rows])
        except (EmbeddingUnavailableError, EmbeddingDimensionError) as exc:
            log.warning("search_embedding_failed", count=len(rows), error=str(exc))
            return "embedding failed"
        await self.repo.set_embeddings(
            [{"id": r["id"], "vector": v} for r, v in zip(rows, vectors, strict=True)]
        )
        log.info("assertions_embedded_on_search", count=len(rows))
        return None

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
            conditions_met = outcome_conditions_met(check.conditions)
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
        if assertion_id:
            # The outcome event triggers the verifier at once; a verdict it can't reach yet
            # stays pending for the verifier's sweep. Its failure never loses the outcome.
            try:
                out |= await self.verifier.verify(out["event_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "outcome_verify_failed",
                    outcome_id=out["event_id"],
                    error=f"{type(exc).__name__}: {exc}",
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
